import hashlib
import secrets
from datetime import timedelta

from sqlmodel import Session, select

from app.auth.models import AuthSession, User
from app.config import get_settings
from app.timeutils import utcnow


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(session: Session, user: User) -> tuple[AuthSession, str]:
    """Returns the AuthSession row and the raw token — the raw value is only
    ever handed to the caller (for the cookie), never persisted."""
    token = secrets.token_urlsafe(32)
    ttl = timedelta(hours=get_settings().session_ttl_hours)
    auth_session = AuthSession(
        token_hash=_hash_token(token),
        user_id=user.id,
        expires_at=utcnow() + ttl,
    )
    session.add(auth_session)
    session.commit()
    session.refresh(auth_session)
    return auth_session, token


def get_user_by_token(session: Session, token: str) -> User | None:
    auth_session = session.exec(
        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
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
        select(AuthSession).where(AuthSession.token_hash == _hash_token(token))
    ).first()
    if auth_session is not None:
        session.delete(auth_session)
        session.commit()
