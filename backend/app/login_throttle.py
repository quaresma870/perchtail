"""Login rate limiting / brute-force lockout for POST /auth/login (see
ROADMAP.md's security hardening backlog -- "no throttling exists today
beyond argon2id's own hashing cost").

In-memory and per-process, not persisted to the DB or shared across
instances -- an accepted trade-off for a single-container SQLite deployment
(CLAUDE.md's own architecture: APScheduler is already in-process for the
same reason). A restart clears all lockouts; that's a narrow, self-healing
gap, not a security regression, since restarting the app isn't something an
unauthenticated attacker can trigger.

Keyed by the submitted username, not by client IP -- the threat this closes
is repeated password guessing against one account, which failure-count
tracking must catch regardless of how many source IPs an attacker spreads
the attempts across.

The dict is bounded, but a flood of other usernames must never be able to
evict (and so reset) an account's failure count or lockout. Entries only
leave once they're idle -- unlocked, with no failure inside the lockout
window -- and if the table is ever full of live entries, new usernames are
refused (fail closed) rather than tracked by evicting someone else.
"""

import threading
import time
from dataclasses import dataclass

from app.config import get_settings

_MAX_TRACKED_USERNAMES = 10_000


@dataclass
class _AttemptState:
    failures: int = 0
    locked_until: float | None = None
    last_failure: float = 0.0


_lock = threading.Lock()
_state: dict[str, _AttemptState] = {}


def _is_idle(entry: _AttemptState, now: float, window: float) -> bool:
    if entry.locked_until is not None:
        return entry.locked_until <= now
    return now - entry.last_failure >= window


def _prune(now: float, window: float) -> None:
    for username in [u for u, e in _state.items() if _is_idle(e, now, window)]:
        del _state[username]


def _has_room(now: float, window: float) -> bool:
    if len(_state) < _MAX_TRACKED_USERNAMES:
        return True
    _prune(now, window)
    return len(_state) < _MAX_TRACKED_USERNAMES


def seconds_until_unlocked(username: str) -> float | None:
    """None if the username may attempt a login; otherwise the whole number
    of seconds to wait, for a Retry-After header. Also non-None for an
    untracked username while the tracker is full of live entries."""
    window = float(get_settings().login_lockout_seconds)
    now = time.monotonic()
    with _lock:
        entry = _state.get(username)
        if entry is None:
            return None if _has_room(now, window) else window
        if entry.locked_until is None:
            return None
        remaining = entry.locked_until - now
        if remaining <= 0:
            del _state[username]
            return None
        return remaining


def record_failure(username: str) -> None:
    settings = get_settings()
    window = float(settings.login_lockout_seconds)
    now = time.monotonic()
    with _lock:
        entry = _state.get(username)
        if entry is not None and _is_idle(entry, now, window):
            entry = None
            del _state[username]
        if entry is None:
            if not _has_room(now, window):
                return  # saturated: seconds_until_unlocked already refuses it
            entry = _AttemptState()
            _state[username] = entry
        entry.failures += 1
        entry.last_failure = now
        if entry.failures >= settings.login_max_attempts:
            entry.locked_until = now + settings.login_lockout_seconds


def record_success(username: str) -> None:
    with _lock:
        _state.pop(username, None)


def reset_for_tests() -> None:
    """Test-only: clears all tracked state between test cases so one test's
    lockout can't bleed into the next."""
    with _lock:
        _state.clear()
