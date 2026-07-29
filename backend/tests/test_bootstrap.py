from app.auth.models import Capability, Role, User
from app.auth.rbac import resolve_capability
from app.bootstrap import SYSTEM_SOURCE_NAME, seed_system_log_source
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
