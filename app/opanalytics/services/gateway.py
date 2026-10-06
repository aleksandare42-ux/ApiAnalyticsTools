"""Payment gateway - the API merchants talk to.

POST /v1/payments                  pick a provider, retry soft declines
                                   on another one, save every attempt
POST /v1/payments/{id}/refund
GET  /v1/payments/{id}
GET  /v1/routing                   current weights and provider health
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .. import reference, routing
from ..db import SettingsCache, create_pool, init_db

PROVIDERS_URL = os.environ.get("PROVIDERS_URL", "http://127.0.0.1:8001")

INSERT_ATTEMPT = """
    INSERT INTO transactions (
        transaction_id, payment_id, attempt_no, merchant_id, created_at,
        country, currency, amount, amount_usd, payment_method, provider,
        status, decline_reason, processing_time_ms, source
    ) VALUES (
        %(transaction_id)s, %(payment_id)s, %(attempt_no)s, %(merchant_id)s,
        %(created_at)s, %(country)s, %(currency)s, %(amount)s,
        %(amount_usd)s, %(payment_method)s, %(provider)s, %(status)s,
        %(decline_reason)s, %(processing_time_ms)s, %(source)s
    )
"""

REFUND = """
    UPDATE transactions
    SET refunded = true, refunded_at = now()
    WHERE transaction_id = %s AND status = 'approved' AND NOT refunded
    RETURNING transaction_id, amount, currency, refunded_at
