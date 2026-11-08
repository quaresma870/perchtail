"""GET /readyz (#330) and database upkeep (#346)."""

import sqlite3

import pytest
from app import db, health
from app.main import app
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select


class _Scheduler:
    def __init__(self, running=True):
        self.running = running


@pytest.fixture()
def file_engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'perchtail.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", db.configure_sqlite_connection)
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def ready_app(monkeypatch, tmp_path, file_engine):
    from app.config import get_settings

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr("app.main.engine", file_engine)
    monkeypatch.setattr("app.main.scheduler", _Scheduler())
    monkeypatch.setattr(get_settings(), "data_dir", str(data_dir))
    monkeypatch.setattr(health, "READY_DB_TIMEOUT_MS", 200)
    return TestClient(app)


def test_readyz_is_ready_and_leaves_nothing_behind(ready_app, file_engine, tmp_path):
    from app.models import JobRun

    response = ready_app.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    with Session(file_engine) as session:
        assert session.exec(select(JobRun)).all() == []
    assert list((tmp_path / "data").iterdir()) == []
    # The normal busy timeout is back on the pooled connection.
    with file_engine.connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar()
    assert timeout == db.SQLITE_BUSY_TIMEOUT_SECONDS * 1000


def test_readyz_is_not_ready_while_the_database_is_locked(ready_app, tmp_path):
    holder = sqlite3.connect(tmp_path / "perchtail.db", isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        response = ready_app.get("/readyz")
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "failed": ["database"]}
    assert ready_app.get("/readyz").status_code == 200


def test_readyz_is_not_ready_when_the_database_fails(ready_app, monkeypatch):
    broken = create_engine("sqlite://")  # no tables: the write fails
    monkeypatch.setattr("app.main.engine", broken)
    response = ready_app.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failed"] == ["database"]


def test_readyz_is_not_ready_when_the_scheduler_is_stopped(ready_app, monkeypatch):
    monkeypatch.setattr("app.main.scheduler", _Scheduler(running=False))
    response = ready_app.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failed"] == ["scheduler"]


def test_readyz_is_not_ready_when_the_data_dir_is_not_writable(ready_app, monkeypatch, tmp_path):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path / "missing"))
    response = ready_app.get("/readyz")
    assert response.status_code == 503
    assert response.json()["failed"] == ["data_dir"]


def test_healthz_stays_liveness_only(ready_app, monkeypatch):
    monkeypatch.setattr("app.main.scheduler", _Scheduler(running=False))
    assert ready_app.get("/healthz").json() == {"status": "ok"}


def test_connections_limit_the_wal_size(file_engine):
    with file_engine.connect() as connection:
        limit = connection.exec_driver_sql("PRAGMA journal_size_limit").scalar()
        mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert limit == db.SQLITE_JOURNAL_SIZE_LIMIT_BYTES
    assert mode == "wal"


def test_the_checkpoint_job_is_registered_hourly():
    from app.jobs import job_name

    scheduler = BackgroundScheduler()
    db.register_upkeep_jobs(scheduler)
    job = scheduler.get_job("wal_checkpoint")
    assert job is not None
    assert job.name == "Database WAL checkpoint"
    assert job.trigger.interval.total_seconds() == 3600
    assert job_name("wal_checkpoint") == "Database WAL checkpoint"


def test_the_checkpoint_truncates_the_wal(file_engine, monkeypatch, tmp_path):
    from app.models import JobRun

    monkeypatch.setattr("app.db.engine", file_engine)
    with Session(file_engine) as session:
        for n in range(200):
            session.add(JobRun(job_id=f"job-{n}", last_error="x" * 500))
        session.commit()
    wal = tmp_path / "perchtail.db-wal"
    assert wal.stat().st_size > 0

    db.run_wal_checkpoint()

    assert wal.stat().st_size == 0
    with Session(file_engine) as session:
        assert len(session.exec(select(JobRun)).all()) == 200


def test_the_checkpoint_skips_other_databases(monkeypatch):
    class _Dialect:
        name = "postgresql"

    class _Other:
        dialect = _Dialect()

        def connect(self):
            raise AssertionError("must not connect")

    monkeypatch.setattr("app.db.engine", _Other())
    db.run_wal_checkpoint()
