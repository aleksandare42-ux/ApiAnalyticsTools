"""Payment provider simulator.

Four fake acquirers behind one service. POST /{provider_id}/authorize
returns approved or declined with a reason, or fails with a timeout / 5xx.
The behaviour comes from reference.py plus the active chaos rules.
"""

import asyncio
import random
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import chaos, reference
from ..db import create_pool, init_db

# How long a "timed out" provider hangs. Must be longer than the gateway
# timeout.
HANG_SECONDS = 6.0


class AuthRequest(BaseModel):
    merchant_id: str
    amount: float
    amount_usd: float
    currency: str
    country: str
    payment_method: str = "card"


class ProviderConfig:
    """Provider settings from the DB, reloaded every few seconds."""

    def __init__(self, pool, ttl=3.0):
        self.pool = pool
        self.ttl = ttl
        self.by_id = {}
        self._loaded = 0.0

    async def get(self):
        if time.monotonic() - self._loaded > self.ttl:
            async with self.pool.connection() as conn:
                cur = await conn.execute("SELECT * FROM providers")
                rows = await cur.fetchall()
            self.by_id = {row["provider_id"]: row for row in rows}
            self._loaded = time.monotonic()
        return self.by_id


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await create_pool()
    await init_db(app.state.pool)
    app.state.chaos = chaos.ChaosCache(app.state.pool)
    app.state.config = ProviderConfig(app.state.pool)
    app.state.stats = {}
    yield
    await app.state.pool.close()


app = FastAPI(title="PSP simulator", lifespan=lifespan)


def count(provider, outcome):
    if provider not in app.state.stats:
        app.state.stats[provider] = Counter()
    app.state.stats[provider][outcome] += 1


def approval_probability(cfg, req):
    merchant = reference.MERCHANT_BY_ID.get(req.merchant_id)
    if merchant:
        risk = reference.RISK_FACTOR[merchant["risk_tier"]]
    else:
        risk = 0.95
    country_factor = reference.COUNTRIES.get(req.country, ("", 0, 0.9))[2]
    method_factor = reference.METHODS[req.payment_method][1]
    p = reference.approval_prob(
        cfg["base_approval"],
        country_factor,
        method_factor,
        risk,
        req.amount_usd,
    )
    return float(p)


@app.post("/{provider_id}/authorize")
async def authorize(provider_id: str, req: AuthRequest):
    providers = await app.state.config.get()
    cfg = providers.get(provider_id)
    if cfg is None:
        raise HTTPException(404, f"unknown provider {provider_id}")
    if req.payment_method not in cfg["methods"]:
        raise HTTPException(
            422, f"{provider_id} does not support {req.payment_method}"
        )

    ctx = {
        "provider": provider_id,
        "country": req.country,
        "merchant": req.merchant_id,
        "payment_method": req.payment_method,
    }
    effects = chaos.aggregate(await app.state.chaos.get(), ctx)
    latency = reference.sample_latency_ms(
        cfg["base_latency_ms"], req.payment_method
    )
    latency += effects.latency_ms

    # technical problems: half of them hang, half fail fast
    if random.random() < reference.BASE_ERROR_RATE + effects.error_rate:
        if random.random() < 0.5:
            count(provider_id, "timeout")
            await asyncio.sleep(HANG_SECONDS)
            return JSONResponse({"error": "late response"}, status_code=504)
        count(provider_id, "unavailable")
        await asyncio.sleep(latency * 0.2 / 1000)
        return JSONResponse({"error": "provider_unavailable"}, status_code=503)

    p_base = approval_probability(cfg, req)
    approved = random.random() < max(0.0, p_base - effects.decline_rate)
    reason = None
    if not approved:
        # was this decline caused by the chaos rule or is it a normal one?
        extra = effects.decline_rate
        chaos_share = extra / max(1e-9, (1 - p_base) + extra)
        if extra and random.random() < chaos_share:
            reason = effects.decline_reason or "do_not_honor"
        else:
            reason = reference.sample_decline_reason()

    await asyncio.sleep(latency / 1000)
    status = "approved" if approved else "declined"
    count(provider_id, status)
    return {
        "status": status,
        "decline_reason": reason,
        "provider_reference": str(uuid.uuid4()),
        "simulated_latency_ms": round(latency),
    }


@app.get("/providers")
async def providers_state():
    providers = await app.state.config.get()
    rules = await app.state.chaos.get()
    provider_rules = [
        r for r in rules if r.target_type in ("provider", "global")
    ]
    result = []
    for provider_id, cfg in providers.items():
        effects = chaos.aggregate(provider_rules, {"provider": provider_id})
        result.append(
            {
                **cfg,
                "chaos": effects.__dict__,
                "counters": app.state.stats.get(provider_id, {}),
            }
        )
    return result


@app.get("/health")
async def health():
    providers = await app.state.config.get()
    return {
        "service": "providers",
        "status": "ok",
        "providers": list(providers),
    }
