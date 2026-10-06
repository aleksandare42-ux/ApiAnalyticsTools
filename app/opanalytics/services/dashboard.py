"""Dashboard: analytics API, control endpoints and the web UI."""

import asyncio
import os
import re
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import bi, chaos, detection, reference, seed
from ..db import DATABASE_URL, create_pool, init_db, patch_settings

URLS = {
    "gateway": os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000"),
    "providers": os.environ.get("PROVIDERS_URL", "http://127.0.0.1:8001"),
    "traffic": os.environ.get("TRAFFIC_URL", "http://127.0.0.1:8002"),
    "detector": os.environ.get("DETECTOR_URL", "http://127.0.0.1:8003"),
    "adminer": os.environ.get("ADMINER_URL", "http://adminer:8080"),
}
PORTS = {
    "gateway": 8000,
    "providers": 8001,
    "traffic": 8002,
    "detector": 8003,
    "dashboard": 8080,
    "adminer": 8081,
    "metabase": 3000,
    "postgres": 5432,
}
SQL_DIR = Path(os.environ.get("SQL_DIR", "/srv/sql"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# window: (minutes, chart bucket)
WINDOWS = {
    "15m": (15, "30 seconds"),
    "1h": (60, "1 minute"),
    "6h": (360, "5 minutes"),
    "24h": (1440, "15 minutes"),
    "7d": (10080, "2 hours"),
    "30d": (43200, "6 hours"),
    "90d": (129600, "1 day"),
}

# what can be used in "group by" from the UI
GROUP_COLUMNS = {
    "provider": "provider",
    "country": "country",
    "merchant": "merchant_id",
    "payment_method": "payment_method",
    "currency": "currency",
    "decline_reason": "decline_reason",
}


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await create_pool(max_size=15)
    await init_db(app.state.pool)
    app.state.http = httpx.AsyncClient(timeout=30)
    app.state.jobs = {}
    yield
    await app.state.http.aclose()
    await app.state.pool.close()


app = FastAPI(title="Payment Ops dashboard", lifespan=lifespan)


def time_filter(window, source, prefix="", previous=False):
    """Build the WHERE part for a time window and a data source.

    With previous=True the window right before the current one is used,
    to compare KPIs with the previous period.
    """
    if window not in WINDOWS:
        raise HTTPException(422, f"window must be one of {list(WINDOWS)}")
    minutes = WINDOWS[window][0]
    params = {
        "from_min": minutes * 2 if previous else minutes,
        "to_min": minutes if previous else 0,
    }
    where = (
        f"{prefix}created_at >= now() - interval '1 minute' * %(from_min)s"
        f" AND {prefix}created_at < now() - interval '1 minute' * %(to_min)s"
    )
    if source in ("live", "seed"):
        where += f" AND {prefix}source = %(source)s"
        params["source"] = source
    return where, params


def group_column(dim):
    if dim not in GROUP_COLUMNS:
        raise HTTPException(422, f"unknown dimension {dim}")
    return GROUP_COLUMNS[dim]


async def fetch(sql, params=None):
    async with app.state.pool.connection() as conn:
        cur = await conn.execute(sql, params)
        if cur.description is None:
            return []
        return await cur.fetchall()


async def call_service(service, method, path, json=None, timeout=30):
    """Forward a request to one of the other services."""
    try:
        resp = await app.state.http.request(
            method, URLS[service] + path, json=json, timeout=timeout
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"{service} is not reachable: {e!r}")
    if resp.status_code >= 400:
        raise HTTPException(resp.status_code, resp.text)
    return resp.json()


# Analytics

KPI_SQL = """
SELECT count(*) AS tx,
       count(DISTINCT payment_id) AS payments,
       count(DISTINCT payment_id) FILTER (WHERE status = 'approved')
           AS paid,
       count(*) FILTER (WHERE status = 'approved') AS approved,
       count(*) FILTER (WHERE status = 'declined') AS declined,
       count(*) FILTER (WHERE status = 'error') AS errors,
       count(*) FILTER (WHERE refunded) AS refunds,
       coalesce(sum(amount_usd) FILTER (WHERE status = 'approved'), 0)
           AS volume_usd,
       avg(processing_time_ms) FILTER (WHERE status <> 'error') AS avg_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY processing_time_ms)
           AS p95_ms
FROM transactions
WHERE {where}
"""

TIMESERIES_SQL = """
SELECT date_bin(%(bucket)s::interval, created_at, '2000-01-01') AS t,
       {column} AS k,
       count(*) AS n,
       count(*) FILTER (WHERE status = 'approved') AS ok,
       count(*) FILTER (WHERE status = 'declined') AS declined,
       count(*) FILTER (WHERE status = 'error') AS err,
       round(avg(processing_time_ms) FILTER (WHERE status <> 'error'))
           AS avg_ms,
       coalesce(sum(amount_usd) FILTER (WHERE status = 'approved'), 0)
           AS volume_usd
FROM transactions
WHERE {where}
GROUP BY 1, 2
ORDER BY 1
"""

BREAKDOWN_SQL = """
SELECT t.{column} AS key,
       count(*) AS tx,
       avg((status = 'approved')::int) AS approval_rate,
       avg((status = 'declined')::int) AS decline_rate,
       avg((status = 'error')::int) AS error_rate,
       count(*) FILTER (WHERE refunded)::float
           / nullif(count(*) FILTER (WHERE status = 'approved'), 0)
           AS refund_rate,
       round(avg(processing_time_ms) FILTER (WHERE status <> 'error'))
           AS avg_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY processing_time_ms)
           AS p95_ms,
       coalesce(sum(amount_usd) FILTER (WHERE status = 'approved'), 0)
           AS volume_usd,
       count(*)::float / sum(count(*)) OVER () AS share
FROM transactions t
WHERE {where}
GROUP BY 1
ORDER BY tx DESC
"""

HEATMAP_SQL = """
SELECT {rows} AS r, {cols} AS c, count(*) AS n,
       avg((status = 'approved')::int) AS approval_rate
FROM transactions
WHERE {where}
GROUP BY 1, 2
"""

DECLINE_REASONS_SQL = """
SELECT decline_reason AS reason,
       count(*) AS n,
       count(*)::float / sum(count(*)) OVER () AS share
FROM transactions
WHERE {where} AND status <> 'approved'
GROUP BY 1
ORDER BY n DESC
"""

FEED_SQL = """
SELECT transaction_id, payment_id, attempt_no, created_at, merchant_id,
       country, currency, amount, amount_usd, payment_method, provider,
       status, decline_reason, processing_time_ms, refunded, source
FROM transactions
WHERE {where}
ORDER BY created_at DESC
LIMIT %(limit)s
"""


def divide(a, b):
    return a / b if b else None


def to_float(value):
    return float(value) if value is not None else None


def kpi_values(row):
    tx = row["tx"] or 0
    approved = row["approved"] or 0
    return {
        "tx": tx,
        "payments": row["payments"],
        "volume_usd": float(row["volume_usd"] or 0),
        "approval_rate": divide(approved, tx),
        "conversion_rate": divide(row["paid"], row["payments"]),
        "decline_rate": divide(row["declined"], tx),
        "error_rate": divide(row["errors"], tx),
        "refund_rate": divide(row["refunds"], approved),
        "avg_ms": to_float(row["avg_ms"]),
        "p95_ms": to_float(row["p95_ms"]),
    }


@app.get("/api/kpis")
async def kpis(window: str = "1h", source: str = "all"):
    where, params = time_filter(window, source)
    current = await fetch(KPI_SQL.format(where=where), params)

    where, params = time_filter(window, source, previous=True)
    previous = await fetch(KPI_SQL.format(where=where), params)

    open_count = await fetch(
        "SELECT count(*) AS n FROM anomalies WHERE status = 'open'"
    )
    return {
        "current": kpi_values(current[0]),
        "previous": kpi_values(previous[0]),
        "open_anomalies": open_count[0]["n"],
        "window": window,
        "source": source,
    }


@app.get("/api/timeseries")
async def timeseries(
    window: str = "1h", source: str = "all", dim: str = "provider"
):
    column = group_column(dim)
    where, params = time_filter(window, source)
    params["bucket"] = WINDOWS[window][1]
    sql = TIMESERIES_SQL.format(column=column, where=where)
    rows = await fetch(sql, params)
    return {"bucket": params["bucket"], "rows": rows}


@app.get("/api/breakdown")
async def breakdown(
    dim: str = "provider", window: str = "1h", source: str = "all"
):
    column = group_column(dim)
    where, params = time_filter(window, source, prefix="t.")
    rows = await fetch(
        BREAKDOWN_SQL.format(column=column, where=where), params
    )
    if dim == "merchant":
        for row in rows:
            merchant = reference.MERCHANT_BY_ID.get(row["key"])
            if merchant:
                row["label"] = f"{merchant['name']} ({row['key']})"
                row["vertical"] = merchant["vertical"]
            else:
                row["label"] = row["key"]
                row["vertical"] = None
    return rows


@app.get("/api/heatmap")
async def heatmap(
    window: str = "1h",
    source: str = "all",
    rows: str = "country",
    cols: str = "provider",
):
    where, params = time_filter(window, source)
    sql = HEATMAP_SQL.format(
        rows=group_column(rows), cols=group_column(cols), where=where
    )
    return await fetch(sql, params)


@app.get("/api/decline_reasons")
async def decline_reasons(window: str = "1h", source: str = "all"):
    where, params = time_filter(window, source)
    return await fetch(DECLINE_REASONS_SQL.format(where=where), params)


@app.get("/api/feed")
async def feed(
    limit: int = Query(50, le=500),
    status: str = None,
    merchant: str = None,
    provider: str = None,
):
    conditions = ["true"]
    params = {"limit": limit}
    filters = {
        "status": status,
        "merchant_id": merchant,
        "provider": provider,
    }
    for column, value in filters.items():
        if value:
            conditions.append(f"{column} = %({column})s")
            params[column] = value
    sql = FEED_SQL.format(where=" AND ".join(conditions))
    return await fetch(sql, params)


@app.get("/api/reference")
async def reference_data():
    return {
        "merchants": reference.MERCHANTS,
        "countries": reference.COUNTRIES,
        "methods": reference.METHOD_CODES,
        "decline_reasons": reference.DECLINE_CODES,
        "chaos_targets": chaos.TARGET_TYPES,
        "chaos_effects": chaos.EFFECTS,
        "windows": list(WINDOWS),
        "ports": PORTS,
    }


# Anomalies


@app.get("/api/anomalies")
async def anomalies(
    status: str = "all",
    detected_by: str = "all",
    limit: int = Query(200, le=2000),
):
    conditions = ["true"]
    params = {"limit": limit}
    if status != "all":
        conditions.append("status = %(status)s")
        params["status"] = status
    if detected_by != "all":
        conditions.append("detected_by = %(detected_by)s")
        params["detected_by"] = detected_by
    sql = f"""
        SELECT * FROM anomalies
        WHERE {" AND ".join(conditions)}
        ORDER BY (status = 'open') DESC, last_seen DESC
        LIMIT %(limit)s
    """
    return await fetch(sql, params)


@app.post("/api/anomalies/{anomaly_id}/resolve")
async def resolve_anomaly(anomaly_id: int):
    await fetch(
        """
        UPDATE anomalies SET status = 'resolved', resolved_at = now()
        WHERE anomaly_id = %s
        """,
        (anomaly_id,),
    )
    return {"ok": True}


@app.delete("/api/anomalies")
async def clear_anomalies(detected_by: str = "live"):
    if detected_by == "all":
        await fetch("DELETE FROM anomalies")
    else:
        await fetch(
            "DELETE FROM anomalies WHERE detected_by = %s", (detected_by,)
        )
    return {"ok": True}


@app.get("/api/evaluation")
async def evaluation():
    truth = await fetch("SELECT * FROM seed_ground_truth ORDER BY start_at")
    found = await fetch(
        "SELECT * FROM anomalies WHERE detected_by = 'batch' "
        "ORDER BY window_start"
    )
    return detection.match_ground_truth(truth, found)


@app.post("/api/detector/run")
async def detector_run():
    return await call_service("detector", "POST", "/run")


@app.post("/api/detector/scan")
async def detector_scan():
    return await call_service("detector", "POST", "/scan-history", timeout=300)


@app.get("/api/detector/status")
async def detector_status():
    return await call_service("detector", "GET", "/status")


# Chaos rules


class ChaosRuleIn(BaseModel):
    name: str | None = None
    target_type: str = "provider"
    target_value: str | None = "alphapay"
    effect: str = "decline_rate"
    value: float = 0.3
    reason: str | None = None
    minutes: float | None = Field(
        15, description="Expire after N minutes, null means never"
    )


async def add_rule(rule):
    if rule.target_type not in chaos.TARGET_TYPES:
        raise HTTPException(422, f"bad target_type {rule.target_type}")
    if rule.effect not in chaos.EFFECTS:
        raise HTTPException(422, f"bad effect {rule.effect}")

    expires_at = None
    if rule.minutes:
        expires_at = datetime.now(timezone.utc) + timedelta(
            minutes=rule.minutes
        )
    target_value = rule.target_value
    if rule.target_type == "global":
        target_value = None
    name = rule.name or (
        f"{rule.effect} {rule.value:g} on {rule.target_type}={target_value}"
    )

    rows = await fetch(
        """
        INSERT INTO chaos_rules
            (name, target_type, target_value, effect, value, reason,
             expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING *
        """,
        (
            name,
            rule.target_type,
            target_value,
            rule.effect,
            rule.value,
            rule.reason,
            expires_at,
        ),
    )
    return rows[0]


@app.get("/api/chaos")
async def list_chaos_rules():
    return await fetch("""
        SELECT *, active AND (expires_at IS NULL OR expires_at > now())
                  AS live
        FROM chaos_rules
        ORDER BY live DESC, created_at DESC
        LIMIT 100
        """)


@app.post("/api/chaos")
async def create_chaos_rule(rule: ChaosRuleIn):
    return await add_rule(rule)


@app.get("/api/chaos/presets")
async def chaos_presets():
    return chaos.PRESETS


@app.post("/api/chaos/presets/{key}")
async def inject_preset(key: str, minutes: float | None = 15):
    if key not in chaos.PRESETS:
        raise HTTPException(
            404, f"unknown preset, available: {list(chaos.PRESETS)}"
        )
    preset = dict(chaos.PRESETS[key])
    del preset["description"]
    return await add_rule(ChaosRuleIn(**preset, minutes=minutes))


@app.delete("/api/chaos/{rule_id}")
async def stop_chaos_rule(rule_id: int):
    await fetch(
        "UPDATE chaos_rules SET active = false WHERE id = %s", (rule_id,)
    )
    return {"ok": True}


@app.delete("/api/chaos")
async def stop_all_chaos(purge: bool = False):
    if purge:
        await fetch("DELETE FROM chaos_rules")
    else:
        await fetch("UPDATE chaos_rules SET active = false WHERE active")
    return {"ok": True}


# Providers, settings, traffic


class ProviderPatch(BaseModel):
    enabled: bool | None = None
    weight: int | None = Field(None, ge=0, le=1000)
    base_approval: float | None = Field(None, ge=0, le=1)
    base_latency_ms: int | None = Field(None, ge=10, le=20000)


@app.get("/api/providers")
async def providers():
    rows = await fetch("SELECT * FROM providers ORDER BY provider_id")
    try:
        routing = await call_service(
            "gateway", "GET", "/v1/routing", timeout=3
        )
    except HTTPException:
        routing = None
    return {"providers": rows, "routing": routing}


@app.patch("/api/providers/{provider_id}")
async def update_provider(provider_id: str, body: ProviderPatch):
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(422, "nothing to change")
    # keys come from the pydantic model, so it's safe to put them in SQL
    assignments = ", ".join(f"{key} = %({key})s" for key in changes)
    rows = await fetch(
        f"UPDATE providers SET {assignments} "
        "WHERE provider_id = %(provider_id)s RETURNING *",
        {**changes, "provider_id": provider_id},
    )
    if not rows:
        raise HTTPException(404, "unknown provider")
    return rows[0]


@app.post("/api/providers/reset")
async def reset_providers():
    for provider in reference.PROVIDERS:
        await fetch(
            """
            UPDATE providers
            SET enabled = true,
                weight = %(weight)s,
                base_approval = %(base_approval)s,
                base_latency_ms = %(base_latency_ms)s
            WHERE provider_id = %(provider_id)s
            """,
            provider,
        )
    return {"ok": True}


@app.get("/api/settings")
async def all_settings():
    rows = await fetch("SELECT key, value FROM settings")
    return {row["key"]: row["value"] for row in rows}


@app.patch("/api/settings/{key}")
async def update_settings(key: str, patch: dict):
    if key not in ("traffic", "gateway", "detector"):
        raise HTTPException(404, "unknown settings key")
    return await patch_settings(app.state.pool, key, patch)


@app.get("/api/traffic/stats")
async def traffic_stats():
    return await call_service("traffic", "GET", "/stats", timeout=3)


@app.post("/api/traffic/burst")
async def traffic_burst(count: int = 200):
    return await call_service(
        "traffic", "POST", "/burst", json={"count": count}
    )


@app.post("/api/traffic/reset-stats")
async def traffic_reset_stats():
    return await call_service("traffic", "POST", "/stats/reset")


class TestPayment(BaseModel):
    merchant_id: str = "m_001"
    amount: float = 19.99
    currency: str | None = None
    country: str = "US"
    payment_method: str = "card"
    force_provider: str | None = None


@app.post("/api/test-payment")
async def test_payment(payment: TestPayment):
    body = payment.model_dump(exclude_none=True)
    body["source"] = "live"
    return await call_service("gateway", "POST", "/v1/payments", json=body)


@app.post("/api/test-refund/{transaction_id}")
async def test_refund(transaction_id: str):
    path = f"/v1/payments/{transaction_id}/refund"
    return await call_service("gateway", "POST", path)


async def check_service(name, url):
    started = time.perf_counter()
    try:
        resp = await app.state.http.get(url, timeout=3)
        ok = resp.status_code < 500
        info = None
        if "json" in resp.headers.get("content-type", ""):
            info = resp.json()
    except Exception as e:
        ok = False
        info = repr(e)
    return {
        "name": name,
        "ok": ok,
        "ms": round((time.perf_counter() - started) * 1000),
        "info": info,
        "port": PORTS.get(name),
    }


DB_INFO_SQL = """
SELECT pg_size_pretty(pg_database_size(current_database())) AS size,
       (SELECT count(*) FROM transactions) AS tx,
       (SELECT count(*) FROM transactions WHERE source = 'live') AS live_tx,
       (SELECT min(created_at) FROM transactions) AS first_tx,
       (SELECT max(created_at) FROM transactions) AS last_tx
"""


@app.get("/api/services")
async def services():
    checks = [
        check_service(name, URLS[name] + "/health")
        for name in ("gateway", "providers", "traffic", "detector")
    ]
    checks.append(check_service("adminer", URLS["adminer"]))
    results = list(await asyncio.gather(*checks))

    started = time.perf_counter()
    try:
        info = (await fetch(DB_INFO_SQL))[0]
        postgres = {"ok": True, "info": info}
    except Exception as e:
        postgres = {"ok": False, "info": repr(e)}
    postgres["name"] = "postgres"
    postgres["port"] = PORTS["postgres"]
    postgres["ms"] = round((time.perf_counter() - started) * 1000)

    dashboard = {
        "name": "dashboard",
        "ok": True,
        "ms": 0,
        "info": None,
        "port": PORTS["dashboard"],
    }
    return [postgres] + results + [dashboard]


# Dataset generation and reset


class SeedRequest(BaseModel):
    rows: int = Field(500_000, ge=1000, le=5_000_000)
    days: int = Field(30, ge=2, le=365)
    inject_anomalies: bool = True
    replace: bool = True
    scan_after: bool = True
    seed: int = 42


def run_seed_job(job, req):
    # runs in a separate thread, the UI polls the job dict for progress
    def progress(pct, message):
        job["progress"] = pct
        job["message"] = message

    try:
        progress(0.01, f"generating {req.rows:,} rows over {req.days} days")
        started = time.perf_counter()
        df, truth = seed.generate(
            req.rows, req.days, inject=req.inject_anomalies, seed=req.seed
        )
        took = time.perf_counter() - started
        progress(0.04, f"generated {len(df):,} rows in {took:.1f}s")

        seed.write(
            df, truth, DATABASE_URL, replace=req.replace, progress=progress
        )

        if req.scan_after:
            job["message"] = "running historical anomaly scan"
            with httpx.Client(timeout=600) as client:
                resp = client.post(URLS["detector"] + "/scan-history")
                resp.raise_for_status()

        job["status"] = "done"
        job["progress"] = 1.0
        job["message"] = (
            f"{len(df):,} rows loaded, {len(truth)} anomalies injected"
        )
    except Exception as e:
        job["status"] = "failed"
        job["message"] = repr(e)
    job["finished"] = time.time()


@app.post("/api/seed")
async def start_seed(req: SeedRequest):
    for job in app.state.jobs.values():
        if job["status"] == "running":
            raise HTTPException(409, "a job is already running")

    job = {
        "id": uuid.uuid4().hex[:8],
        "kind": "seed",
        "status": "running",
        "progress": 0.0,
        "message": "queued",
        "started": time.time(),
        "params": req.model_dump(),
    }
    app.state.jobs[job["id"]] = job
    thread = threading.Thread(
        target=run_seed_job, args=(job, req), daemon=True
    )
    thread.start()
    return job


@app.get("/api/jobs")
async def list_jobs():
    jobs = list(app.state.jobs.values())
    jobs.sort(key=lambda j: j["started"], reverse=True)
    return jobs


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    if job_id not in app.state.jobs:
        raise HTTPException(404, "no such job")
    return app.state.jobs[job_id]


RESET_QUERIES = {
    "live": [
        "DELETE FROM transactions WHERE source = 'live'",
        "DELETE FROM anomalies WHERE detected_by = 'live'",
    ],
    "seed": [
        "DELETE FROM transactions WHERE source = 'seed'",
        "DELETE FROM seed_ground_truth",
        "DELETE FROM anomalies WHERE detected_by = 'batch'",
    ],
    "anomalies": [
        "TRUNCATE anomalies RESTART IDENTITY",
    ],
    "all": [
        "TRUNCATE transactions, anomalies, seed_ground_truth, chaos_rules "
        "RESTART IDENTITY",
    ],
}


@app.post("/api/reset")
async def reset(scope: str = "live"):
    if scope not in RESET_QUERIES:
        raise HTTPException(422, f"scope must be one of {list(RESET_QUERIES)}")
    for sql in RESET_QUERIES[scope]:
        await fetch(sql)
    return {"ok": True, "scope": scope}


# CSV export and SQL lab

# name: (table or view, order by). Only transactions are filtered by time.
EXPORTS = {
    "transactions": ("v_transactions", "created_at"),
    "anomalies": ("anomalies", "first_seen"),
    "hourly_provider": ("v_hourly_provider", "hour, provider"),
    "daily_kpis": ("v_daily_kpis", "day"),
    "daily_merchant": ("v_daily_merchant", "day, merchant_id"),
    "country_provider": ("v_country_provider", "country, provider"),
    "ground_truth": ("seed_ground_truth", "start_at"),
}


@app.get("/api/export/{name}.csv")
async def export_csv(name: str, window: str = "30d", source: str = "all"):
    if name not in EXPORTS:
        raise HTTPException(404, f"available exports: {list(EXPORTS)}")

    where, params = time_filter(window, source)
    table, order_by = EXPORTS[name]
    if name != "transactions":
        where = "true"
    sql = f"SELECT * FROM {table} WHERE {where} ORDER BY {order_by}"
    # COPY doesn't support bind parameters. The values were already
    # validated by time_filter (ints and a fixed set of sources).
    sql = sql.replace("%(from_min)s", str(int(params["from_min"])))
    sql = sql.replace("%(to_min)s", str(int(params["to_min"])))
    if "source" in params:
        sql = sql.replace("%(source)s", f"'{params['source']}'")
    copy_sql = f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER)"

    async def stream():
        conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
        async with conn:
            async with conn.cursor().copy(copy_sql) as copy:
                async for chunk in copy:
                    yield bytes(chunk)

    filename = f"{name}_{datetime.now():%Y%m%d_%H%M}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(stream(), media_type="text/csv", headers=headers)


class SqlRequest(BaseModel):
    query: str
    limit: int = Field(1000, le=10000)


@app.post("/api/sql")
async def run_sql(req: SqlRequest):
    """Run a query from the SQL lab. Read-only, with a timeout."""
    started = time.perf_counter()
    try:
        conn = await psycopg.AsyncConnection.connect(DATABASE_URL)
        async with conn:
            await conn.set_read_only(True)
            async with conn.transaction():
                await conn.execute("SET LOCAL statement_timeout = '20s'")
                cur = await conn.execute(req.query)
                if cur.description is None:
                    return {"columns": [], "rows": [], "ms": 0}
                columns = [col.name for col in cur.description]
                rows = await cur.fetchmany(req.limit)
    except psycopg.Error as e:
        raise HTTPException(400, str(e).strip())

    return {
        "columns": columns,
        "rows": rows,
        "truncated": len(rows) == req.limit,
        "ms": round((time.perf_counter() - started) * 1000),
    }


@app.get("/api/sql/examples")
async def sql_examples():
    examples = []
    for path in sorted(SQL_DIR.glob("**/*.sql")):
        text = path.read_text(encoding="utf-8")
        # the first comment line is used as the title
        match = re.match(r"\s*--\s*(.+)", text)
        examples.append(
            {
                "file": path.relative_to(SQL_DIR).as_posix(),
                "title": match.group(1).strip() if match else path.stem,
                "sql": text,
            }
        )
    return examples


@app.get("/health")
async def health():
    return {"service": "dashboard", "status": "ok"}


app.include_router(bi.router)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
