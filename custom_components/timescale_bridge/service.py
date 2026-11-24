"""Catalog and historical query contract."""

import asyncio
import math
from datetime import UTC, datetime

from .model import MAX_POINTS, RAW_MINUTES, BridgeError, Query, next_bucket


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


class Bridge:
    def __init__(self, database):
        self.database = database
        self._active = 0

    async def request(self, message, *, now=None):
        # Reject excess work instead of accumulating an unbounded waiting queue.
        if self._active >= 4:
            raise BridgeError("busy", "Too many active bridge requests.")
        self._active += 1
        try:
            async with asyncio.timeout(30):
                if message["type"] == "timescale_bridge/catalog":
                    after, limit = message.get("after_id", 0), message.get("limit", 200)
                    if (
                        type(after) is not int
                        or not 0 <= after < 2**31
                        or type(limit) is not int
                        or not 1 <= limit <= 500
                    ):
                        raise BridgeError("invalid_request", "Invalid catalog pagination.")
                    rows = await self.database.catalog(after, limit)
                    return {
                        "api_version": 1,
                        "series": [dict(r) for r in rows[:limit]],
                        "next_after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
                    }
                query = Query.parse(message)
                buckets = query.buckets()
                series = {r["id"]: dict(r) for r in await self.database.lookup(query.ids)}
                if set(series) != set(query.ids):
                    raise BridgeError("unknown_series", "One or more series IDs do not exist.")
                result = []
                now = now or datetime.now(UTC)
                for sid in query.ids:
                    rows = await self.database.points(query, series[sid])
                    if len(rows) > MAX_POINTS:
                        raise BridgeError("too_many_points", "Replica returned too many points.")
                    indexed = {r["bucket_start"]: r for r in rows}
                    if len(indexed) != len(rows) or not set(indexed).issubset(buckets):
                        raise BridgeError("invalid_data", "Replica returned unexpected buckets.")
                    points, coverage = [], []
                    for bucket in buckets:
                        row = indexed.get(bucket)
                        value = row["value"] if row else None
                        count = int(row["count"]) if row else 0
                        if value is not None and not math.isfinite(value):
                            raise BridgeError(
                                "invalid_data", "Replica returned a non-finite value."
                            )
                        edge = (
                            bucket < query.start
                            or next_bucket(bucket, query.resolution) > query.end
                        )
                        live = next_bucket(bucket, query.resolution) > now
                        state = (
                            "missing"
                            if not row or count == 0
                            else "partial"
                            if edge or live or value is None
                            else "observed"
                        )
                        points.append([int(bucket.timestamp() * 1000), value if count else None])
                        coverage.append(
                            {"state": state, "sample_count": count, "live": live, "clipped": edge}
                        )
                    result.append({**series[sid], "points": points, "coverage": coverage})
                return {
                    "api_version": 1,
                    "start": iso(query.start),
                    "end": iso(query.end),
                    "timezone": "Africa/Johannesburg",
                    "resolution": query.resolution,
                    "source": "raw" if query.preset in RAW_MINUTES else "rollup",
                    "energy_semantics": "legacy_bucket_delta",
                    "completeness": "unknown",
                    "series": result,
                }
        except TimeoutError:
            raise BridgeError("timeout", "Bridge request timed out.") from None
        finally:
            self._active -= 1
