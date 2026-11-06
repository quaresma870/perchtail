"""#276: each source's reachability, recorded by real operations and by the
background check, shared by every user."""

from datetime import timedelta

import pytest
from app import source_status, system_settings
from app.collectors.base import (
    SourceAuthError,
    SourceNotFoundError,
    SourceUnreachableError,
)
from app.collectors.errors import tracked
from app.models import Protocol, Source
from app.timeutils import utcnow
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


@pytest.fixture()
def db(monkeypatch):
    """A database of its own, as app.db.engine, with status writes on."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr(source_status, "ENABLED", True)
    source_status.forget_cache()
    yield engine
    source_status.forget_cache()


def _source(engine, **kwargs) -> int:
    with Session(engine) as session:
        source = Source(
            name=kwargs.pop("name", "web01"),
            protocol=kwargs.pop("protocol", Protocol.ssh),
            host="web01",
            base_path="/var/log",
            **kwargs,
        )
        session.add(source)
        session.commit()
        return source.id


def _status(engine, source_id):
    with Session(engine) as session:
        source = session.get(Source, source_id)
        return source.status, source.status_message, source.status_checked_at


def test_an_operation_records_ok_and_a_connectivity_error_records_failing(db):
    source_id = _source(db)
    source = Source(id=source_id, name="x", protocol=Protocol.ssh, host="h", base_path="/")

    with tracked(source):
        pass
    assert _status(db, source_id)[:2] == ("ok", None)

    with pytest.raises(SourceAuthError), tracked(source):
        raise SourceAuthError("Permission denied (publickey)")
    status, message, _ = _status(db, source_id)
    assert status == "failing"
    # The public message only, never the raw error.
    assert message == SourceAuthError.public_message
    assert "publickey" not in message


def test_an_error_about_one_file_changes_nothing(db):
    source_id = _source(db)
    source = Source(id=source_id, name="x", protocol=Protocol.ssh, host="h", base_path="/")
    with tracked(source):
        pass
    with pytest.raises(SourceNotFoundError), tracked(source):
        raise SourceNotFoundError("no such file")
    assert _status(db, source_id)[0] == "ok"


def test_the_same_status_is_written_at_most_once_a_minute(db, monkeypatch):
    source_id = _source(db)
    source_status.record(source_id, True)
    first = _status(db, source_id)[2]
    source_status.record(source_id, True)
    assert _status(db, source_id)[2] == first  # throttled

    later = utcnow() + timedelta(minutes=2)
    monkeypatch.setattr(source_status, "utcnow", lambda: later)
    source_status.record(source_id, True)
    assert _status(db, source_id)[2] == later
    # A change is written at once.
    source_status.record(source_id, False, "The source isn't reachable")
    assert _status(db, source_id)[0] == "failing"


class _Connector:
    def __init__(self, failing: dict):
        self.failing = failing
        self.checked: list[str] = []

    def list_directory(self, source, rules, path):
        self.checked.append(source.name)
        if source.name in self.failing:
            raise self.failing[source.name]
        return []


def test_the_background_check_checks_what_is_due(db, monkeypatch):
    fresh = _source(db, name="fresh")
    stale = _source(db, name="stale")
    never = _source(db, name="never")
    down = _source(db, name="down")
    _source(db, name="off", enabled=False)
    _source(db, name="agent", protocol=Protocol.agent)
    with Session(db) as session:
        session.get(Source, fresh).status_checked_at = utcnow()
        session.get(Source, stale).status_checked_at = utcnow() - timedelta(minutes=30)
        session.commit()

    connector = _Connector({"down": SourceUnreachableError("timed out")})
    monkeypatch.setattr("app.collectors.registry.CONNECTORS", {Protocol.ssh: connector})
    source_status.run_source_checks()

    # Never-checked first, then the stalest; not the fresh, disabled or agent ones.
    assert connector.checked == ["never", "down", "stale"]
    assert _status(db, never)[0] == "ok"
    assert _status(db, stale)[0] == "ok"
    assert _status(db, down)[:2] == ("failing", SourceUnreachableError.public_message)
    assert _status(db, fresh)[0] is None


def test_the_background_check_can_be_turned_off(db, monkeypatch):
    _source(db, name="never")
    with Session(db) as session:
        system_settings.set_int(session, "source_check_minutes", 0)
        session.commit()
    connector = _Connector({})
    monkeypatch.setattr("app.collectors.registry.CONNECTORS", {Protocol.ssh: connector})
    source_status.run_source_checks()
    assert connector.checked == []
