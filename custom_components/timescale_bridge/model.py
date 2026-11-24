"""API validation and time policy, independent of HA and PostgreSQL."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Africa/Johannesburg")
MAX_POINTS = 2048
MAX_SERIES = 8
RAW_MINUTES = {"1h": 1, "3h": 2, "6h": 5, "12h": 15}
SPANS = {"1h": 1, "3h": 3, "6h": 6, "12h": 12, "day": 24, "week": 168, "month": 744, "year": 8784}
VIEWS = {
    "30m": "cagg_metrics_thirty_mins",
    "1h": "cagg_metrics_hourly",
    "1d": "cagg_metrics_daily",
    "1mo": "cagg_metrics_monthly",
}


class BridgeError(Exception):
    """Safe public error; never populated with database exception text."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def invalid(message):
    raise BridgeError("invalid_request", message)


def instant(value):
    if not isinstance(value, str) or len(value) > 40:
        invalid("Use an ISO timestamp with an explicit UTC offset.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        result = result.astimezone(UTC)
        if not 1970 <= result.year <= 2100:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        invalid("Use an ISO timestamp with an explicit UTC offset (1970–2100).")


def bucket_start(value, resolution):
    local = value.astimezone(ZONE)
    if resolution == "1mo":
        local = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif resolution == "1d":
        local = local.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        minutes = 60 if resolution == "1h" else int(resolution[:-1])
        local = local.replace(minute=(local.minute // minutes) * minutes, second=0, microsecond=0)
    return local.astimezone(UTC)


def next_bucket(value, resolution):
    if resolution == "1mo":
        local = value.astimezone(ZONE)
        return local.replace(
            year=local.year + (local.month == 12), month=local.month % 12 + 1
        ).astimezone(UTC)
    minutes = {"1d": 1440, "1h": 60}.get(resolution)
    return value + timedelta(minutes=minutes or int(resolution[:-1]))


@dataclass(frozen=True)
class Query:
    ids: tuple[int, ...]
    start: datetime
    end: datetime
    preset: str
    resolution: str

    @classmethod
    def parse(cls, message):
        ids = message.get("series_ids")
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= MAX_SERIES
            or any(type(x) is not int or not 0 < x < 2**31 for x in ids)
            or len(set(ids)) != len(ids)
        ):
            invalid("Supply 1–8 distinct positive series IDs.")
        start, end = instant(message.get("start")), instant(message.get("end"))
        preset = message.get("preset")
        if preset not in SPANS or not start < end:
            invalid("Supply a supported preset and start before end.")
        if end - start > timedelta(hours=SPANS[preset]):
            invalid("Range exceeds the preset limit.")
        if preset in RAW_MINUTES:
            resolution = f"{RAW_MINUTES[preset]}m"
        elif preset == "week":
            resolution = message.get("resolution")
            if resolution not in ("1h", "1d"):
                invalid("Week requires explicit resolution 1h or 1d pending live verification.")
        else:
            resolution = {"day": "30m", "month": "1d", "year": "1mo"}[preset]
        if "resolution" in message and message["resolution"] != resolution:
            invalid("Resolution does not match the preset.")
        # Rollup values cannot be clipped accurately at arbitrary boundaries.
        if preset not in RAW_MINUTES and any(
            bucket_start(t, resolution) != t for t in (start, end)
        ):
            invalid("Rollup ranges must align to complete Johannesburg buckets.")
        return cls(tuple(ids), start, end, preset, resolution)

    def buckets(self):
        cursor = bucket_start(self.start, self.resolution)
        result = []
        while cursor < self.end:
            result.append(cursor)
            if len(result) > MAX_POINTS:
                invalid("Too many points requested.")
            cursor = next_bucket(cursor, self.resolution)
        return result
