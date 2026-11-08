"""In-process operational state for the detailed health endpoint
(app/api/monitoring.py). Deliberately in-memory, not persisted to SQLite —
same "per-process, doesn't need to survive a restart" reasoning
app.agent_registry already uses for live connection state: uptime and
"last successful sweep" are about *this running process*, not durable
business data."""

import time

_started_at: float | None = None


def mark_started() -> None:
    global _started_at
    _started_at = time.monotonic()


def uptime_seconds() -> float:
    if _started_at is None:
        return 0.0
    return time.monotonic() - _started_at
