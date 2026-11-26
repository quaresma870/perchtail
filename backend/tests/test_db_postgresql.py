"""PostgreSQL as the database (#306), on a real server (TEST_DATABASE_URL):
the server checks pass on a supported one, one instance per database,
sizes past 2^31, the trigram index, and no password in what startup logs.
The size test also runs on SQLite."""

import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from app import instance_lock
from app.config import normalize_database_url
from app.db import check_database_server, database_server
from app.models import Alert, AlertFileState, Customer, FileNameEntry, Protocol, Source
from app.name_index_store import PG_TRGM_INDEX, ensure_name_index_schema
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlmodel import select

TEST_DATABASE_URL = normalize_database_url(os.environ.get("TEST_DATABASE_URL", ""))
BACKEND = Path(__file__).resolve().parents[1]
STRONG_TEST_KEY = "q7Vt2pXk9LmN4rYs8BwE3cHj6FdA1gZu5KoTiPxQ0eR"
TWO_TO_THE_40 = 2**40


class _RecordingLogger:
    def __init__(self):
        self.events: list[tuple[str, str, dict]] = []

    def __getattr__(self, level):
        return lambda event, **fields: self.events.append((level, event, fields))


def test_sizes_and_offsets_past_two_gib_are_kept(session):
    customer = Customer(name="big files")
    session.add(customer)
    session.commit()
    source = Source(
        name="big", customer_id=customer.id, protocol=Protocol.local, host="", base_path="/"
    )
    session.add(source)
    session.commit()
    from app.auth.models import Role, User

    role = Role(name="owner")
    session.add(role)
    session.commit()
    user = User(username="owner", role_id=role.id)
    session.add(user)
    session.commit()
    alert = Alert(
        user_id=user.id,
        name="big",
        query="x",
        webhook_url_encrypted="encrypted",
        created_at=datetime(2026, 11, 26),
    )
    session.add(alert)
    session.commit()
    session.add(
        FileNameEntry(
            source_id=source.id, path="huge.log", size=TWO_TO_THE_40, seen_at=datetime(2026, 1, 1)
        )
    )
    session.add(
        AlertFileState(
            alert_id=alert.id, source_id=source.id, file_path="huge.log", offset=TWO_TO_THE_40
        )
    )
    session.commit()
    session.expire_all()

    assert session.exec(select(FileNameEntry.size)).one() == TWO_TO_THE_40
    assert session.exec(select(AlertFileState.offset)).one() == TWO_TO_THE_40


@pytest.mark.postgres_only
def test_the_test_server_passes_the_startup_checks():
    engine = create_engine(TEST_DATABASE_URL)
    try:
        server = database_server(engine)
        assert server.version_num >= 180000
        assert server.encoding == "UTF8"
        logger = _RecordingLogger()
        check_database_server(engine, logger)
        assert logger.events == []
    finally:
        engine.dispose()


@pytest.mark.postgres_only
def test_one_instance_per_database():
    first_engine = create_engine(TEST_DATABASE_URL)
    second_engine = create_engine(TEST_DATABASE_URL)
    try:
        first = instance_lock.acquire_database(first_engine)
        assert first is not None and first.held
        with pytest.raises(instance_lock.DatabaseLockHeldError) as raised:
            instance_lock.acquire_database(second_engine)
        assert raised.value.holder["pid"]
        logger = _RecordingLogger()
        instance_lock.log_database_held(logger, raised.value)
        [(level, event, fields)] = logger.events
        assert (level, event) == ("critical", "startup.instance_lock_held")
        assert fields["lock"] == "database"
        assert fields["holder_pid"] == raised.value.holder["pid"]

        first.release()
        first.release()  # twice is harmless
        assert not first.held
        again = instance_lock.acquire_database(second_engine)
        again.release()
    finally:
        first_engine.dispose()
        second_engine.dispose()


def test_sqlite_takes_no_database_lock():
    assert instance_lock.acquire_database(create_engine("sqlite://")) is None


def test_the_lock_check_is_registered_only_with_a_database_lock():
    from apscheduler.schedulers.background import BackgroundScheduler

    scheduler = BackgroundScheduler()
    instance_lock.register_database_lock_check(scheduler, None)
    assert scheduler.get_job("database_lock_check") is None

    class _Lock:
        def check(self):
            pass

    instance_lock.register_database_lock_check(scheduler, _Lock())
    job = scheduler.get_job("database_lock_check")
    assert job.name == "Database instance lock check"
    assert job.trigger.interval.total_seconds() == 60


def _terminate(engine, lock: instance_lock.DatabaseLock) -> None:
    """Ends the lock's session from the server side, as a restart would."""
    pid = lock._connection.execute(text("SELECT pg_backend_pid()")).scalar()
    lock._connection.commit()
    with engine.connect() as other:
        assert other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}).scalar()


@pytest.mark.postgres_only
def test_the_lock_check_takes_a_lost_lock_again(monkeypatch):
    monkeypatch.setattr(instance_lock, "_lock_lost", False)
    logger = _RecordingLogger()
    monkeypatch.setattr(instance_lock, "logger", logger)
    engine = create_engine(TEST_DATABASE_URL)
    other_engine = create_engine(TEST_DATABASE_URL)
    lock = instance_lock.acquire_database(engine)
    try:
        lock.check()  # still held: nothing to do
        assert logger.events == []

        _terminate(other_engine, lock)
        lock.check()
        assert [(level, event) for level, event, _ in logger.events] == [
            ("warning", "database.instance_lock_reacquired")
        ]
        assert lock.held and not instance_lock.database_lock_lost()
        # Held again, on the new connection.
        with pytest.raises(instance_lock.DatabaseLockHeldError):
            instance_lock.acquire_database(other_engine)
    finally:
        lock.release()
        engine.dispose()
        other_engine.dispose()


