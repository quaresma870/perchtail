import asyncio
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from app import instance_lock
from app.config import Settings, get_settings
from app.main import app, lifespan
from app.schema_version import DatabaseNewerThanAppError
from app.version import APP_VERSION
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

BACKEND = Path(__file__).resolve().parents[1]
UNKNOWN_REVISION = "ffffffffffff"

# The Docker HEALTHCHECK calls from inside the container (#366).
LOCAL = ("127.0.0.1", 50000)


def test_healthz():
    client = TestClient(app, client=LOCAL)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_healthz_sets_request_id_header():
    client = TestClient(app, client=LOCAL)
    response = client.get("/healthz")
    assert "x-request-id" in response.headers


def test_responses_carry_security_headers():
    client = TestClient(app, client=LOCAL)
    response = client.get("/healthz")

    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["x-content-type-options"] == "nosniff"
    # #276: the browser tells PerchTail's own 502s from a proxy's.
    assert response.headers["x-perchtail"] == "1"
    assert response.headers["referrer-policy"] == "same-origin"
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-eval" not in csp


def test_lifespan_refuses_to_start_with_the_default_credential_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "changeme")
    get_settings.cache_clear()

    async def _enter_and_exit_lifespan():
        async with lifespan(app):
            pass

    try:
        with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
            asyncio.run(_enter_and_exit_lifespan())
    finally:
        get_settings.cache_clear()


class _RecordingLogger:
    """Stands in for app.main's logger: structlog caches loggers on first
    use, so capture_logs can't reliably see app.main's."""

    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **fields: self.events.append((level, event, fields))


@pytest.fixture()
def own_data_dir(tmp_path, monkeypatch):
    """Each test that starts the lifespan gets its own DATA_DIR, so tests in
    parallel never meet on the instance lock (#334)."""
    import app.main as main_module

    monkeypatch.setenv("DATA_DIR", str(tmp_path / "db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", "test-key-not-the-default")
    for name in ("DATABASE_URL", "CREDENTIAL_SALT_PATH", "SSH_KNOWN_HOSTS_PATH"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    logger = _RecordingLogger()
    monkeypatch.setattr(main_module, "logger", logger)
    init_db_calls = []
    monkeypatch.setattr(main_module, "init_db", lambda: init_db_calls.append(True))
    yield tmp_path, logger, init_db_calls
    get_settings.cache_clear()


def _run_lifespan():
    async def _enter_and_exit_lifespan():
        async with lifespan(app):
            pass

    asyncio.run(_enter_and_exit_lifespan())


def test_lifespan_refuses_a_second_instance_before_init_db(own_data_dir):
    _tmp_path, logger, init_db_calls = own_data_dir
    first = instance_lock.acquire(get_settings())
    try:
        with pytest.raises(instance_lock.InstanceLockHeldError):
            _run_lifespan()
    finally:
        first.release()

    assert init_db_calls == []
    [(level, _event, fields)] = [e for e in logger.events if e[1] == "startup.instance_lock_held"]
    assert level == "critical"
    assert fields["holder_pid"] == os.getpid()
    assert fields["holder_version"] == APP_VERSION
    assert fields["holder_host"] and fields["holder_started_at"]


def test_lifespan_refuses_a_database_newer_than_the_app(own_data_dir, monkeypatch):
    import app.main as main_module

    tmp_path, logger, init_db_calls = own_data_dir
    engine = create_engine(f"sqlite:///{tmp_path / 'newer.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        connection.exec_driver_sql(f"INSERT INTO alembic_version VALUES ('{UNKNOWN_REVISION}')")
    monkeypatch.setattr(main_module, "engine", engine)
    try:
        with pytest.raises(DatabaseNewerThanAppError):
            _run_lifespan()
    finally:
        engine.dispose()

    assert init_db_calls == []
    [(level, _event, fields)] = [
        e for e in logger.events if e[1] == "startup.database_newer_than_app"
    ]
    assert level == "critical"
    assert fields["database_revision"] == UNKNOWN_REVISION
    assert fields["app_version"] == APP_VERSION
    # The lock was released on the way out.
    instance_lock.acquire(get_settings()).release()


def _uvicorn(tmp_path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path / "db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCRATCH_DIR": str(tmp_path / "scratch"),
        "CREDENTIAL_ENCRYPTION_KEY": "test-key-not-the-default",
    }
    for name in ("DATABASE_URL", "CREDENTIAL_SALT_PATH", "SSH_KNOWN_HOSTS_PATH"):
        env.pop(name, None)
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_server_exits_non_zero_when_another_instance_holds_data_dir(tmp_path):
    first = instance_lock.acquire(Settings(data_dir=str(tmp_path / "db")))
    try:
        result = _uvicorn(tmp_path)
    finally:
        first.release()

    assert result.returncode != 0
    assert "startup.instance_lock_held" in result.stdout
    assert not (tmp_path / "db" / "perchtail.db").exists()


def test_the_server_exits_non_zero_on_a_database_newer_than_the_app(tmp_path):
    db = tmp_path / "db" / "perchtail.db"
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32))")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (UNKNOWN_REVISION,))

    result = _uvicorn(tmp_path)

    assert result.returncode != 0
    assert "startup.database_newer_than_app" in result.stdout
    with sqlite3.connect(db) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert tables == {"alembic_version"}
