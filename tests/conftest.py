import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from custom_components.timescale_bridge.model import instant
from custom_components.timescale_bridge.service import Bridge


@pytest.fixture
def sample():
    return json.loads((Path(__file__).parent / "fixtures/closed_day.json").read_text())


@pytest.fixture
def message(sample):
    return {
        "id": 1,
        "type": "timescale_bridge/query",
        "series_ids": [1, 2],
        "start": sample["start"],
        "end": sample["end"],
        "preset": "day",
    }


@pytest.fixture
def database(sample):
    db = AsyncMock()

    async def catalog(after, limit):
        return [r for r in sample["catalog"] if r["id"] > after][: limit + 1]

    async def lookup(ids):
        return [r for r in sample["catalog"] if r["id"] in ids]

    async def points(query, series):
        return [
            {
                "bucket_start": instant(sample["start"]) + timedelta(minutes=m),
                "value": value,
                "count": count,
            }
            for m, value, count in sample["rollups"].get(str(series["id"]), [])
        ]

    db.catalog.side_effect = catalog
    db.lookup.side_effect = lookup
    db.points.side_effect = points
    return db


@pytest.fixture
def bridge(database):
    return Bridge(database)
