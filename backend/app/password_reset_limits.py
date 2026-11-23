"""Rate limits for password recovery by email (#244, app/auth/password_reset.py).

Every request counts, not only failures (unlike app/login_throttle.py): the
point is to cap how many links can be asked for and how many tokens can be
tried, whatever happens to them. The values are fixed here, not settings.

- Asking for a link: REQUESTS_PER_ADDRESS per client address (as resolved
  by app/request_context.py; IPv6 grouped by /64, app.login_throttle
  .address_key) in REQUEST_WINDOW_SECONDS; over it, 429 with Retry-After.
- Asking for a link for one identifier: REQUESTS_PER_IDENTIFIER per
  account (or per submitted identifier when no account matches) in
  IDENTIFIER_WINDOW_SECONDS; over it, nothing is sent, but the answer is
  the same as always, so a caller can't fill someone's mailbox and can't
  tell whether the account exists.
- Checking or using a token: TOKEN_ATTEMPTS_PER_ADDRESS per client address
  in TOKEN_WINDOW_SECONDS, shared by both; over it, 429.

In memory and per process, like the login throttle. Each table is bounded,
and a flood can't evict a live entry: when a table is full of keys still
inside their window, a new key is refused (fail closed).
"""

import math
import threading
import time
from collections import deque

from app.login_throttle import address_key

REQUESTS_PER_ADDRESS = 10
REQUEST_WINDOW_SECONDS = 15 * 60.0
REQUESTS_PER_IDENTIFIER = 3
IDENTIFIER_WINDOW_SECONDS = 60 * 60.0
TOKEN_ATTEMPTS_PER_ADDRESS = 20
TOKEN_WINDOW_SECONDS = 15 * 60.0

_MAX_TRACKED_KEYS = 50_000


class SlidingWindow:
    """At most `limit` events per key within the last `window` seconds, in
    memory. Also used for the alert webhook test (app/api/alerts.py)."""

    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.events: dict[str, deque[float]] = {}

    def _trim(self, key: str, now: float) -> deque[float] | None:
        events = self.events.get(key)
        if events is None:
            return None
        while events and now - events[0] >= self.window:
            events.popleft()
        if not events:
            del self.events[key]
            return None
        return events

    def _has_room(self, now: float) -> bool:
        if len(self.events) < _MAX_TRACKED_KEYS:
            return True
        for key in list(self.events):
            self._trim(key, now)
        return len(self.events) < _MAX_TRACKED_KEYS

    def hit(self, key: str, now: float) -> int | None:
        """Counts one event for `key`. None when it's allowed; otherwise the
        whole seconds until one would be (the event isn't counted)."""
        events = self._trim(key, now)
        if events is None:
            if not self._has_room(now):
                return math.ceil(self.window)
            events = deque()
            self.events[key] = events
        if len(events) >= self.limit:
            return max(1, math.ceil(self.window - (now - events[0])))
        events.append(now)
        return None


_lock = threading.Lock()
_requests_by_address = SlidingWindow(REQUESTS_PER_ADDRESS, REQUEST_WINDOW_SECONDS)
_requests_by_identifier = SlidingWindow(REQUESTS_PER_IDENTIFIER, IDENTIFIER_WINDOW_SECONDS)
_token_attempts_by_address = SlidingWindow(TOKEN_ATTEMPTS_PER_ADDRESS, TOKEN_WINDOW_SECONDS)


def request_from_address(client_ip: str | None) -> int | None:
    """None when this address may ask for a link now; otherwise the seconds
    to wait."""
    with _lock:
        return _requests_by_address.hit(address_key(client_ip), time.monotonic())


def request_for_identifier(key: str) -> int | None:
    """None when a link may be sent for this account or identifier now."""
    with _lock:
        return _requests_by_identifier.hit(key, time.monotonic())


def token_attempt_from_address(client_ip: str | None) -> int | None:
    """None when this address may check or use a token now."""
    with _lock:
        return _token_attempts_by_address.hit(address_key(client_ip), time.monotonic())


def reset_for_tests() -> None:
    with _lock:
        for table in (_requests_by_address, _requests_by_identifier, _token_attempts_by_address):
            table.events.clear()
