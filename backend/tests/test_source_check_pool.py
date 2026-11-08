"""#343: background source checks run in a pool with a deadline per source,
and a source that keeps failing backs off."""

import threading
import time
from datetime import timedelta

import pytest
from app import name_index, source_status
from app.collectors.base import SourceUnreachableError
from app.models import Protocol, Source
from app.timeutils import utcnow
from app.worker_pool import DrainablePool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture()
def db(monkeypatch, tmp_path):
    """A file database (worker threads write at once), as app.db.engine."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'checks.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr(source_status, "ENABLED", True)
    monkeypatch.setattr(source_status, "_pool", DrainablePool(4, "test-check"))
    source_status.forget_cache()
    yield engine
    source_status.forget_cache()


def _source(engine, name, **kwargs) -> int:
    with Session(engine) as session:
        source = Source(name=name, protocol=Protocol.ssh, host=name, base_path="/var/log", **kwargs)
        session.add(source)
        session.commit()
        return source.id


def _row(engine, source_id) -> Source:
    with Session(engine) as session:
        source = session.get(Source, source_id)
        session.expunge(source)
        return source


class _Connector:
    def __init__(self, stall: set[str] = frozenset(), failing: set[str] = frozenset()):
        self.stall = stall
        self.failing = failing
        self.release = threading.Event()
        self.checked: list[str] = []

    def list_directory(self, source, rules, path):
        self.checked.append(source.name)
        if source.name in self.stall:
            self.release.wait(10)
        if source.name in self.failing:
            raise SourceUnreachableError("timed out")
        return []


def test_a_stalled_source_only_costs_its_own_slot(db, monkeypatch):
    stalled = _source(db, "stalled")
    others = [_source(db, f"web{i}") for i in range(6)]
    connector = _Connector(stall={"stalled"})
    monkeypatch.setattr("app.collectors.registry.CONNECTORS", {Protocol.ssh: connector})
    monkeypatch.setattr(source_status, "check_deadline", lambda: 0.5)

    started = time.monotonic()
    source_status.run_source_checks()
    try:
        assert time.monotonic() - started < 5
        for source_id in others:
            assert _row(db, source_id).status == "ok"
        row = _row(db, stalled)
        assert (row.status, row.status_message) == ("failing", source_status.TIMED_OUT)
        assert row.check_failures == 1

        # Still running: the next pass doesn't hand it another worker.
        with Session(db) as session:
            session.get(Source, stalled).status_checked_at = None
            session.commit()
        connector.checked.clear()
        source_status.run_source_checks()
        assert "stalled" not in connector.checked
    finally:
        connector.release.set()

    # When it finally answers, the late result doesn't overwrite the timeout.
    deadline = time.monotonic() + 5
    while stalled in source_status._in_flight and time.monotonic() < deadline:
        time.sleep(0.05)
    assert stalled not in source_status._in_flight
    assert _row(db, stalled).status == "failing"


def test_three_failures_in_a_row_back_off_and_a_success_resets(db, monkeypatch):
    source_id = _source(db, "down")
    interval = timedelta(minutes=10)
    failure = [({"label": None}, SourceUnreachableError("timed out"))]
    for _ in range(2):
        source_status.record_background_check(source_id, failure, interval)
    assert _row(db, source_id).check_backoff_until is None

    source_status.record_background_check(source_id, failure, interval)
    row = _row(db, source_id)
    assert row.check_failures == 3
    assert row.check_backoff_until > utcnow() + timedelta(minutes=10)
    assert row.status == "failing"

    # Backing off: the background check and the name index skip it.
    with Session(db) as session:
        session.get(Source, source_id).status_checked_at = None
        session.commit()
    connector = _Connector()
    monkeypatch.setattr("app.collectors.registry.CONNECTORS", {Protocol.ssh: connector})
    source_status.run_source_checks()
    assert connector.checked == []
    with Session(db) as session:
        assert source_id not in name_index.due_source_ids(session)

    # Any success (a user's browse, say) ends it.
    source_status.record(source_id, True)
    row = _row(db, source_id)
    assert (row.check_failures, row.check_backoff_until) == (0, None)


def test_the_backoff_grows_with_jitter_and_is_capped():
    interval = timedelta(minutes=1)
    first = source_status.backoff_delay(3, interval)
    assert timedelta(seconds=90) <= first <= timedelta(minutes=2)
    later = source_status.backoff_delay(5, interval)
    assert timedelta(minutes=6) <= later <= timedelta(minutes=8)
    capped = source_status.backoff_delay(30, interval)
    assert capped <= source_status.BACKOFF_CAP
    assert capped >= source_status.BACKOFF_CAP * 0.75


def test_a_backoff_that_ran_out_is_checked_again(db, monkeypatch):
    source_id = _source(db, "back", check_failures=3)
    with Session(db) as session:
        session.get(Source, source_id).check_backoff_until = utcnow() - timedelta(seconds=1)
        session.commit()
    connector = _Connector()
    monkeypatch.setattr("app.collectors.registry.CONNECTORS", {Protocol.ssh: connector})
    source_status.run_source_checks()
    assert connector.checked == ["back"]
    assert _row(db, source_id).check_failures == 0
