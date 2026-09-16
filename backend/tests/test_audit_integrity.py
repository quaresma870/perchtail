from app.audit import record_audit_event
from app.audit_hash_chain import chain_new_entry
from app.audit_integrity import run_audit_integrity_check, verify_chain
from app.auth.models import AuditChainState, AuditLog
from app.crypto import audit_chain_key
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

# record_audit_event (app/audit.py) always chains under the real,
# app.crypto-derived key -- these tests verify against that same key so a
# manual verify_chain() call here matches what production actually does.
KEY = audit_chain_key()


def _engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    SQLModel.metadata.create_all(engine)
    return engine


def test_verify_chain_on_an_empty_log_is_ok(session):
    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is True
    assert result.checked_count == 0
    assert result.broken_row_id is None

    state = session.exec(select(AuditChainState)).first()
    assert state.status == "ok"
    assert state.last_checked_at is not None


def test_verify_chain_is_ok_for_an_intact_chain(session):
    record_audit_event(session, user_id=None, action="a")
    record_audit_event(session, user_id=None, action="b")
    record_audit_event(session, user_id=None, action="c")
    session.commit()

    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is True
    assert result.checked_count == 3


def test_verify_chain_detects_a_mutated_action(session):
    record_audit_event(session, user_id=None, action="a")
    entry_b = record_audit_event(session, user_id=None, action="b")
    record_audit_event(session, user_id=None, action="c")
    session.commit()

    entry_b.action = "tampered"
    session.add(entry_b)
    session.commit()

    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is False
    assert result.broken_row_id == entry_b.id
    # Everything from the tampered row onward is unverifiable, so the walk
    # stops there rather than continuing past it.
    assert result.checked_count == 2

    state = session.exec(select(AuditChainState)).first()
    assert state.status == "broken"
    assert state.broken_row_id == entry_b.id


def test_verify_chain_detects_a_deleted_row_via_the_gap_it_leaves(session):
    record_audit_event(session, user_id=None, action="a")
    entry_b = record_audit_event(session, user_id=None, action="b")
    entry_c = record_audit_event(session, user_id=None, action="c")
    session.commit()

    session.delete(entry_b)
    session.commit()

    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is False
    assert result.broken_row_id == entry_c.id


def test_verify_chain_detects_a_forged_row_hash_with_a_correct_looking_prev_hash(session):
    # A more sophisticated tamper attempt than a plain content edit: the
    # attacker also patches prev_hash on the row *after* the edit to keep
    # the chain link superficially intact. row_hash itself still won't
    # recompute to match, since it covers the row's own content too.
    entry_a = record_audit_event(session, user_id=None, action="a")
    entry_b = record_audit_event(session, user_id=None, action="b")
    session.commit()

    entry_b.action = "tampered"
    entry_b.prev_hash = entry_a.row_hash  # forged to still "look" linked
    session.add(entry_b)
    session.commit()

    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is False
    assert result.broken_row_id == entry_b.id


def test_verify_chain_with_the_wrong_key_reports_broken(session):
    # Proves the key genuinely participates in verification, not just in
    # writing -- an attacker who edited the row and recomputed hashes with
    # the *wrong* key (not knowing the real one) must still get caught,
    # exactly the scenario a bare, keyless hash chain couldn't catch at all.
    record_audit_event(session, user_id=None, action="a")
    record_audit_event(session, user_id=None, action="b")
    session.commit()

    result = verify_chain(b"the-wrong-key-entirely", session)
    session.commit()
    assert result.ok is False


def test_verify_chain_respects_the_purge_anchor(session):
    # Simulates the state right after a purge sweep: the surviving row's
    # prev_hash points at a row that no longer exists, but the anchor
    # records what that hash was -- see AuditChainState's docstring.
    session.add(AuditChainState(anchor_row_id=1, anchor_hash="anchor-hash"))
    session.commit()

    surviving = AuditLog(user_id=None, action="post-purge")
    chain_new_entry(KEY, session, surviving)
    session.add(surviving)
    session.commit()
    assert surviving.prev_hash == "anchor-hash"

    result = verify_chain(KEY, session)
    session.commit()
    assert result.ok is True


def test_run_audit_integrity_check_persists_ok_status(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        record_audit_event(session, user_id=None, action="a")
        session.commit()

    run_audit_integrity_check()

    with Session(engine) as session:
        state = session.exec(select(AuditChainState)).first()
        assert state.status == "ok"


def test_run_audit_integrity_check_persists_broken_status(monkeypatch):
    engine = _engine()
    monkeypatch.setattr("app.db.engine", engine)

    with Session(engine) as session:
        entry = record_audit_event(session, user_id=None, action="a")
        session.commit()
        entry.action = "tampered"
        session.add(entry)
        session.commit()
        entry_id = entry.id

    run_audit_integrity_check()

    with Session(engine) as session:
        state = session.exec(select(AuditChainState)).first()
        assert state.status == "broken"
        assert state.broken_row_id == entry_id
