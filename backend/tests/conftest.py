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
    for name in (
        "DATABASE_URL",
        "DATABASE_URL_FILE",
        "CREDENTIAL_SALT_PATH",
        "SSH_KNOWN_HOSTS_PATH",
    ):
        os.environ.pop(name, None)
    # TEST_DATABASE_URL (a PostgreSQL database, #306) is where app.db's
    # engine points too, for the tests that go through it.
    if os.environ.get("TEST_DATABASE_URL"):
        os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]


_isolate_state_dirs()

import uuid  # noqa: E402

import pytest  # noqa: E402
from app import (  # noqa: E402
    auth_events,
    login_throttle,
    models,  # noqa: F401  registers tables on SQLModel.metadata
    request_context,
)
from app.auth import models as auth_models  # noqa: E402, F401  registers tables
from app.config import normalize_database_url  # noqa: E402
from app.name_index_store import ensure_name_index_schema  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.pool import NullPool, StaticPool  # noqa: E402
from sqlmodel import Session, SQLModel, create_engine  # noqa: E402

assert models and auth_models

# Set: the suite runs on this PostgreSQL database instead of SQLite in
# memory. Its role needs CREATEDB for the tests that migrate a database of
# their own (empty_postgres_url).
TEST_DATABASE_URL = normalize_database_url(os.environ.get("TEST_DATABASE_URL", ""))


def pytest_configure(config):
    config.addinivalue_line("markers", "sqlite_only: runs only without TEST_DATABASE_URL")
    config.addinivalue_line("markers", "postgres_only: runs only with TEST_DATABASE_URL")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if TEST_DATABASE_URL and item.get_closest_marker("sqlite_only"):
            item.add_marker(pytest.mark.skip(reason="SQLite only; TEST_DATABASE_URL is set"))
        if not TEST_DATABASE_URL and item.get_closest_marker("postgres_only"):
            item.add_marker(pytest.mark.skip(reason="PostgreSQL only; set TEST_DATABASE_URL"))


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
def _reset_follow_audits():
    # app.api.archive remembers which Tails it has audited in a module-level
    # object; ids and paths repeat from one test to the next.
    from app.api.archive import _follow_audits

    _follow_audits.clear()
    yield
    _follow_audits.clear()


@pytest.fixture(autouse=True)
def _reset_login_throttle():
    # app.login_throttle keeps its state in a module-level dict (see its
    # docstring), so one test's failed-login attempts would otherwise bleed
    # into the next test that reuses the same username.
    # The same for app.request_context's untrusted-forwarded-header flag.
    # The same for the per-user limit on webhook tests (app/api/alerts.py).
    from app.api.alerts import reset_test_limit_for_tests

    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()
    request_context.reset_for_tests()
    reset_test_limit_for_tests()
    yield
    login_throttle.reset_for_tests()
    auth_events.reset_for_tests()
    request_context.reset_for_tests()
    reset_test_limit_for_tests()


@pytest.fixture(autouse=True)
def _empty_sso_discovery_cache():
    # app.auth.providers.oidc caches discovery documents per issuer in a
    # module-level dict; tests serve different documents for one issuer.
    from app.auth.providers import oidc

    oidc.clear_discovery_cache()
    yield
    oidc.clear_discovery_cache()


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


_postgres_engine = None


def _test_postgres_engine():
    """The engine on TEST_DATABASE_URL, its schema created once per run from
    the models on an emptied public schema."""
    global _postgres_engine
    if _postgres_engine is None:
        engine = create_engine(TEST_DATABASE_URL)
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
        SQLModel.metadata.create_all(engine)
        ensure_name_index_schema(engine)
        _postgres_engine = engine
    return _postgres_engine


def _empty_postgres_tables(engine) -> None:
    """Every table emptied and its ids starting at 1 again, as in a new
    SQLite database in memory. A test that left a transaction open would
    hold the TRUNCATE up: lock_timeout turns that into an error."""
    quote = engine.dialect.identifier_preparer.quote
    tables = ", ".join(quote(table.name) for table in SQLModel.metadata.sorted_tables)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL lock_timeout = '10s'"))
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def session():
    if TEST_DATABASE_URL:
        engine = _test_postgres_engine()
        try:
            with Session(engine) as session:
                yield session
        finally:
            _empty_postgres_tables(engine)
        return
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


@pytest.fixture()
def empty_postgres_url():
    """A new, empty PostgreSQL database next to TEST_DATABASE_URL's, owned by
    the same role, dropped after the test (needs CREATEDB)."""
    if not TEST_DATABASE_URL:
        pytest.skip("PostgreSQL only; set TEST_DATABASE_URL")
    name = f"perchtail_t_{uuid.uuid4().hex[:12]}"
    admin = create_engine(TEST_DATABASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as connection:
            connection.execute(text(f"CREATE DATABASE {name} ENCODING 'UTF8' TEMPLATE template0"))
        yield make_url(TEST_DATABASE_URL).set(database=name).render_as_string(hide_password=False)
        with admin.connect() as connection:
            connection.execute(text(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)"))
    finally:
        admin.dispose()
