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
the attempts across. A bounded LRU-style eviction keeps a flood of distinct
usernames (a cheap, lookup-free path when the username doesn't exist) from
growing this dict without limit.
"""

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from app.config import get_settings

_MAX_TRACKED_USERNAMES = 10_000


@dataclass
class _AttemptState:
    failures: int = 0
    locked_until: float | None = None


_lock = threading.Lock()
_state: "OrderedDict[str, _AttemptState]" = OrderedDict()


def _get_or_create(username: str) -> _AttemptState:
    entry = _state.get(username)
    if entry is None:
        if len(_state) >= _MAX_TRACKED_USERNAMES:
            _state.popitem(last=False)  # evict the least-recently-touched entry
        entry = _AttemptState()
        _state[username] = entry
    else:
        _state.move_to_end(username)
    return entry


def seconds_until_unlocked(username: str) -> float | None:
    """None if the username isn't currently locked out; otherwise the whole
    number of seconds remaining, for a Retry-After header."""
    with _lock:
        entry = _state.get(username)
        if entry is None or entry.locked_until is None:
            return None
        remaining = entry.locked_until - time.monotonic()
        if remaining <= 0:
            entry.locked_until = None
            entry.failures = 0
            return None
        return remaining


def record_failure(username: str) -> None:
    settings = get_settings()
    with _lock:
        entry = _get_or_create(username)
        entry.failures += 1
        if entry.failures >= settings.login_max_attempts:
            entry.locked_until = time.monotonic() + settings.login_lockout_seconds


def record_success(username: str) -> None:
    with _lock:
        _state.pop(username, None)


def reset_for_tests() -> None:
    """Test-only: clears all tracked state between test cases so one test's
    lockout can't bleed into the next."""
    with _lock:
        _state.clear()
