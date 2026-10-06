"""Database helpers shared by all services."""

import os
import time
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from . import reference

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://opa:opa@127.0.0.1:5432/opanalytics"
)
SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

# Any number works, it just has to be the same in every service.
SCHEMA_LOCK_ID = 734201

DEFAULT_SETTINGS = {
    "traffic": {
        "running": False,
        "tps": 15,
        "refund_delay_s": [5, 60],
    },
    "gateway": {
        "cascade": True,
        "smart_routing": False,
        "timeout_ms": 2500,
    },
    "detector": {
        "enabled": True,
        "interval_s": 20,
        "window_min": 5,
        "guard_min": 5,
        "baseline_min": 60,
    },
}

INSERT_MERCHANT = """
    INSERT INTO merchants
    VALUES (%(merchant_id)s, %(name)s, %(vertical)s, %(home_country)s,
            %(risk_tier)s, %(volume_weight)s, %(base_refund_rate)s)
    ON CONFLICT (merchant_id) DO NOTHING
"""

INSERT_PROVIDER = """
    INSERT INTO providers (provider_id, name, weight, base_approval,
                           base_latency_ms, methods, fee_pct)
    VALUES (%(provider_id)s, %(name)s, %(weight)s, %(base_approval)s,
            %(base_latency_ms)s, %(methods)s, %(fee_pct)s)
    ON CONFLICT (provider_id) DO NOTHING
"""

# New default keys are added, values changed by the user are kept.
INSERT_SETTINGS = """
    INSERT INTO settings (key, value) VALUES (%s, %s)
    ON CONFLICT (key) DO UPDATE SET value = %s || settings.value
"""

PATCH_SETTINGS = """
    INSERT INTO settings (key, value) VALUES (%s, %s)
    ON CONFLICT (key) DO UPDATE
    SET value = settings.value || EXCLUDED.value, updated_at = now()
    RETURNING value
"""


async def create_pool(max_size=10):
    pool = AsyncConnectionPool(
        DATABASE_URL,
        min_size=1,
        max_size=max_size,
        open=False,
        kwargs={"autocommit": True, "row_factory": dict_row},
    )
    await pool.open(wait=True, timeout=60)
    return pool


async def init_db(pool):
    """Create tables, reference data and default settings.

    Every service calls this on startup, the advisory lock makes sure
    they don't do it at the same time.
    """
    async with pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_lock(%s)", (SCHEMA_LOCK_ID,))
        try:
            await conn.execute(SCHEMA_SQL)
            for merchant in reference.MERCHANTS:
                await conn.execute(INSERT_MERCHANT, merchant)
            for provider in reference.PROVIDERS:
                await conn.execute(INSERT_PROVIDER, provider)
            for key, value in DEFAULT_SETTINGS.items():
                await conn.execute(
                    INSERT_SETTINGS, (key, Jsonb(value), Jsonb(value))
                )
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(%s)", (SCHEMA_LOCK_ID,)
            )


async def get_settings(pool, key):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT value FROM settings WHERE key = %s", (key,)
        )
        row = await cur.fetchone()
    if row:
        return row["value"]
    return dict(DEFAULT_SETTINGS.get(key, {}))


async def patch_settings(pool, key, patch):
    async with pool.connection() as conn:
        cur = await conn.execute(PATCH_SETTINGS, (key, Jsonb(patch)))
        row = await cur.fetchone()
    return row["value"]


class SettingsCache:
    def __init__(self, pool, key, ttl=2.0):
        self.pool = pool
        self.key = key
        self.ttl = ttl
        self.value = dict(DEFAULT_SETTINGS.get(key, {}))
        self._loaded = 0.0

    async def get(self):
        if time.monotonic() - self._loaded > self.ttl:
            try:
                self.value = await get_settings(self.pool, self.key)
            finally:
                self._loaded = time.monotonic()
        return self.value
