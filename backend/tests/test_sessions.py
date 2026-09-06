from datetime import timedelta

from app.auth.models import AuthSession, Role, User
from app.auth.providers.local import create_local_user
from app.auth.sessions import (
    create_session,
    delete_session,
    get_user_by_token,
    list_sessions_for_user,
    revoke_session,
)
from app.timeutils import utcnow
from sqlmodel import select


def _make_user(session) -> User:
    role = Role(name="Support")
    session.add(role)
    session.commit()
    session.refresh(role)
    return create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )


def test_create_session_roundtrips_and_hashes_the_token(session):
    user = _make_user(session)
    auth_session, token = create_session(session, user)

    assert auth_session.token_hash != token
    assert auth_session.expires_at > utcnow()


def test_get_user_by_token_returns_the_user(session):
    user = _make_user(session)
    _, token = create_session(session, user)

    resolved = get_user_by_token(session, token)
    assert resolved is not None
    assert resolved.id == user.id


def test_get_user_by_token_rejects_unknown_token(session):
    assert get_user_by_token(session, "not-a-real-token") is None


def test_get_user_by_token_rejects_expired_session(session):
    user = _make_user(session)
    auth_session, token = create_session(session, user)

    auth_session.expires_at = utcnow() - timedelta(seconds=1)
    session.add(auth_session)
    session.commit()

    assert get_user_by_token(session, token) is None


def test_get_user_by_token_rejects_inactive_user(session):
    user = _make_user(session)
    _, token = create_session(session, user)

    user.active = False
    session.add(user)
    session.commit()

    assert get_user_by_token(session, token) is None


def test_get_user_by_token_updates_last_seen_at(session):
    user = _make_user(session)
    auth_session, token = create_session(session, user)
    assert auth_session.last_seen_at is None

    get_user_by_token(session, token)

    session.refresh(auth_session)
    assert auth_session.last_seen_at is not None


def test_delete_session_revokes_the_token(session):
    user = _make_user(session)
    _, token = create_session(session, user)
    assert get_user_by_token(session, token) is not None

    delete_session(session, token)
    assert get_user_by_token(session, token) is None


def test_delete_session_is_idempotent(session):
    delete_session(session, "never-existed")


def test_two_sessions_for_the_same_user_are_independent(session):
    user = _make_user(session)
    _, token_a = create_session(session, user)
    _, token_b = create_session(session, user)

    assert get_user_by_token(session, token_a) is not None
    assert len(session.exec(select(AuthSession)).all()) == 2

    delete_session(session, token_a)
    assert get_user_by_token(session, token_a) is None
    assert get_user_by_token(session, token_b) is not None


def test_create_session_persists_the_user_agent(session):
    user = _make_user(session)
    auth_session, _ = create_session(session, user, user_agent="Mozilla/5.0 (test)")

    assert auth_session.user_agent == "Mozilla/5.0 (test)"


def test_create_session_user_agent_defaults_to_none(session):
    user = _make_user(session)
    auth_session, _ = create_session(session, user)

    assert auth_session.user_agent is None


def test_list_sessions_for_user_returns_only_that_users_sessions(session):
    user_a = _make_user(session)
    role = Role(name="Other")
    session.add(role)
    session.commit()
    session.refresh(role)
    user_b = create_local_user(
        session, actor_user_id=None, username="other@example.com", password="x", role_id=role.id
    )

    create_session(session, user_a)
    create_session(session, user_a)
    create_session(session, user_b)

    assert len(list_sessions_for_user(session, user_a.id)) == 2
    assert len(list_sessions_for_user(session, user_b.id)) == 1


def test_list_sessions_for_user_excludes_expired_sessions(session):
    user = _make_user(session)
    auth_session, _ = create_session(session, user)
    auth_session.expires_at = utcnow() - timedelta(seconds=1)
    session.add(auth_session)
    session.commit()

    assert list_sessions_for_user(session, user.id) == []


def test_list_sessions_for_user_orders_most_recently_active_first(session):
    user = _make_user(session)
    older, _ = create_session(session, user)
    newer, _ = create_session(session, user)
    older.last_seen_at = utcnow() - timedelta(hours=1)
    newer.last_seen_at = utcnow()
    session.add(older)
    session.add(newer)
    session.commit()

    result = list_sessions_for_user(session, user.id)
    assert [s.id for s in result] == [newer.id, older.id]


def test_revoke_session_deletes_a_session_owned_by_the_user(session):
    user = _make_user(session)
    auth_session, token = create_session(session, user)

    assert revoke_session(session, user_id=user.id, session_id=auth_session.id) is True
    assert get_user_by_token(session, token) is None


def test_revoke_session_refuses_a_session_owned_by_someone_else(session):
    user_a = _make_user(session)
    role = Role(name="Other")
    session.add(role)
    session.commit()
    session.refresh(role)
    user_b = create_local_user(
        session, actor_user_id=None, username="other@example.com", password="x", role_id=role.id
    )
    auth_session, token = create_session(session, user_b)

    assert revoke_session(session, user_id=user_a.id, session_id=auth_session.id) is False
    assert get_user_by_token(session, token) is not None


def test_revoke_session_returns_false_for_an_unknown_id(session):
    user = _make_user(session)
    assert revoke_session(session, user_id=user.id, session_id=999999) is False
