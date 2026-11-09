"""Server-side limits on connector work (#344).

Listings, opens, downloads, folder ZIPs and archive listings each hold a
worker thread and a connection to a source for as long as they run. Without
a limit a few users opening big folders, or a script, can take every worker
while the rest of the app waits. Each such request takes a slot first:

- a connector slot, counted globally and per source, for as long as it
  talks to the source;
- a transfer slot, counted per user, for a download or a folder ZIP until
  the client has it all (or goes away).

Slots never queue: when one isn't free the request is answered 503 with a
Retry-After header, and the client tries again a few seconds later. The
limits are System settings (Settings -> System); what's in use shows in
GET /monitoring/health and /metrics. In memory only, per process."""

import threading
from collections import Counter
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlmodel import Session

from app import system_settings
from app.logging_config import get_logger

logger = get_logger(__name__)

# What a 503 asks the client to wait, in seconds.
RETRY_AFTER_SECONDS = 5

GLOBAL_KEY = "concurrency_global"
SOURCE_KEY = "concurrency_per_source"
USER_KEY = "concurrency_user_transfers"


@dataclass(frozen=True)
class Limits:
    global_max: int
    per_source: int
    per_user: int


def current_limits(session: Session) -> Limits:
    return Limits(
        global_max=system_settings.get_int(session, GLOBAL_KEY),
        per_source=system_settings.get_int(session, SOURCE_KEY),
        per_user=system_settings.get_int(session, USER_KEY),
    )


_BUSY_MESSAGES = {
    "global": (
        "PerchTail is busy with other listings and downloads right now. "
        "Try again in a few seconds."
    ),
    "source": "This source is busy with other requests right now. Try again in a few seconds.",
    "user": (
        "You already have as many downloads and folder ZIPs in progress as this server "
        "allows. Wait for one to finish, then try again."
    ),
}


def busy(scope: str) -> HTTPException:
    """The 503 answered when a slot isn't free: `scope` is global, source
    or user."""
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"message": _BUSY_MESSAGES[scope], "error_code": "busy", "scope": scope},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


class Slot:
    """A slot taken from the limiter. release() is idempotent, so the
    `finally` of a streamed response and its background task can both call
    it. Also a context manager."""

    def __init__(self, release):
        self._release = release
        self._lock = threading.Lock()
        self._released = False

    def release(self) -> None:
        with self._lock:
            if self._released:
                return
            self._released = True
        self._release()

    def __enter__(self) -> "Slot":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


class SlotLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._global = 0
        self._sources: Counter[int] = Counter()
        self._users: Counter[int] = Counter()
        self._rejected: Counter[str] = Counter()

    def _reject(self, scope: str, **context) -> HTTPException:
        self._rejected[scope] += 1
        logger.warning("limits.busy", scope=scope, **context)
        return busy(scope)

    def connector(self, limits: Limits, source_id: int) -> Slot:
        """A connector slot (global and per source), or a 503."""
        with self._lock:
            if self._global >= limits.global_max:
                raise self._reject("global", source_id=source_id)
            if self._sources[source_id] >= limits.per_source:
                raise self._reject("source", source_id=source_id)
            self._global += 1
            self._sources[source_id] += 1

        def release() -> None:
            with self._lock:
                self._global = max(0, self._global - 1)
                self._sources[source_id] -= 1
                if self._sources[source_id] <= 0:
                    del self._sources[source_id]

        return Slot(release)

    def transfer(self, limits: Limits, user_id: int) -> Slot:
        """A transfer slot (per user, downloads and folder ZIPs), or a 503."""
        with self._lock:
            if self._users[user_id] >= limits.per_user:
                raise self._reject("user", user_id=user_id)
            self._users[user_id] += 1

        def release() -> None:
            with self._lock:
                self._users[user_id] -= 1
                if self._users[user_id] <= 0:
                    del self._users[user_id]

        return Slot(release)

    def snapshot(self, limits: Limits) -> dict:
        with self._lock:
            return {
                "global_in_use": self._global,
                "global_max": limits.global_max,
                "per_source_max": limits.per_source,
                "busiest_source_in_use": max(self._sources.values(), default=0),
                "sources_in_use": {str(k): v for k, v in self._sources.items()},
                "per_user_transfers_max": limits.per_user,
                "transfers_in_use": sum(self._users.values()),
                "busiest_user_transfers": max(self._users.values(), default=0),
                "rejected": {
                    scope: self._rejected.get(scope, 0) for scope in ("global", "source", "user")
                },
            }

    def reset(self) -> None:
        """For tests."""
        with self._lock:
            self._global = 0
            self._sources.clear()
            self._users.clear()
            self._rejected.clear()


_limiter = SlotLimiter()


def get_limiter() -> SlotLimiter:
    return _limiter
