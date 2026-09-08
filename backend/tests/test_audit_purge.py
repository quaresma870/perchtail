from datetime import timedelta

from app.audit_purge import run_audit_purge_sweep
from app.auth.models import AuditLog
from app.models import SystemSetting
from app.timeutils import utcnow
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_default_retention_purges_entries_older_than_365_days(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        old = AuditLog(user_id=None, action="source.create")
        old.timestamp = utcnow() - timedelta(days=400)
        recent = AuditLog(user_id=None, action="source.update")
        session.add(old)
        session.add(recent)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        remaining = session.exec(select(AuditLog)).all()
        assert [r.action for r in remaining] == ["source.update"]


def test_configured_retention_is_honored(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        session.add(SystemSetting(key="audit_retention_days", value="7"))
        entry = AuditLog(user_id=None, action="source.create")
        entry.timestamp = utcnow() - timedelta(days=8)
        session.add(entry)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        assert session.exec(select(AuditLog)).all() == []


def test_zero_retention_means_keep_forever(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        session.add(SystemSetting(key="audit_retention_days", value="0"))
        entry = AuditLog(user_id=None, action="source.create")
        entry.timestamp = utcnow() - timedelta(days=10_000)
        session.add(entry)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        assert len(session.exec(select(AuditLog)).all()) == 1


def test_entries_within_retention_are_kept(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        entry = AuditLog(user_id=None, action="source.create")
        entry.timestamp = utcnow() - timedelta(days=1)
        session.add(entry)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        assert len(session.exec(select(AuditLog)).all()) == 1
