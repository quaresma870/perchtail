"""Password recovery by email for local accounts (#244).

Off by default. It works only while the system setting
`password_reset_enabled` is on (a super admin's choice, Settings -> Sign-in
security),
email is set up and on (Settings -> Email, app/email_delivery.py) and
PUBLIC_BASE_URL is https, since the link carries a secret.

- Asking for a link (`request_reset`) never says whether an account
  matched: the endpoint answers the same way at once, and the lookup, the
  token and the email happen afterwards, in the background.
- Only an active local account with a password and an email, whose role is
  neither a super admin nor holds any global capability
  (app.auth.mfa_policy.admin_account), can recover its password; this is
  checked when the link is asked for and again when it's used.
- The token is 32 random bytes, sent only in the link's fragment
  (`/#/reset-password/<token>`, which browsers don't send to the server or
  in Referer) and stored only as its SHA-256 (PasswordResetToken). It works
  once, for `password_reset_minutes`, and only while the account's password
  hash and email are still the ones it was issued for, so changing either
  (the user, an admin reset, an email change) or asking for a new link ends
  it.
- Using it sets the new password (with the whole policy, history and "not
  the current one" included), ends every session and opens none: the next
  sign-in still asks for the second factor. It doesn't clear a login
  lockout.

Never logs or audits a token, a link or a password.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app import auth_events, password_policy, profile, system_settings
from app.audit import record_audit_event
from app.auth import mfa_policy
from app.auth.models import AuthProviderType, PasswordResetToken, User
from app.auth.providers.local import check_new_password, set_password
from app.auth.sessions import hash_token, revoke_all_sessions
from app.config import get_settings
from app.email_delivery import (
    EmailDeliveryError,
    EmailNotConfiguredError,
    get_smtp_settings,
    send_email,
)
from app.logging_config import get_logger
from app.password_reset_limits import request_for_identifier
from app.timeutils import utcnow

logger = get_logger(__name__)

ENABLED_SETTING = "password_reset_enabled"
MINUTES_SETTING = "password_reset_minutes"
MIN_MINUTES = 5
MAX_MINUTES = 60
TOKEN_BYTES = 32
MAX_IDENTIFIER = 254

SUBJECT = "Reset your PerchTail password"
EMAIL_CHANGED_SUBJECT = "Your PerchTail email address was changed"

EMAIL_NOT_READY = "Set up email under Settings → Email and turn it on first"
URL_NOT_HTTPS = "PUBLIC_BASE_URL must start with https:// for reset links"


def unavailable_reason(session: Session) -> str | None:
    """Why recovery can't be turned on (or work) right now, or None when it
    can: email must be set up and on, and PUBLIC_BASE_URL https."""
    config = get_smtp_settings(session)
    if config is None or not config.enabled:
        return EMAIL_NOT_READY
    if not get_settings().public_base_url.lower().startswith("https://"):
        return URL_NOT_HTTPS
    return None


def is_active(session: Session) -> bool:
    """Whether recovery works right now: turned on and available."""
    if not system_settings.get_bool(session, ENABLED_SETTING):
        return False
    return unavailable_reason(session) is None


def ineligible_reason(user: User) -> str | None:
    """Why this account can't recover its password by email, or None."""
    if not user.active:
        return "inactive"
    if user.auth_provider != AuthProviderType.local:
        return "sso_account"
    if not user.password_hash:
        return "no_password"
    if not user.email:
        return "no_email"
    if mfa_policy.admin_account(user):
        return "privileged"
    return None


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def reset_link(token: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/#/reset-password/{token}"


def _email_body(link: str, minutes: int) -> str:
    return (
        "Someone asked to reset the password of your PerchTail account.\n"
        "\n"
        f"To choose a new password, open this link within {minutes} minutes:\n"
        "\n"
        f"{link}\n"
        "\n"
        "The link works once. If you didn't ask for this, ignore this email: "
        "your password stays as it is.\n"
    )


def valid_identifier(raw: str) -> str | None:
    """The identifier with surrounding spaces removed, or None when it can't
    be a username or an email (empty, too long, control characters)."""
    identifier = raw.strip()
    if not identifier or len(identifier) > MAX_IDENTIFIER:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in identifier):
        return None
    return identifier


def _find_account(session: Session, identifier: str) -> User | None:
    user = session.exec(select(User).where(User.username == identifier)).first()
    if user is not None:
        return user
    try:
        email = profile.normalize_email(identifier)
    except profile.ProfileError:
        return None
    if email is None:
        return None
    return session.exec(select(User).where(User.email == email)).first()


def _delete_tokens(session: Session, user_id: int) -> None:
    session.exec(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id))


def request_reset(bind: Engine, identifier: str, client_ip: str | None) -> None:
    """The background half of asking for a link: finds the account, applies
    the per-account limit, replaces any earlier link with a new one, sends
    it and audits the outcome. Runs after the response, in its own database
    session; never raises."""
    try:
        with Session(bind) as session:
            _request_reset(session, identifier, client_ip)
    except Exception:  # noqa: BLE001 - nothing to answer: logged, never the token
        logger.exception("password_reset.request_failed")


