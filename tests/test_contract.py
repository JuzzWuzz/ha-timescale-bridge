import asyncio
from datetime import timedelta

import pytest

from custom_components.timescale_bridge.model import BridgeError, Query, instant


async def test_closed_day_missing_zero_and_energy_comparison(bridge, message, sample):
    result = await bridge.request(message, now=instant("2026-11-23T00:00:00Z"))
    assert result["completeness"] == "unknown"
    for series in result["series"]:
        sid = str(series["id"])
        assert len(series["points"]) == 48
        assert series["points"][2][1] is None
        assert series["coverage"][2]["state"] == "missing"
        assert series["coverage"][0]["state"] == "observed"
        assert (
            sum(p[1] for p in series["points"] if p[1] is not None)
            == (sample["expected_legacy_bucket_sums"][sid])
        )
        raw = sample["raw_meters"][sid]
        # Independent boundary reference from exact midnight readings, not sum of buckets.
        assert raw[-1][1] - raw[0][1] == sample["expected_boundary_totals"][sid]
        assert (
            sample["expected_boundary_totals"][sid] != (sample["expected_legacy_bucket_sums"][sid])
        )
    assert result["series"][1]["points"][0][1] == 0


async def test_explicit_johannesburg_offsets_equal_utc(bridge, message):
    utc = await bridge.request(message)
    local = await bridge.request(
        {**message, "start": "2026-11-22T00:00:00+02:00", "end": "2026-11-23T00:00:00+02:00"}
    )
    assert utc == local


async def test_partial_live_bucket(bridge, message):
    result = await bridge.request(message, now=instant(message["start"]) + timedelta(minutes=10))
    assert result["series"][0]["coverage"][0] == {
        "state": "partial",
        "sample_count": 2,
        "live": True,
        "clipped": False,
    }


async def test_missing_counter_value_is_partial(bridge, database, message):
    database.points.side_effect = None
    database.points.return_value = [
        {"bucket_start": instant(message["start"]), "value": None, "count": 1}
    ]
    result = await bridge.request(message)
    assert result["series"][0]["coverage"][0]["state"] == "partial"
    assert result["series"][0]["points"][0][1] is None


@pytest.mark.parametrize(
    "change",
    [
        {"series_ids": [True]},
        {"series_ids": [1, 1]},
        {"series_ids": []},
        {"series_ids": list(range(1, 10))},
        {"series_ids": ["1; DROP TABLE raw_metrics"]},
        {"start": "2026-11-22T00:00:00"},
        {"end": "2026-11-21T22:00:00Z"},
        {"end": "2026-11-24T22:00:00Z"},
        {"start": "2026-11-21T22:01:00Z"},
        {"preset": "week"},
        {"resolution": "1d"},
        {"preset": "arbitrary_sql"},
    ],
)
async def test_invalid_requests_do_no_io(bridge, database, message, change):
    with pytest.raises(BridgeError, match="."):
        await bridge.request({**message, **change})
    database.lookup.assert_not_awaited()


@pytest.mark.parametrize(("preset", "minutes"), [("1h", 1), ("3h", 2), ("6h", 5), ("12h", 15)])
def test_rolling_resolution(message, preset, minutes):
    message.update(preset=preset, end="2026-11-21T23:00:00Z")
    query = Query.parse(message)
    assert query.resolution == f"{minutes}m"
    assert len(query.buckets()) == 60 // minutes


def test_monthly_calendar_buckets(message):
    query = Query.parse(
        {
            **message,
            "preset": "year",
            "start": "2024-12-31T22:00:00Z",
            "end": "2025-12-31T22:00:00Z",
        }
    )
    assert len(query.buckets()) == 12
    assert query.buckets()[2] == instant("2025-02-28T22:00:00Z")


async def test_pagination_and_unknown_series(bridge, message):
    first = await bridge.request({"type": "timescale_bridge/catalog", "limit": 2})
    second = await bridge.request(
        {"type": "timescale_bridge/catalog", "limit": 2, "after_id": first["next_after_id"]}
    )
    assert [s["id"] for s in second["series"]] == [3]
    assert second["next_after_id"] is None
    with pytest.raises(BridgeError) as error:
        await bridge.request({**message, "series_ids": [999]})
    assert error.value.code == "unknown_series"


async def test_backpressure_and_cancellation(bridge, database, message):
    gate = asyncio.Event()

    async def blocked(_):
        await gate.wait()

    database.lookup.side_effect = blocked
    tasks = [asyncio.create_task(bridge.request(message)) for _ in range(4)]
    await asyncio.sleep(0)
    with pytest.raises(BridgeError) as error:
        await bridge.request(message)
    assert error.value.code == "busy"
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    assert bridge._active == 0


async def test_nonfinite_and_duplicate_buckets_rejected(bridge, database, message):
    database.points.side_effect = None
    row = {"bucket_start": instant(message["start"]), "value": float("nan"), "count": 2}
    database.points.return_value = [row]
    with pytest.raises(BridgeError, match="non-finite"):
        await bridge.request(message)
    database.points.return_value = [row, row]
    with pytest.raises(BridgeError, match="unexpected buckets"):
        await bridge.request(message)


async def test_raw_edges_and_ordering(bridge, database, message):
    database.points.side_effect = None
    database.points.return_value = [
        {"bucket_start": instant("2026-11-21T22:01:00Z"), "value": -2.0, "count": 3},
        {"bucket_start": instant("2026-11-21T22:00:00Z"), "value": 0.0, "count": 2},
    ]
    result = await bridge.request(
        {
            **message,
            "preset": "1h",
            "series_ids": [3],
            "start": "2026-11-21T22:00:30Z",
            "end": "2026-11-21T22:01:30Z",
        }
    )
    series = result["series"][0]
    assert [p[1] for p in series["points"]] == [0, -2]
    assert all(c["state"] == "partial" and c["clipped"] for c in series["coverage"])


async def test_total_timeout_releases_request_slot(bridge, database, message, monkeypatch):
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda _: original_timeout(0.001))

    async def blocked(_):
        await asyncio.Event().wait()

    database.lookup.side_effect = blocked
    with pytest.raises(BridgeError) as error:
        await bridge.request(message)
    assert error.value.code == "timeout"
    assert bridge._active == 0


async def test_oversized_response_is_not_truncated(bridge, database, message):
    database.points.side_effect = None
    database.points.return_value = [{}] * 2049
    with pytest.raises(BridgeError) as error:
        await bridge.request(message)
    assert error.value.code == "too_many_points"


@pytest.mark.parametrize("fields", [{"limit": 501}, {"limit": True}, {"after_id": -1}])
async def test_invalid_catalog_limits(bridge, database, fields):
    with pytest.raises(BridgeError):
        await bridge.request({"type": "timescale_bridge/catalog", **fields})
    database.catalog.assert_not_awaited()
