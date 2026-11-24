"""Opt-in codec/SQL check against a new local disposable database, never a supplied DSN.

Run from repository root: PYTHONPATH=. <test-python> scripts/verify_disposable_timescale.py
Requires local Docker and a pre-pulled timescale/timescaledb-ha:pg17 image.
"""

import asyncio
import json
import os
import subprocess
import uuid
from datetime import timedelta

import asyncpg

from custom_components.timescale_bridge.database import Database
from custom_components.timescale_bridge.model import Query

IMAGE = "timescale/timescaledb-ha:pg17"


def docker(*args):
    env = os.environ.copy()
    for key in (
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        env.pop(key, None)
    return subprocess.check_output(
        ["docker", "--host=unix:///var/run/docker.sock", *args], env=env, text=True
    ).strip()


class CaptureQuery(Database):
    """Capture production SQL/args; don't bypass the production replica guard."""

    async def fetch(self, sql, *args):
        return sql, args


async def check(port):
    connection = None
    for _ in range(120):
        try:
            connection = await asyncpg.connect(
                host="127.0.0.1",
                port=port,
                user="postgres",
                database="bridge_fixture",
                ssl=False,
                timeout=1,
            )
            break
        except (OSError, asyncpg.PostgresError):
            await asyncio.sleep(0.5)
    if connection is None:
        raise RuntimeError("Disposable database did not become ready")
    try:
        await connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        await connection.execute("CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit")
        print(await connection.fetchval("SELECT version()"))
        print(
            [
                dict(r)
                for r in await connection.fetch(
                    "SELECT extname, extversion FROM pg_extension ORDER BY extname"
                )
            ]
        )
        await connection.execute(
            "CREATE TABLE public.raw_metrics (time timestamptz, device_metric_id int, value float8)"
        )
        base = {
            "series_ids": [1],
            "start": "2026-11-21T22:00:00Z",
            "end": "2026-11-21T23:00:00Z",
            "preset": "1h",
        }
        start = Query.parse(base).start
        await connection.executemany(
            "INSERT INTO public.raw_metrics VALUES ($1, $2, $3)",
            [
                (start + timedelta(seconds=20 * i), sid, offset + i)
                for sid, offset in ((1, 100.0), (2, -10.0))
                for i in range(90)
            ],
        )
        # Demonstrate the old failure using asyncpg's real interval codec.
        try:
            await connection.fetchval("SELECT $1::interval", "1 minutes")
        except asyncpg.DataError:
            print("Old string interval rejected as expected")
        else:
            raise AssertionError("String unexpectedly accepted by interval codec")
        for preset, minutes in (("1h", 1), ("3h", 2), ("6h", 5), ("12h", 15)):
            interval = timedelta(minutes=minutes)
            assert await connection.fetchval("SELECT $1::interval", interval) == interval
            for sid, metric in ((1, "energy"), (2, "temperature")):
                query = Query.parse({**base, "preset": preset})
                sql, args = await CaptureQuery(None).points(query, {"id": sid, "metric": metric})
                rows = await connection.fetch(sql, *args)
                assert len(rows) == 30 // minutes
                for i, row in enumerate(rows):
                    assert row["bucket_start"] == start + i * interval
                    assert row["count"] == 3 * minutes
                    expected = (
                        3 * minutes - 1
                        if metric == "energy"
                        else -10 + i * 3 * minutes + (3 * minutes - 1) / 2
                    )
                    assert row["value"] == expected
                print(f"PASS {preset} {metric}: codec, buckets, counts and values")
    finally:
        await connection.close()


def main():
    name = f"bridge-codec-{uuid.uuid4().hex[:12]}"
    docker(
        "run",
        "--pull=never",
        "--rm",
        "-d",
        "--name",
        name,
        "-e",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        "-e",
        "POSTGRES_DB=bridge_fixture",
        "-p",
        "127.0.0.1::5432",
        IMAGE,
    )
    try:
        info = json.loads(docker("inspect", name))[0]
        port = int(info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"])
        print("Disposable image:", info["Image"])
        asyncio.run(check(port))
    finally:
        docker("rm", "-f", "-v", name)


if __name__ == "__main__":
    main()
