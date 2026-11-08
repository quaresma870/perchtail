from datetime import timedelta

import pytest
from app.audit_hash_chain import backfill_chain_if_needed
from app.audit_integrity import verify_chain
from app.audit_purge import run_audit_purge_sweep
from app.auth.models import AuditChainState, AuditLog
from app.models import SystemSetting
from app.timeutils import utcnow
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

KEY = b"unit-test-key-not-a-real-secret"


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


def test_purge_advances_the_chain_anchor_to_the_newest_deleted_row(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        old = AuditLog(user_id=None, action="old.one")
        old.timestamp = utcnow() - timedelta(days=400)
        newer_old = AuditLog(user_id=None, action="old.two")
        newer_old.timestamp = utcnow() - timedelta(days=390)
        recent = AuditLog(user_id=None, action="recent.one")
        session.add(old)
        session.add(newer_old)
        session.add(recent)
        session.commit()
        backfill_chain_if_needed(KEY, session)
        session.commit()
        expected_anchor_hash = newer_old.row_hash
        expected_anchor_row_id = newer_old.id

    run_audit_purge_sweep()

    with Session(engine) as session:
        state = session.exec(select(AuditChainState)).first()
        assert state.anchor_row_id == expected_anchor_row_id
        assert state.anchor_hash == expected_anchor_hash


def test_verification_still_passes_for_the_surviving_chain_after_a_purge(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        old = AuditLog(user_id=None, action="old.one")
        old.timestamp = utcnow() - timedelta(days=400)
        recent = AuditLog(user_id=None, action="recent.one")
        session.add(old)
        session.add(recent)
        session.commit()
        backfill_chain_if_needed(KEY, session)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        result = verify_chain(KEY, session)
        session.commit()
        assert result.ok is True
        assert result.checked_count == 1


def test_purge_never_creates_a_hole_when_timestamp_and_id_order_diverge(monkeypatch):
    # Simulates the rare race the id-prefix logic exists for: a row's
    # timestamp is captured at construction, its id at commit, so under
    # concurrent writes the two orderings can, very rarely, diverge -- here
    # row2 has a higher id than row1 but an *older* timestamp, as if two
    # concurrent requests' commits landed in the opposite order from how
    # their audit events were originally constructed.
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        row1 = AuditLog(user_id=None, action="row1")  # id=1, recent
        row1.timestamp = utcnow() - timedelta(days=5)
        session.add(row1)
        session.commit()

        row2 = AuditLog(user_id=None, action="row2")  # id=2, but OLD timestamp
        row2.timestamp = utcnow() - timedelta(days=400)
        session.add(row2)
        session.commit()

        row3 = AuditLog(user_id=None, action="row3")  # id=3, recent
        session.add(row3)
        session.commit()

        backfill_chain_if_needed(KEY, session)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        remaining_ids = [r.id for r in session.exec(select(AuditLog).order_by(AuditLog.id)).all()]
        # A naive "timestamp < cutoff" filter would have deleted row2 alone,
        # leaving a hole in the id sequence between row1 and row3 that
        # desyncs the chain anchor -- nothing gets purged this cycle
        # instead, since row2 isn't part of a clean id-prefix; it's left
        # for a later sweep rather than purged in a way that corrupts the
        # chain.
        assert remaining_ids == [1, 2, 3]

        result = verify_chain(KEY, session)
        session.commit()
        assert result.ok is True


def test_a_sweep_with_nothing_to_purge_leaves_the_anchor_untouched(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        session.add(AuditChainState(anchor_row_id=1, anchor_hash="untouched"))
        entry = AuditLog(user_id=None, action="recent")
        session.add(entry)
        session.commit()

    run_audit_purge_sweep()

    with Session(engine) as session:
        state = session.exec(select(AuditChainState)).first()
        assert state.anchor_row_id == 1
        assert state.anchor_hash == "untouched"


@pytest.fixture(params=[None, 1, 2], ids=["default-chunk", "chunk-1", "chunk-2"], autouse=True)
def _chunk_size(request, monkeypatch):
    """#346: every test above runs with the default chunk and with chunks
    small enough that the purge takes several transactions, and must give
    the same result."""
    if request.param is not None:
        monkeypatch.setattr("app.audit_purge.CHUNK_SIZE", request.param)


def test_the_purge_deletes_in_chunks_each_in_its_own_transaction(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr("app.audit_purge.CHUNK_SIZE", 2)

    with Session(engine) as session:
        for n in range(5):
            row = AuditLog(user_id=None, action=f"old.{n}")
            row.timestamp = utcnow() - timedelta(days=400)
            session.add(row)
            session.commit()
        session.add(AuditLog(user_id=None, action="recent"))
        session.commit()
        backfill_chain_if_needed(KEY, session)
        session.commit()
        newest_old_hash = session.get(AuditLog, 5).row_hash

    from app import audit_purge

    chunks = []
    real_chunk = audit_purge._purge_chunk

    def counting_chunk(engine, last_id):
        deleted = real_chunk(engine, last_id)
        chunks.append(deleted)
        return deleted

    monkeypatch.setattr(audit_purge, "_purge_chunk", counting_chunk)
    run_audit_purge_sweep()

    assert chunks == [2, 2, 1, 0]
    with Session(engine) as session:
        assert [r.action for r in session.exec(select(AuditLog)).all()] == ["recent"]
        state = session.exec(select(AuditChainState)).first()
        assert (state.anchor_row_id, state.anchor_hash) == (5, newest_old_hash)
        assert verify_chain(KEY, session).ok is True


def test_rows_written_after_the_purge_started_are_kept(monkeypatch):
    """With every row old, the purge stops at the newest id it saw when it
    started, even if a newer row arrives between chunks."""
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)
    monkeypatch.setattr("app.audit_purge.CHUNK_SIZE", 1)

    with Session(engine) as session:
        for n in range(2):
            row = AuditLog(user_id=None, action=f"old.{n}")
            row.timestamp = utcnow() - timedelta(days=400)
            session.add(row)
        session.commit()

    from app import audit_purge

    real_chunk = audit_purge._purge_chunk
    written = []

    def chunk_then_write(engine, last_id):
        deleted = real_chunk(engine, last_id)
        if not written:
            with Session(engine) as session:
                late = AuditLog(user_id=None, action="late")
                late.timestamp = utcnow() - timedelta(days=400)
                session.add(late)
                session.commit()
            written.append(True)
        return deleted

    monkeypatch.setattr(audit_purge, "_purge_chunk", chunk_then_write)
    run_audit_purge_sweep()

    with Session(engine) as session:
        assert [r.action for r in session.exec(select(AuditLog)).all()] == ["late"]
