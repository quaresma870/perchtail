"""A user's display name and email (#243): validation and the uniqueness
check, shared by My profile (PATCH /auth/me), the Users admin API and the
SSO claim sync.

Emails are stored lowercased, so uniqueness is case-insensitive. No
delivery check: there's no SMTP yet, so the email isn't verified, only
well-formed."""

import re
import unicodedata

from sqlmodel import Session, select

from app.audit import record_audit_event
from app.auth.models import User
from app.logging_config import get_logger

logger = get_logger(__name__)

MAX_DISPLAY_NAME = 100
MAX_EMAIL = 254  # RFC 5321's limit for a forward path
# Deliberately simple: one @, something on each side, a dot in the domain,
# no spaces. The real test of an email is delivering to it.
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s.]+$")


class ProfileError(ValueError):
    """A value for the profile that's refused; str() is the message."""


class EmailTakenError(ProfileError):
    """The email belongs to another account (a 409, not a 422)."""


def normalize_display_name(value: str | None) -> str | None:
    """Trimmed, inner whitespace collapsed; None when empty. Refuses
    control characters and anything over MAX_DISPLAY_NAME."""
    if value is None:
        return None
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise ProfileError("The name can't contain control characters.")
    cleaned = " ".join(value.split())
    if len(cleaned) > MAX_DISPLAY_NAME:
        raise ProfileError(f"The name can be at most {MAX_DISPLAY_NAME} characters.")
    return cleaned or None


def normalize_email(value: str | None) -> str | None:
    """Trimmed and lowercased; None when empty. Refuses anything that
    isn't shaped like an address."""
    if value is None:
        return None
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if len(cleaned) > MAX_EMAIL or not _EMAIL.match(cleaned):
        raise ProfileError("Enter a valid email address.")
    return cleaned


def email_taken(session: Session, email: str, *, except_user_id: int | None = None) -> bool:
    query = select(User.id).where(User.email == email)
    if except_user_id is not None:
        query = query.where(User.id != except_user_id)
    return session.exec(query).first() is not None


def check_email_available(session: Session, email: str | None, *, user_id: int | None) -> None:
    if email is not None and email_taken(session, email, except_user_id=user_id):
        raise EmailTakenError("That email address is already used by another account.")


def apply_profile(session: Session, user: User, *, actor_id: int | None, changes: dict) -> None:
    """Validates and sets display_name and/or email (only the keys present
    in `changes`), auditing each real change: user.profile_update for the
    name, user.email_change for the email. Raises ProfileError before
    changing anything. Doesn't commit."""
    name = user.display_name
    email = user.email
    if "display_name" in changes:
        name = normalize_display_name(changes["display_name"])
    if "email" in changes:
        email = normalize_email(changes["email"])
        check_email_available(session, email, user_id=user.id)

    if name != user.display_name:
        user.display_name = name
        record_audit_event(
            session,
            user_id=actor_id,
            action="user.profile_update",
            target_type="user",
            target_id=user.id,
            metadata={"display_name": name},
        )
    if email != user.email:
        # Once SMTP exists, the old address is told about this (#243).
        record_audit_event(
            session,
            user_id=actor_id,
            action="user.email_change",
            target_type="user",
            target_id=user.id,
            metadata={"old_email": user.email, "new_email": email},
        )
        user.email = email
    session.add(user)


def sync_from_claims(session: Session, user: User, claims: dict) -> None:
    """An SSO account's name and email, from the ID token on each sign-in.
    A malformed value, or an email another account already has, is left
    out (and logged) rather than failing the sign-in. Doesn't commit."""
    changes: dict = {}
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        parts = [claims.get("given_name"), claims.get("family_name")]
        name = " ".join(part for part in parts if isinstance(part, str) and part.strip())
    if name:
        try:
            changes["display_name"] = normalize_display_name(name)
        except ProfileError:
            logger.warning("sso.profile_name_ignored", user_id=user.id)
    email = claims.get("email")
    # An IdP that says the address isn't verified doesn't get to set it.
    # Some IdPs send the flag as a string.
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.strip().lower() != "false"
    if isinstance(email, str) and verified is not False:
        try:
            normalized = normalize_email(email)
            check_email_available(session, normalized, user_id=user.id)
            changes["email"] = normalized
        except ProfileError as exc:
            logger.warning("sso.profile_email_ignored", user_id=user.id, reason=str(exc))
    if changes:
        apply_profile(session, user, actor_id=user.id, changes=changes)