@pytest.mark.postgres_only
def test_a_lock_taken_by_another_instance_is_lost_for_good(monkeypatch, tmp_path):
    from app import main
    from app.config import get_settings
    from fastapi.testclient import TestClient

    monkeypatch.setattr(instance_lock, "_lock_lost", False)
    logger = _RecordingLogger()
    monkeypatch.setattr(instance_lock, "logger", logger)
    engine = create_engine(TEST_DATABASE_URL)
    other_engine = create_engine(TEST_DATABASE_URL)
    lock = instance_lock.acquire_database(engine)
    other = None
    try:
        _terminate(other_engine, lock)
        other = instance_lock.acquire_database(other_engine)
        lock.check()
        [(level, event, fields)] = logger.events
        assert (level, event) == ("critical", "database.instance_lock_lost")
        assert fields["holder_pid"]
        assert instance_lock.database_lock_lost()

        # Fail-closed until the process ends, even once the other one stops.
        other.release()
        lock.check()
        assert len(logger.events) == 1

        class _Scheduler:
            running = True

        monkeypatch.setattr(main, "engine", engine)
        monkeypatch.setattr(main, "scheduler", _Scheduler())
        monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
        response = TestClient(main.app, client=("127.0.0.1", 50000)).get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready", "failed": ["database_lock"]}
    finally:
        if other is not None:
            other.release()
        lock.release()
        engine.dispose()
        other_engine.dispose()


@pytest.mark.postgres_only
def test_the_trigram_index_is_created_once(empty_postgres_url):
    from sqlmodel import SQLModel

    engine = create_engine(empty_postgres_url)
    try:
        SQLModel.metadata.create_all(engine)
        ensure_name_index_schema(engine)
        ensure_name_index_schema(engine)
        with engine.connect() as connection:
            definitions = (
                connection.execute(
                    text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
                    {"name": PG_TRGM_INDEX},
                )
                .scalars()
                .all()
            )
            extension = connection.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
            ).scalar()
            fts = connection.execute(text("SELECT to_regclass('file_name_fts')")).scalar()
    finally:
        engine.dispose()
    [definition] = definitions
    assert "gin" in definition and "gin_trgm_ops" in definition
    assert extension == 1
    assert fts is None


def _environment(tmp_path: Path, url: str) -> dict[str, str]:
    env = {
        **os.environ,
        "DATABASE_URL": url,
        "DATA_DIR": str(tmp_path / "db"),
        "LOG_DIR": str(tmp_path / "logs"),
        "SCRATCH_DIR": str(tmp_path / "scratch"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "CREDENTIAL_ENCRYPTION_KEY": STRONG_TEST_KEY,
        "UPDATE_CHECK": "false",
    }
    for name in ("TEST_DATABASE_URL", "CREDENTIAL_SALT_PATH", "SSH_KNOWN_HOSTS_PATH"):
        env.pop(name, None)
    return env


_START_AND_STOP = (
    "from fastapi.testclient import TestClient\n"
    "from app.main import app\n"
    "with TestClient(app, client=('127.0.0.1', 50000)) as client:\n"
    "    print('healthz', client.get('/healthz').status_code)\n"
)


def _everything_logged(tmp_path: Path, *results: subprocess.CompletedProcess) -> str:
    logs = "".join(
        path.read_text(errors="replace") for path in (tmp_path / "logs").glob("*") if path.is_file()
    )
    return logs + "".join(result.stdout + result.stderr for result in results)


@pytest.mark.postgres_only
def test_startup_never_logs_the_database_password(tmp_path, empty_postgres_url):
    password = make_url(empty_postgres_url).password
    assert password
    env = _environment(tmp_path, empty_postgres_url)
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert migrate.returncode == 0, migrate.stderr
    start = subprocess.run(
        [sys.executable, "-c", _START_AND_STOP],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert start.returncode == 0, start.stderr
    assert "healthz 200" in start.stdout
    everything = _everything_logged(tmp_path, migrate, start)
    assert "audit_hash_chain.marker_written" in everything  # the start really ran
    assert password not in everything


@pytest.mark.postgres_only
def test_a_refused_connection_never_shows_the_password(tmp_path):
    # A % in the password, which Alembic's configparser would otherwise read
    # as interpolation and quote in its error.
    wrong = f"Wr0ng%-{uuid.uuid4().hex}"
    url = make_url(TEST_DATABASE_URL).set(password=wrong).render_as_string(hide_password=False)
    env = _environment(tmp_path, url)
    migrate = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    start = subprocess.run(
        [sys.executable, "-c", _START_AND_STOP],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert migrate.returncode != 0
    assert start.returncode != 0
    # Refused by the server, not by a URL that couldn't be read.
    assert "password authentication failed" in migrate.stderr
    assert "password authentication failed" in start.stderr
    everything = _everything_logged(tmp_path, migrate, start)
    assert wrong.split("-")[1] not in everything
