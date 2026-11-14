from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC 'now'. SQLite has no native timezone-aware datetime type, so
    a timestamp written as timezone-aware comes back naive after a DB
    round-trip — comparing that against a fresh datetime.now(UTC) raises
    TypeError. Every stored timestamp and every comparison against one
    should go through this so they stay on the same (naive) footing."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_utc(value: datetime | None) -> datetime | None:
    """A stored (naive UTC) timestamp marked as UTC, for answers that must
    say their time zone: serialized, it ends in +00:00 instead of reading
    as local time."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def utc_iso(value: datetime) -> str:
    """A stored timestamp as an ISO 8601 string ending in Z."""
    return as_utc(value).isoformat().replace("+00:00", "Z")
