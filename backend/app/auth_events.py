"""Failed logins, lockouts and attempts while locked out, in the audit log
and the structured app log (#171). Successful logins were already audited
(`user.login`); failures left no trace, which is the opposite of what an
incident responder needs to spot brute force, password spraying or a
deliberate lockout.

Never records the password or MFA code. Repeated identical failures (same
action, username, client IP and reason) within AGGREGATE_SECONDS are
counted instead of written one row each, so a spray can't flood the audit
log and its hash chain; the next row written for that key carries how many
were folded into it."""

import threading
import time

from sqlmodel import Session, select

from app.audit import record_audit_event
from app.auth.models import User
from app.logging_config import get_logger

logger = get_logger(__name__)

AGGREGATE_SECONDS = 60.0

_lock = threading.Lock()
# key -> (monotonic time of the last row written, failures folded since)
_recent: dict[tuple[str, str | None, str | None, str], tuple[float, int]] = {}


def _user_id(session: Session, username: str | None) -> int | None:
    if username is None:
        return None
    user = session.exec(select(User).where(User.username == username)).first()
    return user.id if user is not None else None


def _record(
    session: Session,
    action: str,
    *,
    username: str | None,
    client_ip: str | None,
    reason: str,
    extra: dict | None = None,
) -> None:
    # An account lockout and an address lockout from the same failure are
    # two events, not one repeated.
    scope = (extra or {}).get("scope")
    key = (action, username, client_ip, f"{reason}:{scope}" if scope else reason)
    now = time.monotonic()
    with _lock:
        last = _recent.get(key)
        if last is not None and now - last[0] < AGGREGATE_SECONDS:
            _recent[key] = (last[0], last[1] + 1)
            logger.warning(
                action, username=username, client_ip=client_ip, reason=reason, aggregated=True
            )
            return
        folded = last[1] if last is not None else 0
        _recent[key] = (now, 0)
        if len(_recent) > 10_000:
            # Bounded: drop entries whose window has passed.
            for stale in [k for k, (t, _) in _recent.items() if now - t >= AGGREGATE_SECONDS]:
                del _recent[stale]

    metadata = {"client_ip": client_ip, "reason": reason, **(extra or {})}
    if username is not None:
        metadata = {"username": username, **metadata}
    if folded:
        metadata["similar_events_folded"] = folded
    record_audit_event(
        session,
        user_id=_user_id(session, username),
        action=action,
        target_type="user",
        metadata=metadata,
    )
    session.commit()


def login_failed(session: Session, *, username: str, client_ip: str | None, reason: str) -> None:
    _record(session, "user.login_failed", username=username, client_ip=client_ip, reason=reason)


def locked_out(
    session: Session,
    *,
    username: str,
    client_ip: str | None,
    lock_seconds: float,
    scope: str = "account",
) -> None:
    """`scope` says what was locked (app/login_throttle.py): `account` (the
    username, from every address), `account_and_address` (an emergency
    account, from this address only) or `address` (every username from this
    address)."""
    _record(
        session,
        "user.locked_out",
        username=username,
        client_ip=client_ip,
        reason="too_many_failures",
        extra={"lock_seconds": round(lock_seconds), "scope": scope},
    )


def login_throttled(session: Session, *, username: str, client_ip: str | None) -> None:
    _record(
        session, "user.login_throttled", username=username, client_ip=client_ip, reason="locked"
    )


def sso_login_failed(
    session: Session, *, client_ip: str | None, reason: str, error: str | None = None
) -> None:
    """A sign-in through the identity provider that didn't end in a session.
    The callback is reachable without signing in, so it's folded like the
    failures above. `error` is the provider's own error code or the kind of
    failure, never a token or a claim."""
    extra = {"error": error[:100]} if error else None
    _record(
        session,
        "user.sso_login_failed",
        username=None,
        client_ip=client_ip,
        reason=reason,
        extra=extra,
    )


def reset_for_tests() -> None:
    with _lock:
        _recent.clear()
