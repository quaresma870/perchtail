from app.auth.models import AuditLog, Role
from app.auth.providers.local import (
    LocalPasswordProvider,
    change_password,
    create_local_user,
    hash_password,
)
from sqlmodel import select


def _make_role(session) -> Role:
    role = Role(name="Support")
    session.add(role)
    session.commit()
    session.refresh(role)
    return role


def test_create_local_user_forces_password_change_and_audits(session):
    role = _make_role(session)
    user = create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )

    assert user.must_change_password is True
    assert user.password_hash != "s3cret!"

    actions = {e.action for e in session.exec(select(AuditLog)).all()}
    assert "user.create" in actions


def test_authenticate_success_updates_last_login_and_audits(session):
    role = _make_role(session)
    create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )

    provider = LocalPasswordProvider()
    authenticated = provider.authenticate(session, "jdoe@example.com", "s3cret!")

    assert authenticated is not None
    assert authenticated.last_login_at is not None

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert actions.count("user.login") == 1


def test_authenticate_wrong_password_returns_none(session):
    role = _make_role(session)
    create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )

    provider = LocalPasswordProvider()
    assert provider.authenticate(session, "jdoe@example.com", "wrong-password") is None


def test_authenticate_unknown_username_returns_none(session):
    provider = LocalPasswordProvider()
    assert provider.authenticate(session, "nobody@example.com", "whatever") is None


def test_authenticate_inactive_user_returns_none(session):
    role = _make_role(session)
    user = create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )
    user.active = False
    session.add(user)
    session.commit()

    provider = LocalPasswordProvider()
    assert provider.authenticate(session, "jdoe@example.com", "s3cret!") is None


def test_change_password_clears_must_change_flag_and_audits(session):
    role = _make_role(session)
    user = create_local_user(
        session,
        actor_user_id=None,
        username="jdoe@example.com",
        password="s3cret!",
        role_id=role.id,
    )
    assert user.must_change_password is True

    change_password(session, user, "new-password!")
    session.refresh(user)

    assert user.must_change_password is False

    provider = LocalPasswordProvider()
    assert provider.authenticate(session, "jdoe@example.com", "s3cret!") is None
    assert provider.authenticate(session, "jdoe@example.com", "new-password!") is not None

    actions = [e.action for e in session.exec(select(AuditLog)).all()]
    assert "user.change_password" in actions


def test_hash_password_produces_argon2_hash():
    hashed = hash_password("s3cret!")
    assert hashed.startswith("$argon2")
    assert hashed != "s3cret!"
