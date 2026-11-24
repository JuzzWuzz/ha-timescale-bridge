import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from custom_components.timescale_bridge.database import Database
from custom_components.timescale_bridge.model import BridgeError, Query


@pytest.fixture
def db():
    connection = AsyncMock()
    connection.fetchval.return_value = True
    connection.transaction = MagicMock()
    connection.transaction.return_value.__aenter__ = AsyncMock()
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=False)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    pool.close = AsyncMock()
    return Database(pool), connection


async def test_readonly_and_bound_values(db, message):
    database, conn = db
    await database.points(Query.parse(message), {"id": 1, "metric": "energy"})
    conn.transaction.assert_called_once_with(readonly=True)
    conn.fetchval.assert_awaited_once_with("SELECT pg_is_in_recovery()")
    sql, *args = conn.fetch.await_args.args
    assert "public.cagg_metrics_thirty_mins" in sql
    assert "device_metric_id = $1" in sql
    assert args == [1, Query.parse(message).start, Query.parse(message).end, 2049]
    database.pool.acquire.return_value.__aexit__.assert_awaited_once()


async def test_primary_fails_closed(db):
    database, conn = db
    conn.fetchval.return_value = False
    with pytest.raises(BridgeError) as error:
        await database.catalog(0, 200)
    assert error.value.code == "replica_required"
    conn.fetch.assert_not_awaited()


@pytest.mark.parametrize(
    "failure", [TimeoutError(), OSError("secret"), asyncpg.PostgresError("password=secret")]
)
async def test_database_errors_redacted(db, failure):
    database, conn = db
    conn.fetch.side_effect = failure
    with pytest.raises(BridgeError) as error:
        await database.catalog(0, 200)
    assert "secret" not in str(error.value)
    assert error.value.code == "replica_unavailable"
    database.pool.acquire.return_value.__aexit__.assert_awaited_once()


async def test_query_cancellation_releases_connection(db):
    database, conn = db
    conn.fetch.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await database.catalog(0, 200)
    conn.transaction.return_value.__aexit__.assert_awaited_once()
    database.pool.acquire.return_value.__aexit__.assert_awaited_once()


async def test_raw_gauges_do_not_run_counter_aggregate(db, message):
    database, conn = db
    query = Query.parse({**message, "preset": "1h", "end": "2026-11-21T23:00:00Z"})
    await database.points(query, {"id": 3, "metric": "temperature"})
    assert "counter_agg" not in conn.fetch.await_args.args[0]
    await database.points(query, {"id": 1, "metric": "energy"})
    assert "delta(counter_agg(time, value))" in conn.fetch.await_args.args[0]


async def test_pool_single_target_and_failed_open_cleanup(db):
    database, conn = db
    config = dict(
        host="replica.example",
        port=5432,
        database="test",
        username="readonly",
        password="not-real",
        sslmode="verify-full",
    )
    with patch("asyncpg.create_pool", AsyncMock(return_value=database.pool)) as create:
        await Database.open(config)
        kwargs = create.await_args.kwargs
        assert kwargs["host"] == "replica.example"
        assert kwargs["max_size"] == 2
        assert kwargs["server_settings"]["default_transaction_read_only"] == "on"
        conn.fetchval.return_value = False
        with pytest.raises(BridgeError):
            await Database.open(config)
        database.pool.terminate.assert_called_once()


async def test_close_timeout_terminates_pool(db):
    database, _ = db
    database.pool.close.side_effect = TimeoutError()
    await database.close()
    database.pool.terminate.assert_called_once()


@pytest.mark.parametrize(("preset", "minutes"), [("1h", 1), ("3h", 2), ("6h", 5), ("12h", 15)])
@pytest.mark.parametrize("metric", ["energy", "temperature"])
async def test_rolling_interval_uses_native_codec(db, message, preset, minutes, metric):
    database, conn = db
    query = Query.parse({**message, "preset": preset, "end": "2026-11-21T23:00:00Z"})
    await database.points(query, {"id": 1, "metric": metric})
    sql, *args = conn.fetch.await_args.args
    assert "time_bucket($4::interval" in sql
    assert args == [1, query.start, query.end, timedelta(minutes=minutes), 2049]
    assert type(args[3]) is timedelta
