"""Database configuration (#306): DATABASE_URL and DATABASE_URL_FILE, the
PostgreSQL URL forms, the pool, the URL never shown with its password, and
the checks on a PostgreSQL server at startup (version, encoding, TLS)."""

from pathlib import Path

import pytest
from app import db
from app.config import (
    Settings,
    normalize_database_url,
    redacted_database_url,
    value_or_file,
)
from app.db import (
    DatabaseServer,
    DatabaseServerRefusedError,
    check_database_server,
    engine_options,
    is_local_host,
)
from pydantic import ValidationError
from sqlalchemy import create_engine

SECRET = "S3cr3t-Pa55word"
PG_URL = f"postgresql://perchtail:{SECRET}@db.example.com:5432/perchtail"


@pytest.fixture(autouse=True)
def _no_database_url_in_the_environment(monkeypatch):
    # With TEST_DATABASE_URL the suite sets DATABASE_URL (conftest.py); the
    # Settings built here read only what each test gives them.
    for name in ("DATABASE_URL", "DATABASE_URL_FILE"):
        monkeypatch.delenv(name, raising=False)


class _RecordingLogger:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **fields: self.events.append((level, event, fields))


@pytest.mark.parametrize(
    "given, expected",
    [
        ("postgres://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("sqlite:////data/db/perchtail.db", "sqlite:////data/db/perchtail.db"),
    ],
)
def test_postgresql_urls_use_psycopg(given, expected):
    assert normalize_database_url(given) == expected
    assert Settings(database_url=given).database_url == expected


def test_an_empty_database_url_is_sqlite_in_data_dir(tmp_path):
    settings = Settings(data_dir=str(tmp_path))
    assert settings.database_url == f"sqlite:///{tmp_path / 'perchtail.db'}"
    assert "database" in settings.derived_from_data_dir


def test_database_url_file_is_read_without_its_line_break(tmp_path):
    secret_file = tmp_path / "database-url"
    secret_file.write_text(PG_URL + "\n")
    settings = Settings(database_url_file=str(secret_file))
    assert settings.database_url == normalize_database_url(PG_URL)
    assert "database" not in settings.derived_from_data_dir


def test_database_url_and_its_file_together_are_refused(tmp_path):
    secret_file = tmp_path / "database-url"
    secret_file.write_text(PG_URL)
    with pytest.raises(ValidationError) as raised:
        Settings(database_url=PG_URL, database_url_file=str(secret_file))
    message = str(raised.value)
    assert "DATABASE_URL and DATABASE_URL_FILE are both set" in message
    assert SECRET not in message


def test_a_missing_database_url_file_is_refused(tmp_path):
    with pytest.raises(ValidationError, match="doesn't exist"):
        Settings(database_url_file=str(tmp_path / "missing"))


def test_an_empty_database_url_file_is_refused(tmp_path):
    secret_file = tmp_path / "database-url"
    secret_file.write_text("\n")
    with pytest.raises(ValidationError, match="empty file"):
        Settings(database_url_file=str(secret_file))


def test_value_or_file_never_quotes_the_content(tmp_path):
    secret_file = tmp_path / "secret"
    secret_file.write_bytes(b"\xff\xfe" + SECRET.encode())
    with pytest.raises(ValueError) as raised:
        value_or_file("SOME_SECRET", "", str(secret_file))
    assert SECRET not in str(raised.value)
    assert value_or_file("SOME_SECRET", "plain", "") == "plain"


def test_an_unparsable_url_is_refused_without_quoting_it():
    with pytest.raises(ValidationError) as raised:
        Settings(database_url=f"not a url {SECRET}")
    assert "DATABASE_URL is not a valid database URL" in str(raised.value)
    assert SECRET not in str(raised.value)


def test_a_refused_setting_does_not_quote_the_other_values():
    # hide_input_in_errors: pydantic would otherwise print every value read.
    with pytest.raises(ValidationError) as raised:
        Settings(database_url=PG_URL, trusted_proxies="not-an-address")
    assert SECRET not in str(raised.value)


@pytest.mark.parametrize(
    "url",
    [
        PG_URL,
        f"postgresql+psycopg://perchtail@db/perchtail?password={SECRET}",
        f"postgresql+psycopg://perchtail@db/perchtail?sslmode=verify-full&sslpassword={SECRET}",
    ],
)
def test_the_redacted_url_hides_every_password(url):
    shown = redacted_database_url(url)
    assert SECRET not in shown
    assert "perchtail" in shown and "db" in shown


def test_a_percent_encoded_password_is_hidden_too():
    shown = redacted_database_url("postgresql+psycopg://perchtail:S3cr%40t@db/perchtail")
    assert "S3cr" not in shown


def test_an_unparsable_url_is_never_shown():
    assert SECRET not in redacted_database_url(f"garbage {SECRET}")


def test_postgresql_engine_options_and_pool():
    settings = Settings(database_url=PG_URL)
    options = engine_options(settings.database_url, settings)
    assert options["pool_pre_ping"] is True
    assert options["pool_recycle"] == 1800
    assert options["connect_args"] == {
        "application_name": "perchtail",
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }
    # Nothing connects until a query runs.
    engine = create_engine(settings.database_url, **options)
    try:
        assert engine.dialect.driver == "psycopg"
        assert engine.pool.size() == 10
        assert engine.pool._max_overflow == 20
    finally:
        engine.dispose()


def test_the_pool_sizes_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "0")
    settings = Settings(database_url=PG_URL)
    options = engine_options(settings.database_url, settings)
    assert (options["pool_size"], options["max_overflow"]) == (3, 0)


