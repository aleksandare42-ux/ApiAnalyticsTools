"""Anomaly detector service.

A background loop runs the live checks every `interval_s` seconds and
opens, updates or resolves incidents in the anomalies table.

POST /run           run the live checks now
POST /scan-history  scan the whole history hour by hour
"""

import asyncio
import time
from contextlib import asynccontextmanager

import pandas as pd
from fastapi import FastAPI

from .. import detection
from ..db import SettingsCache, create_pool, init_db

# Current window: the last `win` minutes of live traffic.
# Baseline: `base` minutes before that, with a `guard` minutes gap, so an
# incident that started a few minutes ago doesn't end up in its own
# baseline. Seed data counts for the baseline too.
LIVE_SQL = """
WITH bounds AS (
    SELECT now() - interval '1 minute' * %(win)s AS cur_from,
           now() - interval '1 minute' * (%(win)s + %(guard)s) AS base_to,
           now() - interval '1 minute' * (%(win)s + %(guard)s + %(base)s)
               AS base_from
),
w AS (
    SELECT t.{column} AS v,
           t.created_at >= b.cur_from AS cur,
           t.status,
           t.processing_time_ms AS ms
    FROM transactions t, bounds b
    WHERE t.created_at >= b.base_from
      AND ((t.created_at >= b.cur_from AND t.source = 'live')
           OR t.created_at < b.base_to)
)
SELECT v,
       count(*) FILTER (WHERE cur) AS cur_n,
       count(*) FILTER (WHERE cur AND status = 'approved') AS cur_ok,
       count(*) FILTER (WHERE cur AND status = 'error') AS cur_err,
       avg(ms) FILTER (WHERE cur AND status <> 'error') AS cur_lat,
       stddev_samp(ms) FILTER (WHERE cur AND status <> 'error')
           AS cur_lat_sd,
       count(*) FILTER (WHERE NOT cur) AS base_n,
       count(*) FILTER (WHERE NOT cur AND status = 'approved') AS base_ok,
       count(*) FILTER (WHERE NOT cur AND status = 'error') AS base_err,
       avg(ms) FILTER (WHERE NOT cur AND status <> 'error') AS base_lat,
       stddev_samp(ms) FILTER (WHERE NOT cur AND status <> 'error')
           AS base_lat_sd
FROM w
GROUP BY v
"""

REFUND_SQL = """
WITH bounds AS (
    SELECT now() - interval '1 minute' * %(win)s AS cur_from,
           now() - interval '1 minute' * (%(win)s + %(guard)s) AS base_to,
           now() - interval '1 minute' * (%(win)s + %(guard)s + %(base)s)
               AS base_from
)
SELECT t.merchant_id AS v,
       count(*) FILTER (WHERE t.status = 'approved'
                          AND t.created_at >= b.cur_from) AS cur_ok,
       count(*) FILTER (WHERE t.refunded
                          AND t.refunded_at >= b.cur_from) AS cur_ref,
       count(*) FILTER (WHERE t.status = 'approved'
                          AND t.created_at < b.base_to) AS base_ok,
       count(*) FILTER (WHERE t.refunded
                          AND t.refunded_at < b.base_to
                          AND t.refunded_at >= b.base_from) AS base_ref
FROM transactions t, bounds b
WHERE t.source = 'live'
  AND (t.created_at >= b.base_from OR t.refunded_at >= b.base_from)
GROUP BY t.merchant_id
"""

# Keeps one open incident per (dimension, value, metric). Severity can
# only go up while the incident is open.
UPSERT_LIVE = """
INSERT INTO anomalies (
    detected_by, dimension, dim_value, metric, status, severity,
    observed, expected, z_score, sample_size, window_start, window_end,
    message, correlated_with
) VALUES (
    'live', %(dimension)s, %(dim_value)s, %(metric)s, 'open', %(severity)s,
    %(observed)s, %(expected)s, %(z_score)s, %(sample_size)s,
    now() - interval '1 minute' * %(win)s, now(),
    %(message)s, %(correlated_with)s
)
ON CONFLICT (detected_by, dimension, dim_value, metric)
    WHERE status = 'open'
DO UPDATE SET
    last_seen = now(),
    window_end = now(),
    observed = EXCLUDED.observed,
    expected = EXCLUDED.expected,
    z_score = EXCLUDED.z_score,
    sample_size = EXCLUDED.sample_size,
    message = EXCLUDED.message,
    correlated_with = EXCLUDED.correlated_with,
    severity = CASE
        WHEN array_position(ARRAY['medium', 'high', 'critical'],
                            EXCLUDED.severity)
           > array_position(ARRAY['medium', 'high', 'critical'],
                            anomalies.severity)
        THEN EXCLUDED.severity
        ELSE anomalies.severity
    END
"""

RESOLVE_STALE = """
UPDATE anomalies
SET status = 'resolved', resolved_at = now()
WHERE detected_by = 'live'
  AND status = 'open'
  AND last_seen < now() - interval '1 second' * %s
RETURNING anomaly_id
"""

INSERT_BATCH = """
INSERT INTO anomalies (
    detected_by, dimension, dim_value, metric, status, severity,
    observed, expected, z_score, sample_size, window_start, window_end,
    first_seen, last_seen, resolved_at, message, correlated_with
) VALUES (
    'batch', %(dimension)s, %(dim_value)s, %(metric)s, %(status)s,
    %(severity)s, %(observed)s, %(expected)s, %(z_score)s, %(sample_size)s,
    %(window_start)s, %(window_end)s, %(window_start)s, %(window_end)s,
    %(resolved_at)s, %(message)s, %(correlated_with)s
)
ON CONFLICT DO NOTHING
"""