"""


class PaymentRequest(BaseModel):
    merchant_id: str = Field("m_001", description="See GET /v1/merchants")
    amount: float = Field(19.99, gt=0)
    currency: str | None = Field(
        None, description="Defaults to the country's currency"
    )
    country: str = "US"
    payment_method: str = "card"
    force_provider: str | None = Field(
        None, description="Skip routing, for testing"
    )
    source: str = "live"


class ProvidersCache:
    def __init__(self, pool, ttl=2.0):
        self.pool = pool
        self.ttl = ttl
        self.rows = []
        self._loaded = 0.0

    async def get(self):
        if time.monotonic() - self._loaded > self.ttl:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT * FROM providers ORDER BY provider_id"
                )
                self.rows = await cur.fetchall()
            self._loaded = time.monotonic()
        return self.rows


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await create_pool(max_size=20)
    await init_db(app.state.pool)
    # Windows' select() can't handle more than 512 sockets, so keep the
    # pool size reasonable.
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=200)
    app.state.http = httpx.AsyncClient(base_url=PROVIDERS_URL, limits=limits)
    app.state.providers = ProvidersCache(app.state.pool)
    app.state.settings = SettingsCache(app.state.pool, "gateway")
    app.state.health = routing.HealthTracker()
    app.state.counters = {
        "payments": 0,
        "attempts": 0,
        "cascaded": 0,
        "approved": 0,
    }
    yield
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="Payment gateway", lifespan=lifespan)


async def call_provider(provider, body, timeout):
    """Returns (status, decline_reason)."""
    try:
        resp = await app.state.http.post(
            f"/{provider}/authorize", json=body, timeout=timeout
        )
    except httpx.TimeoutException:
        return "error", "provider_timeout"
    except httpx.HTTPError:
        return "error", "provider_unavailable"

    if resp.status_code == 504:
        return "error", "provider_timeout"
    if resp.status_code >= 500:
        return "error", "provider_unavailable"
    if resp.status_code >= 400:
        return "declined", "card_not_supported"
    data = resp.json()
    return data["status"], data.get("decline_reason")


def validate(req):
    if req.merchant_id not in reference.MERCHANT_BY_ID:
        raise HTTPException(404, f"unknown merchant {req.merchant_id}")
    if req.country not in reference.COUNTRIES:
        raise HTTPException(422, f"unsupported country {req.country}")
    if req.payment_method not in reference.METHODS:
        raise HTTPException(
            422, f"unsupported payment_method {req.payment_method}"
        )
    currency = req.currency or reference.COUNTRIES[req.country][0]
    currency = currency.upper()
    if currency not in reference.FX:
        raise HTTPException(422, f"unsupported currency {currency}")
    return currency


@app.post("/v1/payments")
async def create_payment(req: PaymentRequest):
    currency = validate(req)
    settings = await app.state.settings.get()

    if req.force_provider:
        route = [req.force_provider]
    else:
        providers = await app.state.providers.get()
        weights = routing.effective_weights(
            providers,
            req.payment_method,
            app.state.health,
            smart=settings.get("smart_routing", False),
        )
        route = routing.rank(weights)
    if not route:
        raise HTTPException(
            422, f"no enabled provider supports {req.payment_method}"
        )

    max_attempts = 2 if settings.get("cascade", True) else 1
    timeout = settings.get("timeout_ms", 2500) / 1000

    payment_id = uuid.uuid4()
    amount_usd = round(req.amount / reference.FX[currency], 2)
    body = {
        "merchant_id": req.merchant_id,
        "amount": req.amount,
        "amount_usd": amount_usd,
        "currency": currency,
        "country": req.country,
        "payment_method": req.payment_method,
    }

    attempts = []
    for attempt_no, provider in enumerate(route[:max_attempts], start=1):
        created_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        status, reason = await call_provider(provider, body, timeout)
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        app.state.health.record(provider, status == "approved")

        transaction_id = uuid.uuid4()
        async with app.state.pool.connection() as conn:
            await conn.execute(
                INSERT_ATTEMPT,
                {
                    "transaction_id": transaction_id,
                    "payment_id": payment_id,
                    "attempt_no": attempt_no,
                    "merchant_id": req.merchant_id,
                    "created_at": created_at,
                    "country": req.country,
                    "currency": currency,
                    "amount": req.amount,
                    "amount_usd": amount_usd,
                    "payment_method": req.payment_method,
                    "provider": provider,
                    "status": status,
                    "decline_reason": reason,
                    "processing_time_ms": elapsed_ms,
                    "source": req.source,
                },
            )

        attempts.append(
            {
                "transaction_id": str(transaction_id),
                "attempt_no": attempt_no,
                "provider": provider,
                "status": status,
                "decline_reason": reason,
                "processing_time_ms": elapsed_ms,
            }
        )
        # only soft declines are worth retrying on another provider
        if status == "approved" or reason not in reference.SOFT_DECLINES:
            break

    last = attempts[-1]
    counters = app.state.counters
    counters["payments"] += 1
    counters["attempts"] += len(attempts)
    if len(attempts) > 1:
        counters["cascaded"] += 1
    if last["status"] == "approved":
        counters["approved"] += 1

    return {
        "payment_id": str(payment_id),
        "status": last["status"],
        "provider": last["provider"],
        "decline_reason": last["decline_reason"],
        "transaction_id": last["transaction_id"],
        "amount": req.amount,
        "currency": currency,
        "attempts": attempts,
    }


@app.post("/v1/payments/{transaction_id}/refund")
async def refund(transaction_id: uuid.UUID):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(REFUND, (transaction_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(
            409, "transaction not found, not approved or already refunded"
        )
    return row


@app.get("/v1/payments/{payment_id}")
async def get_payment(payment_id: uuid.UUID):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(
            """
            SELECT * FROM transactions
            WHERE payment_id = %s OR transaction_id = %s
            ORDER BY attempt_no
            """,
            (payment_id, payment_id),
        )
        rows = await cur.fetchall()
    if not rows:
        raise HTTPException(404, "payment not found")
    return {"payment_id": rows[0]["payment_id"], "attempts": rows}


@app.get("/v1/merchants")
async def merchants():
    return reference.MERCHANTS


@app.get("/v1/routing")
async def routing_state():
    settings = await app.state.settings.get()
    providers = await app.state.providers.get()
    smart = settings.get("smart_routing", False)
    weights = {}
    for method in reference.METHOD_CODES:
        weights[method] = routing.effective_weights(
            providers, method, app.state.health, smart
        )
    return {
        "settings": settings,
        "health": app.state.health.snapshot(),
        "weights_by_method": weights,
        "counters": app.state.counters,
    }


@app.get("/health")
async def health():
    return {
        "service": "gateway",
        "status": "ok",
        "counters": app.state.counters,
    }
