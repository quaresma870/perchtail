import json

from app.auth.models import (
    AuthProviderType,
    Capability,
    Role,
    SSOProtocol,
    SSOProviderConfig,
    User,
)
from app.auth.providers.local import verify_password
from app.auth.rbac import resolve_capability
from app.bootstrap import (
    SUPER_ADMIN_ROLE_NAME,
    SYSTEM_SOURCE_NAME,
    backfill_sso_user_issuers,
    seed_initial_super_admin,
    seed_system_log_source,
)
from app.crypto import encrypt_secret
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


def _oidc_provider(session, issuer: str, *, enabled: bool) -> None:
    config = {"issuer": issuer, "client_id": "c", "client_secret": "s"}
    session.add(
        SSOProviderConfig(
            protocol=SSOProtocol.oidc,
            name=issuer,
            config=encrypt_secret(json.dumps(config)),
            enabled=enabled,
        )
    )
    session.commit()


def _unbound_sso_user(session) -> User:
    role = Role(name="r")
    session.add(role)
    session.commit()
    user = User(
        username="sso@example.com",
        role_id=role.id,
        auth_provider=AuthProviderType.oidc,
        external_id="sub-1",
    )
    session.add(user)
    session.commit()
    return user


def test_backfill_binds_existing_sso_users_to_the_enabled_providers_issuer(session):
    user = _unbound_sso_user(session)
    _oidc_provider(session, "https://old.example.com", enabled=False)
    _oidc_provider(session, "https://idp.example.com", enabled=True)

    assert backfill_sso_user_issuers(session) == 1

    session.refresh(user)
    assert user.external_issuer == "https://idp.example.com"


def test_backfill_uses_the_only_provider_even_when_disabled(session):
    user = _unbound_sso_user(session)
    _oidc_provider(session, "https://idp.example.com", enabled=False)

    assert backfill_sso_user_issuers(session) == 1

    session.refresh(user)
    assert user.external_issuer == "https://idp.example.com"


def test_backfill_leaves_users_unbound_when_the_issuer_is_ambiguous(session):
    user = _unbound_sso_user(session)
    _oidc_provider(session, "https://a.example.com", enabled=False)
    _oidc_provider(session, "https://b.example.com", enabled=False)

    assert backfill_sso_user_issuers(session) == 0

    session.refresh(user)
    assert user.external_issuer is None


def test_backfill_leaves_local_accounts_alone(session):
    role = Role(name="r")
    session.add(role)
    session.commit()
    local = User(username="admin", role_id=role.id)
    session.add(local)
    session.commit()
    _oidc_provider(session, "https://idp.example.com", enabled=True)

    assert backfill_sso_user_issuers(session) == 0
    session.refresh(local)
    assert local.external_issuer is None