def test_a_pool_without_connections_is_refused():
    with pytest.raises(ValidationError, match="DATABASE_POOL_SIZE"):
        Settings(database_url=PG_URL, database_pool_size=0)


def test_sqlite_engine_options_are_unchanged(tmp_path):
    url = f"sqlite:///{tmp_path / 'perchtail.db'}"
    assert engine_options(url, Settings(database_url=url)) == {
        "connect_args": {"check_same_thread": False, "timeout": db.SQLITE_BUSY_TIMEOUT_SECONDS}
    }


@pytest.mark.parametrize(
    "host, local",
    [
        ("", True),
        ("/var/run/postgresql", True),
        ("@abstract", True),
        ("localhost", True),
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("db", False),
        ("10.0.0.5", False),
        ("db.example.com", False),
    ],
)
def test_local_hosts(host, local):
    assert is_local_host(host) is local


def _server(**changes) -> DatabaseServer:
    values = {"version_num": 180001, "encoding": "UTF8", "host": "127.0.0.1", "sslmode": "prefer"}
    return DatabaseServer(**{**values, **changes})


@pytest.fixture()
def fake_server(monkeypatch):
    """check_database_server on a server described by the test, and the
    module's TLS flag restored after it."""
    holder = {}
    monkeypatch.setattr(db, "database_server", lambda target: holder["server"])
    monkeypatch.setattr(db, "_tls_not_verified", False)

    def use(**changes):
        holder["server"] = _server(**changes)

    return use


def _check(require_tls=True):
    logger = _RecordingLogger()
    settings = Settings(database_url=PG_URL, database_require_verified_tls=require_tls)
    check_database_server(None, logger, settings)
    return logger


def test_sqlite_has_nothing_to_check(monkeypatch):
    monkeypatch.setattr(db, "_tls_not_verified", False)
    engine = create_engine("sqlite://")
    logger = _RecordingLogger()
    check_database_server(engine, logger, Settings(database_url="sqlite://"))
    assert logger.events == []
    assert not db.database_tls_not_verified()