HOURLY_SQL = """
SELECT date_trunc('hour', created_at) AS t,
       {column} AS v,
       count(*) AS n,
       count(*) FILTER (WHERE status = 'approved') AS ok,
       count(*) FILTER (WHERE status = 'error') AS err,
       avg(processing_time_ms) FILTER (WHERE status <> 'error') AS lat
FROM transactions
GROUP BY 1, 2
"""

DAILY_REFUNDS_SQL = """
SELECT date_trunc('day', created_at) AS t,
       merchant_id AS v,
       count(*) FILTER (WHERE status = 'approved') AS approved,
       count(*) FILTER (WHERE refunded) AS refunds
FROM transactions
GROUP BY 1, 2
"""


@asynccontextmanager
async def lifespan(app):
    app.state.pool = await create_pool()
    await init_db(app.state.pool)
    app.state.settings = SettingsCache(app.state.pool, "detector")
    app.state.last_run = None
    app.state.last_scan = None
    app.state.lock = asyncio.Lock()
    task = asyncio.create_task(detector_loop())
    yield
    task.cancel()
    await app.state.pool.close()


app = FastAPI(title="Anomaly detector", lifespan=lifespan)


async def run_live():
    settings = await app.state.settings.get()
    params = {
        "win": int(settings.get("window_min", 5)),
        "guard": int(settings.get("guard_min", 5)),
        "base": int(settings.get("baseline_min", 60)),
    }
    started = time.perf_counter()
    findings = []

    async with app.state.lock, app.state.pool.connection() as conn:
        for dimension, column in detection.DIMENSIONS.items():
            sql = LIVE_SQL.format(column=column)
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
            for row in rows:
                findings += detection.check_window_row(dimension, row)
            if dimension in ("merchant", "country"):
                findings += detection.check_volume_rows(dimension, rows)

        cur = await conn.execute(REFUND_SQL, params)
        for row in await cur.fetchall():
            findings += detection.check_refund_row(row)

        detection.attribute_root_causes(findings)
        for f in findings:
            await conn.execute(
                UPSERT_LIVE, {**f.as_dict(), "win": params["win"]}
            )

        # an incident that wasn't seen for a few runs is over
        stale_after = max(3 * int(settings.get("interval_s", 20)), 45)
        cur = await conn.execute(RESOLVE_STALE, (stale_after,))
        resolved = len(await cur.fetchall())

    app.state.last_run = {
        "at": time.time(),
        "findings": len(findings),
        "resolved": resolved,
        "ms": round((time.perf_counter() - started) * 1000),
        "items": [f.message for f in findings],
    }
    return app.state.last_run


async def detector_loop():
    while True:
        settings = await app.state.settings.get()
        try:
            if settings.get("enabled", True):
                await run_live()
        except Exception as e:
            app.state.last_run = {"at": time.time(), "error": repr(e)}
        await asyncio.sleep(max(5, int(settings.get("interval_s", 20))))


async def load_frame(conn, sql):
    cur = await conn.execute(sql)
    df = pd.DataFrame(await cur.fetchall())
    if df.empty:
        return df
    df["t"] = pd.to_datetime(df["t"], utc=True)
    for column in df.columns:
        if column not in ("t", "v"):
            df[column] = pd.to_numeric(df[column])
    return df


def batch_row(finding):
    row = finding.as_dict()
    start = row["window_start"].to_pydatetime()
    end = row["window_end"].to_pydatetime()
    recent = end > pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=1)
    row["window_start"] = start
    row["window_end"] = end
    row["status"] = "open" if recent else "resolved"
    row["resolved_at"] = None if recent else end
    return row


async def run_history_scan():
    started = time.perf_counter()
    findings = []
    async with app.state.pool.connection() as conn:
        for dimension, column in detection.DIMENSIONS.items():
            df = await load_frame(conn, HOURLY_SQL.format(column=column))
            # pandas work is CPU bound, don't block the event loop
            findings += await asyncio.to_thread(
                detection.detect_hourly, df, dimension
            )

        df = await load_frame(conn, DAILY_REFUNDS_SQL)
        if not df.empty:
            findings += await asyncio.to_thread(
                detection.detect_daily_refunds, df
            )
        detection.attribute_root_causes(findings)

        await conn.execute("DELETE FROM anomalies WHERE detected_by = 'batch'")
        for f in findings:
            await conn.execute(INSERT_BATCH, batch_row(f))

    app.state.last_scan = {
        "at": time.time(),
        "findings": len(findings),
        "ms": round((time.perf_counter() - started) * 1000),
    }
    return app.state.last_scan


@app.post("/run")
async def run_now():
    return await run_live()


@app.post("/scan-history")
async def scan_history():
    return await run_history_scan()


@app.get("/status")
async def status():
    return {
        "settings": await app.state.settings.get(),
        "last_run": app.state.last_run,
        "last_scan": app.state.last_scan,
    }


@app.get("/health")
async def health():
    last_run = app.state.last_run
    if last_run:
        last_run = {k: v for k, v in last_run.items() if k != "items"}
    return {"service": "detector", "status": "ok", "last_run": last_run}
