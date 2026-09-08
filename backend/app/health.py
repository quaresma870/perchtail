"""In-process operational state for the detailed health endpoint
(app/api/monitoring.py). Deliberately in-memory, not persisted to SQLite —
same "per-process, doesn't need to survive a restart" reasoning
app.agent_registry already uses for live connection state: uptime and
"last successful sweep" are about *this running process*, not durable
business data."""

import time

from app.timeutils import utcnow

_started_at: float | None = None
_last_search_sweep_at_iso: str | None = None


def mark_started() -> None:
    global _started_at
    _started_at = time.monotonic()


def uptime_seconds() -> float:
    if _started_at is None:
        return 0.0
    return time.monotonic() - _started_at


def record_search_sweep() -> None:
    """Called once per completed indexing sweep (app.search_index's
    run_indexing_sweep), regardless of whether individual sources within
    it failed -- a partial sweep still counts as "the sweep ran"."""
    global _last_search_sweep_at_iso
    _last_search_sweep_at_iso = utcnow().isoformat()


def last_search_sweep_at() -> str | None:
    return _last_search_sweep_at_iso