def test_a_server_older_than_18_is_refused(fake_server):
    fake_server(version_num=170006)
    logger = _RecordingLogger()
    with pytest.raises(DatabaseServerRefusedError, match="PostgreSQL 18"):
        check_database_server(None, logger, Settings(database_url=PG_URL))
    [(level, event, fields)] = logger.events
    assert (level, event) == ("critical", "startup.database_version_unsupported")
    assert fields["server_version_num"] == 170006


def test_a_database_not_in_utf8_is_refused(fake_server):
    fake_server(encoding="LATIN1")
    logger = _RecordingLogger()
    with pytest.raises(DatabaseServerRefusedError, match="UTF8"):
        check_database_server(None, logger, Settings(database_url=PG_URL))
    [(level, event, fields)] = logger.events
    assert (level, event) == ("critical", "startup.database_encoding_unsupported")
    assert fields["server_encoding"] == "LATIN1"


@pytest.mark.parametrize("sslmode", ["disable", "allow", "prefer", "require"])
def test_a_remote_server_without_a_verified_certificate_is_refused(fake_server, sslmode):
    fake_server(host="db.example.com", sslmode=sslmode)
    logger = _RecordingLogger()
    with pytest.raises(DatabaseServerRefusedError, match="DATABASE_REQUIRE_VERIFIED_TLS") as raised:
        check_database_server(None, logger, Settings(database_url=PG_URL))
    assert SECRET not in str(raised.value)
    [(level, event, fields)] = logger.events
    assert (level, event) == ("critical", "startup.database_tls_not_verified")
    assert fields["sslmode"] == sslmode
    assert not db.database_tls_not_verified()


def test_require_verified_tls_false_only_warns(fake_server):
    fake_server(host="db", sslmode="prefer")
    logger = _check(require_tls=False)
    [(level, event, fields)] = logger.events
    assert (level, event) == ("warning", "startup.database_tls_not_verified")
    assert "DATABASE_REQUIRE_VERIFIED_TLS is false" in fields["hint"]
    assert db.database_tls_not_verified()


@pytest.mark.parametrize("sslmode", ["verify-ca", "verify-full"])
def test_a_verified_certificate_passes(fake_server, sslmode):
    fake_server(host="db.example.com", sslmode=sslmode)
    assert _check().events == []
    assert not db.database_tls_not_verified()


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "/var/run/postgresql"])
def test_a_local_server_needs_no_tls(fake_server, host):
    fake_server(host=host, sslmode="disable")
    assert _check().events == []


def test_the_warnings_endpoint_reports_unverified_database_tls(session, monkeypatch):
    from app.api.auth import get_current_active_user
    from app.api.system_settings import router
    from app.auth.models import Role, User
    from app.db import get_session
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    role = Role(name="warnings admin", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="warnings-admin", role_id=role.id)
    session.add(user)
    session.commit()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_current_active_user] = lambda: user
    client = TestClient(app)

    monkeypatch.setattr("app.api.system_settings.key_strength.is_weak", lambda key: False)
    monkeypatch.setattr(db, "_tls_not_verified", True)
    [warning] = client.get("/system-settings/warnings").json()
    assert warning["code"] == "database_tls_not_verified"
    assert "verify-full" in warning["advice"]

    monkeypatch.setattr(db, "_tls_not_verified", False)
    assert client.get("/system-settings/warnings").json() == []


def test_only_a_sqlite_file_adds_its_folder_to_the_protected_paths(tmp_path):
    from app.main import protected_paths

    db_dir = tmp_path / "elsewhere"
    sqlite = Settings(data_dir=str(tmp_path / "db"), database_url=f"sqlite:///{db_dir / 'p.db'}")
    assert db_dir in protected_paths(sqlite)

    postgres = Settings(data_dir=str(tmp_path / "db"), database_url=PG_URL)
    paths = protected_paths(postgres)
    assert Path(tmp_path / "db") in paths
    assert not any("postgresql" in str(path) or "db.example.com" in str(path) for path in paths)
    assert len(paths) == len(protected_paths(sqlite)) - 1
