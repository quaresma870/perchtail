"""#327 job records and catch-up, #335 bounded shutdown."""

import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from app import jobs
from app.models import JobRun
from app.timeutils import utcnow
from app.worker_pool import DrainablePool
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.db.engine", engine)
    return engine


def _record(engine, job_id) -> JobRun | None:
    with Session(engine) as session:
        return session.get(JobRun, job_id)


def test_a_run_that_works_is_recorded(db):
    assert jobs.run_recorded("purge", lambda: None) is True
    row = _record(db, "purge")
    assert row.last_ok is True
    assert row.last_error is None
    assert row.last_started_at <= row.last_finished_at == row.last_success_at
    assert row.last_duration_ms >= 0


def test_a_failing_run_is_recorded_without_urls_or_secrets(db):
    def boom():
        raise RuntimeError(
            "POST https://hooks.example.com/abc?token=xyz failed; password=hunter2 " + "x" * 900
        )

    assert jobs.run_recorded("alerts", boom) is False
    row = _record(db, "alerts")
    assert row.last_ok is False
    assert row.last_error.startswith("RuntimeError: POST <url> failed; password=<hidden>")
    assert "hooks.example.com" not in row.last_error
    assert "hunter2" not in row.last_error
    assert len(row.last_error) == jobs.MAX_ERROR


def test_a_failure_keeps_the_last_success(db):
    jobs.run_recorded("purge", lambda: None)
    success = _record(db, "purge").last_success_at
    jobs.run_recorded("purge", lambda: 1 / 0)
    row = _record(db, "purge")
    assert row.last_ok is False
    assert row.last_success_at == success
    assert row.last_error.startswith("ZeroDivisionError")


def test_kwargs_reach_the_job(db):
    seen = {}
    jobs.run_recorded(
        "sweep", lambda idle_seconds: seen.update(idle=idle_seconds), {"idle_seconds": 5}
    )
    assert seen == {"idle": 5}


def test_a_record_that_cant_be_saved_doesnt_fail_the_job(monkeypatch):
    class Broken:
        def connect(self, *a, **k):
            raise RuntimeError("database is locked")

    monkeypatch.setattr("app.db.engine", Broken())
    ran = []
    assert jobs.run_recorded("purge", lambda: ran.append(1)) is True
    assert ran == [1]


def test_overdue_means_never_succeeded_or_an_interval_ago(db):
    day = timedelta(days=1)
    assert jobs.is_overdue("purge", day) is True
    with Session(db) as session:
        session.add(JobRun(job_id="purge", last_success_at=utcnow() - timedelta(hours=2)))
        session.commit()
    assert jobs.is_overdue("purge", day) is False
    assert jobs.is_overdue("purge", timedelta(hours=1)) is True


def test_an_overdue_job_runs_soon_after_startup(db):
    scheduler = BackgroundScheduler()
    with Session(db) as session:
        session.add(JobRun(job_id="recent", last_success_at=utcnow()))
        session.commit()

    overdue = jobs.register(
        scheduler, "integrity", "Integrity", lambda: None, timedelta(days=365), catch_up=True
    )
    recent = jobs.register(
        scheduler, "recent", "Recent", lambda: None, timedelta(days=365), catch_up=True
    )
    plain = jobs.register(scheduler, "plain", "Plain", lambda: None, timedelta(days=365))

    soon = datetime.now(UTC) + jobs.CATCH_UP_DELAY
    assert abs(overdue.next_run_time - soon) < timedelta(seconds=5)
    assert getattr(recent, "next_run_time", None) is None
    assert getattr(plain, "next_run_time", None) is None
    assert overdue.id == "integrity" and overdue.name == "Integrity"
    assert jobs.job_name("integrity") == "Integrity"


def test_job_records_reads_every_record(db):
    jobs.run_recorded("a", lambda: None)
    jobs.run_recorded("b", lambda: None)
    with Session(db) as session:
        assert set(jobs.job_records(session)) == {"a", "b"}


# --- shutdown -----------------------------------------------------------------


def test_drain_waits_for_running_work_and_drops_queued_work():
    pool = DrainablePool(1, "test-drain")
    started = threading.Event()
    done = []

    def slow():
        started.set()
        time.sleep(0.3)
        done.append("slow")

    pool.submit(slow)
    queued = pool.submit(lambda: done.append("queued"))
    started.wait(2)
    assert pool.drain(5) is True
    assert done == ["slow"]
    assert queued.cancelled()
    assert pool.submit(lambda: None) is None


def test_drain_gives_up_at_the_time_limit():
    pool = DrainablePool(1, "test-drain")
    release = threading.Event()
    pool.submit(release.wait, 10)
    begun = time.monotonic()
    assert pool.drain(0.2) is False
    assert time.monotonic() - begun < 2
    release.set()


def test_shutdown_lets_a_running_job_finish_within_the_limit():
    scheduler = BackgroundScheduler()
    running = threading.Event()
    finished = []

    def job():
        running.set()
        time.sleep(0.3)
        finished.append(1)

    scheduler.add_job(job, "interval", seconds=60, next_run_time=datetime.now(UTC))
    scheduler.start()
    assert running.wait(5)
    pool = DrainablePool(1, "test-drain")
    assert jobs.shutdown(scheduler, [pool], timeout=5) is True
    assert finished == [1]
    assert not scheduler.running


def test_shutdown_is_bounded_when_a_job_hangs():
    scheduler = BackgroundScheduler()
    running = threading.Event()
    release = threading.Event()

    def job():
        running.set()
        release.wait(10)

    scheduler.add_job(job, "interval", seconds=60, next_run_time=datetime.now(UTC))
    scheduler.start()
    assert running.wait(5)
    begun = time.monotonic()
    try:
        assert jobs.shutdown(scheduler, [], timeout=0.3) is False
        assert time.monotonic() - begun < 2
    finally:
        release.set()
