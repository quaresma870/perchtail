from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC 'now'. SQLite has no native timezone-aware datetime type, so
    a timestamp written as timezone-aware comes back naive after a DB
    round-trip — comparing that against a fresh datetime.now(UTC) raises
    TypeError. Every stored timestamp and every comparison against one
    should go through this so they stay on the same (naive) footing."""
    return datetime.now(UTC).replace(tzinfo=None)
