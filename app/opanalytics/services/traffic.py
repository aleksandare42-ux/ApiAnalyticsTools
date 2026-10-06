"""Traffic simulator.

Sends payments from our fake merchants to the gateway with the TPS from
the `traffic` settings and refunds some of the approved ones later.
"""

import asyncio
import os
import random
import time
from contextlib import asynccontextmanager

import httpx
import numpy as np
from fastapi import FastAPI
from pydantic import BaseModel, Field

from .. import chaos, reference
from ..db import SettingsCache, create_pool, init_db, patch_settings

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
TICK = 0.1
# Stay well below the 512 sockets select() can handle on Windows.
MAX_IN_FLIGHT = 200


class Control(BaseModel):
    running: bool | None = None
    tps: float | None = Field(None, ge=0, le=500)


class Burst(BaseModel):
    count: int = Field(100, ge=1, le=20000)


def new_stats():
    return {
        "sent": 0,
        "approved": 0,
        "declined": 0,
        "error": 0,
        "http_errors": 0,
        "dropped": 0,
        "refunds": 0,
        "in_flight": 0,
        "last_error": None,
        "started": time.time(),
    }


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await create_pool()
    await init_db(app.state.pool)
    app.state.settings = SettingsCache(app.state.pool, "traffic", ttl=1.0)
    app.state.chaos = chaos.ChaosCache(app.state.pool)
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=100)
    app.state.http = httpx.AsyncClient(
        base_url=GATEWAY_URL, timeout=15, limits=limits
    )
    app.state.stats = new_stats()
    app.state.tasks = set()
    loop_task = asyncio.create_task(generate_traffic())
    yield
    loop_task.cancel()
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="Traffic simulator", lifespan=lifespan)


def start_task(coro):
    # keep a reference, otherwise the task can be garbage collected
    task = asyncio.create_task(coro)
    app.state.tasks.add(task)
    task.add_done_callback(app.state.tasks.discard)


async def refund_later(transaction_id, delay):
    await asyncio.sleep(delay)
    try:
        resp = await app.state.http.post(
            f"/v1/payments/{transaction_id}/refund"
        )
    except httpx.HTTPError:
        return
    if resp.status_code == 200:
        app.state.stats["refunds"] += 1


def maybe_refund(payment, result, rules, settings):
    merchant = reference.MERCHANT_BY_ID[payment.merchant_id]
    ctx = {
        "merchant": payment.merchant_id,
        "country": payment.country,
        "payment_method": payment.payment_method,
        "provider": result["provider"],
    }
    effects = chaos.aggregate(rules, ctx)
    if random.random() < merchant["base_refund_rate"] + effects.refund_rate:
        low, high = settings.get("refund_delay_s", [5, 60])
        delay = random.uniform(low, high)
        start_task(refund_later(result["transaction_id"], delay))


async def send_payment(rules, settings):
    stats = app.state.stats
    payment = reference.random_payment(
        merchant_mult=chaos.volume_multipliers(rules, "merchant"),
        country_mult=chaos.volume_multipliers(rules, "country"),
    )
    stats["sent"] += 1
    stats["in_flight"] += 1
    try:
        resp = await app.state.http.post("/v1/payments", json=payment.__dict__)
        if resp.status_code != 200:
            stats["http_errors"] += 1
            stats["last_error"] = f"{resp.status_code}: {resp.text[:200]}"
            return
        result = resp.json()
        stats[result["status"]] += 1
        if result["status"] == "approved":
            maybe_refund(payment, result, rules, settings)
    except httpx.HTTPError as e:
        stats["http_errors"] += 1
        stats["last_error"] = repr(e)
    finally:
        stats["in_flight"] -= 1


async def generate_traffic():
    rng = np.random.default_rng()
    while True:
        try:
            settings = await app.state.settings.get()
            if not settings.get("running") or settings.get("tps", 0) <= 0:
                await asyncio.sleep(0.5)
                continue

            rules = await app.state.chaos.get()
            for _ in range(rng.poisson(settings["tps"] * TICK)):
                if app.state.stats["in_flight"] >= MAX_IN_FLIGHT:
                    app.state.stats["dropped"] += 1
                    continue
                start_task(send_payment(rules, settings))
        except Exception as e:
            # don't let the loop die, e.g. while the DB restarts
            app.state.stats["last_error"] = repr(e)
            await asyncio.sleep(1)
        await asyncio.sleep(TICK)


@app.post("/control")
async def control(body: Control):
    patch = body.model_dump(exclude_none=True)
    value = await patch_settings(app.state.pool, "traffic", patch)
    app.state.settings._loaded = 0
    return value


@app.post("/burst")
async def burst(body: Burst):
    """Send N payments right away, in addition to the normal traffic."""
    rules = await app.state.chaos.get()
    settings = await app.state.settings.get()

    async def feed():
        for _ in range(body.count):
            while len(app.state.tasks) >= MAX_IN_FLIGHT:
                await asyncio.sleep(0.05)
            start_task(send_payment(rules, settings))

    start_task(feed())
    return {"queued": body.count}


@app.get("/stats")
async def stats():
    result = dict(app.state.stats)
    result["uptime_s"] = round(time.time() - result.pop("started"))
    result["settings"] = await app.state.settings.get()
    return result


@app.post("/stats/reset")
async def reset_stats():
    app.state.stats = new_stats()
    return {"ok": True}


@app.get("/health")
async def health():
    settings = await app.state.settings.get()
    return {
        "service": "traffic",
        "status": "ok",
        "running": settings.get("running"),
        "tps": settings.get("tps"),
    }