def _request_reset(session: Session, identifier: str, client_ip: str | None) -> None:
    if not is_active(session):
        return
    user = _find_account(session, identifier)
    limit_key = f"user:{user.id}" if user is not None else f"id:{identifier.lower()}"
    if request_for_identifier(limit_key) is not None:
        auth_events.password_reset_requested(
            session,
            client_ip=client_ip,
            result="rate_limited",
            user=user,
            identifier=identifier if user is None else None,
        )
        return
    if user is None:
        auth_events.password_reset_requested(
            session, client_ip=client_ip, result="no_match", identifier=identifier
        )
        return
    reason = ineligible_reason(user)
    if reason is not None:
        auth_events.password_reset_requested(
            session, client_ip=client_ip, result="not_eligible", user=user, reason=reason
        )
        return

    minutes = system_settings.get_int(session, MINUTES_SETTING)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    _delete_tokens(session, user.id)
    now = utcnow()
    row = PasswordResetToken(
        user_id=user.id,
        token_hash=hash_token(token),
        password_fingerprint=_fingerprint(user.password_hash),
        email_fingerprint=_fingerprint(user.email),
        created_at=now,
        expires_at=now + timedelta(minutes=minutes),
    )
    session.add(row)
    # Committed before sending, so the link works as soon as it arrives.
    session.commit()

    try:
        send_email(session, user.email, SUBJECT, _email_body(reset_link(token), minutes))
    except (EmailNotConfiguredError, EmailDeliveryError) as exc:
        session.delete(row)
        session.commit()
        auth_events.password_reset_requested(
            session, client_ip=client_ip, result="send_failed", user=user, reason=str(exc)
        )
        return
    auth_events.password_reset_requested(session, client_ip=client_ip, result="sent", user=user)


@dataclass
class TokenCheck:
    """The outcome of looking a token up: the account when it may be used,
    otherwise the reason (for the audit log) and, when known, whose it was."""

    user: User | None
    row: PasswordResetToken | None
    failure: str | None


def check_token(session: Session, token: str) -> TokenCheck:
    """Whether `token` can be used now. A token found but no longer usable
    (expired, superseded, the account no longer eligible) is deleted; the
    caller commits."""
    row = session.exec(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(token))
    ).first()
    if row is None:
        return TokenCheck(user=None, row=None, failure="invalid_token")
    user = session.get(User, row.user_id)
    failure = None
    if user is None:
        failure = "invalid_token"
    elif row.expires_at <= utcnow():
        failure = "expired"
    elif (
        not user.password_hash
        or not user.email
        or not secrets.compare_digest(row.password_fingerprint, _fingerprint(user.password_hash))
        or not secrets.compare_digest(row.email_fingerprint, _fingerprint(user.email))
    ):
        failure = "superseded"
    elif ineligible_reason(user) is not None:
        failure = "not_eligible"
    if failure is not None:
        session.delete(row)
        return TokenCheck(user=user, row=None, failure=failure)
    return TokenCheck(user=user, row=row, failure=None)


class TokenAlreadyUsedError(Exception):
    """Another request used the same link first."""


def complete_reset(
    session: Session, user: User, row: PasswordResetToken, new_password: str
) -> None:
    """Sets the new password through a checked token, in one transaction
    with deleting the account's links and ending all of its sessions.
    Raises PasswordPolicyError, or TokenAlreadyUsedError, before changing
    anything. Commits."""
    policy = password_policy.get_policy(session)
    check_new_password(session, user, new_password, policy)
    # The link is spent first: of two requests racing with it, only the one
    # that deletes its row goes on.
    consumed = session.exec(delete(PasswordResetToken).where(PasswordResetToken.id == row.id))
    if consumed.rowcount != 1:
        raise TokenAlreadyUsedError
    set_password(session, user, new_password, policy)
    user.must_change_password = False
    session.add(user)
    _delete_tokens(session, user.id)
    revoked = revoke_all_sessions(session, user.id)
    record_audit_event(
        session,
        user_id=user.id,
        action="user.password_reset_completed",
        target_type="user",
        target_id=user.id,
    )
    if revoked:
        record_audit_event(
            session,
            user_id=user.id,
            action="session.revoke",
            target_type="user",
            target_id=user.id,
            metadata={"count": revoked, "reason": "password_reset"},
        )
    session.commit()


def purge_expired_tokens(session: Session) -> int:
    """Deletes links past their expiry. Commits."""
    result = session.exec(
        delete(PasswordResetToken).where(PasswordResetToken.expires_at <= utcnow())
    )
    session.commit()
    return result.rowcount or 0


def notify_email_changed(bind: Engine, user_id: int, old_email: str) -> None:
    """Tells the previous address that the account's email was changed,
    when email is set up and on. Runs after the response; never raises.
    Audited with the outcome, never the message."""
    body = (
        "The email address of your PerchTail account was just changed, and this address "
        "no longer receives its messages.\n"
        "\n"
        "If you didn't do this, contact your PerchTail administrator.\n"
    )
    try:
        with Session(bind) as session:
            config = get_smtp_settings(session)
            if config is None or not config.enabled:
                return
            try:
                send_email(session, old_email, EMAIL_CHANGED_SUBJECT, body)
            except (EmailNotConfiguredError, EmailDeliveryError) as exc:
                outcome = {"result": "send_failed", "error": str(exc)}
            else:
                outcome = {"result": "sent"}
            record_audit_event(
                session,
                user_id=user_id,
                action="user.email_change_notice",
                target_type="user",
                target_id=user_id,
                metadata={"recipient": old_email, **outcome},
            )
            session.commit()
    except Exception:  # noqa: BLE001 - nothing to answer: logged
        logger.exception("profile.email_change_notice_failed")
