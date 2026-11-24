"""Single-target, bounded, read-only PostgreSQL access."""

import asyncio
from datetime import timedelta

import asyncpg

from .model import MAX_POINTS, RAW_MINUTES, VIEWS, BridgeError

CATALOG_SQL = """
SELECT id, device, metric, subtype FROM public.devices_metrics
WHERE id > $1 ORDER BY id LIMIT $2
"""
LOOKUP_SQL = """
SELECT id, device, metric, subtype FROM public.devices_metrics
WHERE id = ANY($1::int[]) ORDER BY id
"""
RAW_SQL = """
SELECT time_bucket($4::interval, time, 'Africa/Johannesburg') AS bucket_start,
       {aggregate} AS value,
       count(*) AS count
FROM public.raw_metrics
WHERE device_metric_id = $1 AND time >= $2 AND time < $3
GROUP BY 1 ORDER BY 1 LIMIT $5
"""


class Database:
    def __init__(self, pool):
        self.pool = pool

    @classmethod
    async def open(cls, config):
        pool = await asyncpg.create_pool(
            host=config["host"],
            port=config["port"],
            database=config["database"],
            user=config["username"],
            password=config["password"],
            ssl=config["sslmode"],
            min_size=0,
            max_size=2,
            timeout=5,
            command_timeout=10,
            server_settings={
                "default_transaction_read_only": "on",
                "statement_timeout": "10000",
                "application_name": "ha_timescale_bridge",
            },
        )
        db = cls(pool)
        try:
            await db.fetch("SELECT 1")
        except BaseException:
            pool.terminate()
            raise
        return db

    async def fetch(self, sql, *args):
        try:
            async with asyncio.timeout(15):
                async with self.pool.acquire(timeout=5) as conn:
                    async with conn.transaction(readonly=True):
                        # Fail closed if the replica is promoted or the host points at a primary.
                        if not await conn.fetchval("SELECT pg_is_in_recovery()"):
                            raise BridgeError("replica_required", "The target is not a replica.")
                        return await conn.fetch(sql, *args)
        except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            raise BridgeError("replica_unavailable", "Replica read failed or timed out.") from None

    async def catalog(self, after_id, limit):
        return await self.fetch(CATALOG_SQL, after_id, limit + 1)

    async def lookup(self, ids):
        return await self.fetch(LOOKUP_SQL, list(ids))

    async def points(self, query, series):
        if query.preset in RAW_MINUTES:
            return await self.fetch(
                RAW_SQL.format(
                    aggregate=(
                        "CASE WHEN count(*) > 1 THEN delta(counter_agg(time, value)) ELSE NULL END"
                        if series["metric"] == "energy"
                        else "avg(value)"
                    )
                ),
                series["id"],
                query.start,
                query.end,
                timedelta(minutes=RAW_MINUTES[query.preset]),
                MAX_POINTS + 1,
            )
        # Identifiers come exclusively from this internal allowlist, never from a request.
        view = VIEWS[query.resolution]
        return await self.fetch(
            f"SELECT bucket_start, value, count FROM public.{view} "
            "WHERE device_metric_id = $1 AND bucket_start >= $2 AND bucket_start < $3 "
            "ORDER BY bucket_start LIMIT $4",
            series["id"],
            query.start,
            query.end,
            MAX_POINTS + 1,
        )

    async def close(self):
        try:
            async with asyncio.timeout(5):
                await self.pool.close()
        except TimeoutError:
            self.pool.terminate()
