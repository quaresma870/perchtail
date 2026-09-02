import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session, select

from app.auth.models import AuthSession, User
from app.config import get_settings
from app.timeutils import utcnow


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    session: Session, user: User, *, user_agent: str | None = None
) -> tuple[AuthSession, str]:
    """Returns the AuthSession row and the raw token — the raw value is only
    ever handed to the caller (for the cookie), never persisted."""
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    auth_session = AuthSession(
        token_hash=hash_token(token),
        user_id=user.id,
        expires_at=utcnow() + ttl,
        user_agent=user_agent,
    )
    session.add(auth_session)
    session.commit()
    session.refresh(auth_session)
    return auth_session, token


def get_user_by_token(session: Session, token: str) -> User | None:
    auth_session = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if auth_session is None:
        return None

    if auth_session.expires_at < utcnow():
        return None

    user = session.get(User, auth_session.user_id)
    if user is None or not user.active:
        return None

    auth_session.last_seen_at = utcnow()
    session.add(auth_session)
    session.commit()
    return user


def delete_session(session: Session, token: str) -> None:
    auth_session = session.exec(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).first()
    if auth_session is not None:
        session.delete(auth_session)
        session.commit()


def list_sessions_for_user(session: Session, user_id: int) -> list[AuthSession]:
    """Only ever the caller's own sessions -- see api/auth.py's
    GET /auth/sessions, which is self-service (no admin capability lets one
    user see or revoke another's), same scope as change-password."""
    return list(
        session.exec(
            select(AuthSession)
            .where(AuthSession.user_id == user_id, AuthSession.expires_at > utcnow())
            .order_by(AuthSession.last_seen_at.desc().nulls_last(), AuthSession.created_at.desc())
        ).all()
    )


def revoke_session(session: Session, *, user_id: int, session_id: int) -> bool:
    """True if a session owned by user_id with this id was found and
    deleted; False if not (either it doesn't exist, already expired, or
    belongs to someone else -- the caller shouldn't be able to tell which,
    same 404-for-both reasoning as every other ownership-scoped lookup in
    this codebase, e.g. api/alerts.py's _get_owned_alert)."""
    auth_session = session.get(AuthSession, session_id)
    if auth_session is None or auth_session.user_id != user_id:
        return False
    session.delete(auth_session)
    session.commit()
    return True
