from app.auth.models import Capability, Role, User
from app.auth.providers.local import verify_password
from app.auth.rbac import resolve_capability
from app.bootstrap import (
    SUPER_ADMIN_ROLE_NAME,
    SYSTEM_SOURCE_NAME,
    seed_initial_super_admin,
    seed_system_log_source,
)
from app.models import Rule, Source
from sqlmodel import select


def test_seed_system_log_source_creates_source_and_rule(session, monkeypatch, tmp_path):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()

    source = seed_system_log_source(session)

    assert source.name == SYSTEM_SOURCE_NAME
    assert source.is_system is True
    assert source.customer_id is None
    assert source.folder_id is None
    assert source.base_path == str(tmp_path)

    rules = session.exec(select(Rule).where(Rule.source_id == source.id)).all()
    assert len(rules) == 1
    assert rules[0].pattern == "**/*"

    get_settings.cache_clear()


def test_seed_system_log_source_is_idempotent(session):
    first = seed_system_log_source(session)
    second = seed_system_log_source(session)

    assert first.id == second.id
    assert len(session.exec(select(Source).where(Source.is_system.is_(True))).all()) == 1
    assert len(session.exec(select(Rule).where(Rule.source_id == first.id)).all()) == 1


def test_system_source_stays_super_admin_only_after_seeding(session):
    source = seed_system_log_source(session)

    regular_role = Role(name="Support", is_super_admin=False)
    session.add(regular_role)
    session.commit()
    session.refresh(regular_role)
    regular_user = User(username="support@example.com", role_id=regular_role.id)
    session.add(regular_user)
    session.commit()
    session.refresh(regular_user)

    admin_role = Role(name="Super Admin", is_super_admin=True)
    session.add(admin_role)
    session.commit()
    session.refresh(admin_role)
    admin_user = User(username="admin@example.com", role_id=admin_role.id)
    session.add(admin_user)
    session.commit()
    session.refresh(admin_user)

    assert resolve_capability(session, regular_user, source, Capability.view) is False
    assert resolve_capability(session, admin_user, source, Capability.view) is True


def test_seed_initial_super_admin_creates_user_on_empty_db(session):
    user = seed_initial_super_admin(session)

    assert user is not None
    assert user.username == "admin"
    assert user.must_change_password is True

    role = session.get(Role, user.role_id)
    assert role.name == SUPER_ADMIN_ROLE_NAME
    assert role.is_super_admin is True
    assert role.is_builtin is True


def test_seed_initial_super_admin_password_is_usable(session):
    from unittest.mock import patch

    with patch("app.bootstrap.secrets.token_urlsafe", return_value="fixed-temp-password"):
        user = seed_initial_super_admin(session)

    assert verify_password(user, "fixed-temp-password") is True


def test_seed_initial_super_admin_is_noop_once_any_user_exists(session):
    role = Role(name="Some Role")
    session.add(role)
    session.commit()
    session.refresh(role)
    session.add(User(username="someone@example.com", role_id=role.id))
    session.commit()

    result = seed_initial_super_admin(session)

    assert result is None
    assert len(session.exec(select(User)).all()) == 1


def test_seed_initial_super_admin_respects_custom_username(session, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("INITIAL_ADMIN_USERNAME", "root")
    get_settings.cache_clear()

    user = seed_initial_super_admin(session)
    assert user.username == "root"

    get_settings.cache_clear()
