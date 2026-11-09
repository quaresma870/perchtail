import ipaddress
import socket

import pytest
from app import (
    auth_events,
    login_throttle,
    models,  # noqa: F401  registers tables on SQLModel.metadata
)
from app.auth import models as auth_models  # noqa: F401  registers tables on SQLModel.metadata
from app.name_index_store import ensure_name_index_schema
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

assert models and auth_models

_FAKE_PUBLIC_IP = "93.184.216.34"


def _fake_getaddrinfo(host, *args, **kwargs):
    """Test-only DNS stub for app.webhook_safety's SSRF guard -- keeps
    webhook_url validation hermetic instead of depending on real DNS
    resolution being available (or resolving predictably) in CI. Literal
    IP addresses pass through untouched, so tests that exercise SSRF
    blocking against e.g. 127.0.0.1 or 169.254.169.254 still see the
    real address; any hostname resolves to a fixed public IP."""
    try:
        ipaddress.ip_address(host)
        resolved = host
    except ValueError:
        resolved = _FAKE_PUBLIC_IP
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (resolved, 0))]


@pytest.fixture(autouse=True)
def _stub_dns_for_webhook_safety(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)


@pytest.fixture(autouse=True)
def _allow_local_sources_under_pytest_tmp(monkeypatch, tmp_path_factory):
    # Connector/search/archive tests point non-system local sources at
    # tmp_path; LOCAL_SOURCE_ROOTS (empty by default) would refuse them all.
    # Set both: some fixtures rebuild Settings via get_settings.cache_clear().
    from app.config import get_settings

    root = str(tmp_path_factory.getbasetemp())
    monkeypatch.setenv("LOCAL_SOURCE_ROOTS", root)
    monkeypatch.setattr(get_settings(), "local_source_roots", root)


@pytest.fixture(autouse=True)
def _reset_limits_and_disk_floor(monkeypatch):
    # app.limits keeps its slots in a module-level limiter; the scratch
    # free-space floor (#345) would refuse fetches on a nearly full test
    # disk. Tests that check either set them up themselves.
    from app.config import get_settings
    from app.limits import get_limiter

    monkeypatch.setenv("SCRATCH_MIN_FREE_GB", "0")
    monkeypatch.setenv("SCRATCH_MIN_FREE_PERCENT", "0")
    monkeypatch.setattr(get_settings(), "scratch_min_free_gb", 0.0)
    monkeypatch.setattr(get_settings(), "scratch_min_free_percent", 0.0)
    get_limiter().reset()
    yield
    get_limiter().reset()


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    # app.login_throttle keeps its state in a module-level dict (see its
    # docstring), so one test's failed-login attempts would otherwise bleed
    # into the next test that reuses the same username.
    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()
    yield
    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()


@pytest.fixture(autouse=True)
def _no_source_status_writes(monkeypatch):
    # app/source_status.py writes through app.db.engine, which tests don't
    # point at their database; tests/test_source_status.py turns it on.
    monkeypatch.setattr("app.source_status.ENABLED", False)


@pytest.fixture()
def session():
    # StaticPool: FastAPI's TestClient runs the app in a separate thread, and
    # the default SQLite in-memory pool is thread-affined — without this, the
    # endpoint sees a different, empty in-memory DB than the one this fixture
    # just created tables in ("no such table" errors that only show up when a
    # test goes through a TestClient rather than calling the session directly).
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    ensure_name_index_schema(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture(autouse=True)
def _reset_agent_upgrades():
    # app.agent_upgrades keeps each source's upgrade in a module-level
    # tracker (#319), and writes how one ended through app.db.engine, which
    # tests don't point at their database; tests that check it set their own.
    from app.agent_upgrades import get_agent_upgrades

    upgrades = get_agent_upgrades()
    upgrades.reset()
    upgrades.set_audit_writer(lambda state: None)
    yield
    upgrades.reset()
    upgrades.set_audit_writer(None)
