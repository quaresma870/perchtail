"""Login rate limiting / brute-force lockout for POST /auth/login and the
password re-confirmations that share its budget.

In-memory and per-process, not persisted to the DB or shared across
instances -- an accepted trade-off for a single-container SQLite deployment
(APScheduler is already in-process for the same reason). A restart clears
all lockouts; that's a narrow, self-healing gap, not a security regression,
since restarting the app isn't something an unauthenticated attacker can
trigger.

Two limits, both counted on failures only:

- Per account: LOGIN_MAX_ATTEMPTS failures against one username lock it for
  LOGIN_LOCKOUT_SECONDS, however many addresses the attempts come from, so
  guessing one account's password can't be spread across a botnet.
  Emergency accounts (active local super admins with MFA on,
  app/auth/mfa_policy.py) are the exception: their failures are counted per
  username *and* client address, so an anonymous caller can't lock the
  break-glass account out of every address. Their MFA is what stands against
  guessing their password from many addresses; a super admin without MFA on
  yet is counted per username like everyone else.
- Per client address (as resolved by app/request_context.py; IPv6 grouped by
  /64, since one host usually holds the whole prefix):
  LOGIN_MAX_ATTEMPTS failures from one address, across every username, lock
  that address for LOGIN_LOCKOUT_SECONDS. Other addresses keep signing in. A
  success doesn't clear it: one valid account mustn't reset the budget an
  address spends guessing others. Everyone behind one NAT or shared proxy
  shares this budget, so five failures by anyone there lock the address for
  all of them; behind a reverse proxy, TRUSTED_PROXIES must name it so each
  client is counted by its own address.

Both tables are bounded, and a flood can never evict (and so reset) a live
entry: entries only leave once they're idle -- unlocked, with no failure
inside the lockout window. When the account table is full of live entries,
a new username isn't tracked and only the address limit applies to it (it
used to refuse every new username, which let one anonymous caller lock
everybody out). When the address table is full of live entries, a new
address is refused (fail closed); filling it takes that many distinct
addresses failing inside one lockout window.
"""

import ipaddress
import threading
import time
from dataclasses import dataclass

from app.config import get_settings

_MAX_TRACKED_ACCOUNTS = 10_000
_MAX_TRACKED_ADDRESSES = 50_000
_IPV6_PREFIX = 64
_NO_ADDRESS = "-"


@dataclass
class _AttemptState:
    failures: int = 0
    locked_until: float | None = None
    last_failure: float = 0.0


class _Tracker:
    def __init__(self, capacity_name: str, *, refuse_when_full: bool):
        # Read through the module so tests can lower the capacity.
        self._capacity_name = capacity_name
        self._refuse_when_full = refuse_when_full
        self.state: dict[str, _AttemptState] = {}

    def _capacity(self) -> int:
        return globals()[self._capacity_name]

    @staticmethod
    def _is_idle(entry: _AttemptState, now: float, window: float) -> bool:
        if entry.locked_until is not None:
            return entry.locked_until <= now
        return now - entry.last_failure >= window

    def _has_room(self, now: float, window: float) -> bool:
        if len(self.state) < self._capacity():
            return True
        for key in [k for k, e in self.state.items() if self._is_idle(e, now, window)]:
            del self.state[key]
        return len(self.state) < self._capacity()

    def seconds_locked(self, key: str, now: float, window: float) -> float | None:
        entry = self.state.get(key)
        if entry is None:
            if self._refuse_when_full and not self._has_room(now, window):
                return window
            return None
        if entry.locked_until is None:
            return None
        remaining = entry.locked_until - now
        if remaining <= 0:
            del self.state[key]
            return None
        return remaining

    def record_failure(self, key: str, now: float, window: float, max_attempts: int) -> bool:
        """True when this failure is the one that locks the key."""
        entry = self.state.get(key)
        if entry is not None and self._is_idle(entry, now, window):
            entry = None
            del self.state[key]
        if entry is None:
            if not self._has_room(now, window):
                return False  # saturated: see the module docstring
            entry = _AttemptState()
            self.state[key] = entry
        entry.failures += 1
        entry.last_failure = now
        if entry.locked_until is None and entry.failures >= max_attempts:
            entry.locked_until = now + window
            return True
        return False

    def clear(self, key: str) -> None:
        self.state.pop(key, None)


_lock = threading.Lock()
_accounts = _Tracker("_MAX_TRACKED_ACCOUNTS", refuse_when_full=False)
_addresses = _Tracker("_MAX_TRACKED_ADDRESSES", refuse_when_full=True)


@dataclass(frozen=True)
class FailureOutcome:
    """Which limits this failure triggered, for the audit log."""

    account_locked: bool = False
    address_locked: bool = False


def address_key(client_ip: str | None) -> str:
    """The address-limit key for a client address: the address itself, or
    its /64 for IPv6. Anything that isn't an address is used as it is."""
    if not client_ip:
        return _NO_ADDRESS
    try:
        address = ipaddress.ip_address(client_ip)
    except ValueError:
        return client_ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network(f"{address}/{_IPV6_PREFIX}", strict=False))
    return str(address)


def _account_key(username: str, client_ip: str | None, per_address: bool) -> str:
    if per_address:
        return f"{username}\x00{address_key(client_ip)}"
    return username


def _limits() -> tuple[float, int]:
    settings = get_settings()
    return float(settings.login_lockout_seconds), settings.login_max_attempts


def seconds_until_allowed(
    username: str, client_ip: str | None, *, per_address_account: bool = False
) -> float | None:
    """None if this username may attempt a login from this address;
    otherwise the seconds to wait, for a Retry-After header (the longer of
    the two limits when both apply)."""
    window, _ = _limits()
    now = time.monotonic()
    with _lock:
        waits = [
            _accounts.seconds_locked(
                _account_key(username, client_ip, per_address_account), now, window
            ),
            _addresses.seconds_locked(address_key(client_ip), now, window),
        ]
    present = [w for w in waits if w is not None]
    return max(present) if present else None


def record_failure(
    username: str, client_ip: str | None, *, per_address_account: bool = False
) -> FailureOutcome:
    window, max_attempts = _limits()
    now = time.monotonic()
    with _lock:
        account_locked = _accounts.record_failure(
            _account_key(username, client_ip, per_address_account), now, window, max_attempts
        )
        address_locked = _addresses.record_failure(
            address_key(client_ip), now, window, max_attempts
        )
    return FailureOutcome(account_locked=account_locked, address_locked=address_locked)


def record_success(
    username: str, client_ip: str | None, *, per_address_account: bool = False
) -> None:
    """Clears the account's failures. The address's stay (module docstring)."""
    with _lock:
        _accounts.clear(_account_key(username, client_ip, per_address_account))


def reset_for_tests() -> None:
    """Test-only: clears all tracked state between test cases so one test's
    lockout can't bleed into the next."""
    with _lock:
        _accounts.state.clear()
        _addresses.state.clear()
