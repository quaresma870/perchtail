import atexit
import ipaddress
import os
import shutil
import socket
import tempfile


def _isolate_state_dirs() -> None:
    """Every state path PerchTail derives from DATA_DIR, LOG_DIR, SCRATCH_DIR
    and BACKUP_DIR points into a folder of this test run, set before any
    `app` module builds its settings (app.db opens its engine at import).
    The defaults are relative folders (./data/...) that every run from the
    same checkout would share, and whatever the shell has set could be a
    real installation's."""
    root = tempfile.mkdtemp(prefix="perchtail-tests-")
    atexit.register(shutil.rmtree, root, ignore_errors=True)
    for name, sub in (
        ("DATA_DIR", "db"),
        ("LOG_DIR", "logs"),
        ("SCRATCH_DIR", "scratch"),
        ("BACKUP_DIR", "backups"),
    ):
        os.environ[name] = os.path.join(root, sub)
    for name in ("DATABASE_URL", "CREDENTIAL_SALT_PATH", "SSH_KNOWN_HOSTS_PATH"):
        os.environ.pop(name, None)


_isolate_state_dirs()

import pytest  # noqa: E402
from app import (  # noqa: E402
    auth_events,
    login_throttle,
    models,  # noqa: F401  registers tables on SQLModel.metadata
    request_context,
)
from app.auth import models as auth_models  # noqa: E402, F401  registers tables
from app.name_index_store import ensure_name_index_schema  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

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
    # The same for app.request_context's untrusted-forwarded-header flag.
    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()
    request_context.reset_for_tests()
    yield
    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()
    request_context.reset_for_tests()


@pytest.fixture(autouse=True)
def _own_audit_chain_marker(monkeypatch, tmp_path):
    # The audit chain marker lives in DATA_DIR (app.audit_hash_chain), which
    # tests share: each test gets its own, absent unless the test writes it.
    from app import audit_hash_chain

    original = audit_hash_chain.chain_marker_path
    monkeypatch.setattr(
        audit_hash_chain,
        "chain_marker_path",
        lambda data_dir=None: original(tmp_path if data_dir is None else data_dir),
    )


@pytest.fixture(autouse=True)
def _mfa_not_enforced(monkeypatch):
    # Admin accounts must use MFA (app/auth/mfa_policy.py); most tests sign
    # in as one without it. Tests of the requirement turn it back on.
    monkeypatch.setattr("app.auth.mfa_policy.ENFORCED", False)


@pytest.fixture(autouse=True)
def _initial_admin_password_in_tmp(monkeypatch, tmp_path):
    # The first-run password file goes to DATA_DIR, which tests share.
    monkeypatch.setattr(
        "app.initial_admin_password.initial_admin_password_path",
        lambda: tmp_path / "initial-admin-password",
    )


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
