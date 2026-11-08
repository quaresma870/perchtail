import pytest
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


# --- #217: known breaks from the #213 race --------------------------------


def _forked_row(session, parent: AuditLog, action: str) -> AuditLog:
    """A row the #213 race wrote: valid under the key, chained from an
    earlier row instead of the tip."""
    from app.audit_hash_chain import compute_row_hash

    entry = AuditLog(user_id=None, action=action)
    entry.prev_hash = parent.row_hash
    entry.row_hash = compute_row_hash(
        KEY,
        parent.row_hash,
        user_id=None,
        action=action,
        target_type=None,
        target_id=None,
        timestamp=entry.timestamp,
        metadata=None,
    )
    session.add(entry)
    session.commit()
    return entry


def _set_cutoff(session, row_id):
    state = session.exec(select(AuditChainState)).first()
    if state is None:
        state = AuditChainState()
    state.known_break_cutoff_row_id = row_id
    session.add(state)
    session.commit()


def _lab_case(session):
    """The lab's case: three rows written in the same millisecond, all
    chained from one tip, then a normal row on top."""
    record_audit_event(session, user_id=None, action="before")
    tip = record_audit_event(session, user_id=None, action="tip")
    first = record_audit_event(session, user_id=None, action="source.open")
    session.commit()
    second = _forked_row(session, tip, "source.open")
    third = _forked_row(session, tip, "source.open")
    after = record_audit_event(session, user_id=None, action="after")
    session.commit()
    _set_cutoff(session, third.id)
    return first, second, third, after


def _verify(session):
    result = verify_chain(KEY, session)
    session.commit()
    return result


def test_race_breaks_are_reported_apart_from_tampering(session):
    _, second, third, _ = _lab_case(session)

    result = _verify(session)

    assert result.ok is False
    assert result.race_break_ids == [second.id, third.id]
    state = session.exec(select(AuditChainState)).first()
    assert (state.status, state.broken_row_id, state.race_break_count) == (
        "race_breaks",
        second.id,
        2,
    )


def test_recorded_race_breaks_verify_as_intact(session):
    from app.audit_integrity import KNOWN_BREAKS_ACTION, record_known_breaks

    first, second, third, _ = _lab_case(session)

    result = record_known_breaks(KEY, session, user_id=None)
    session.commit()

    assert result.ok is True
    assert result.known_break_count == 2
    state = session.exec(select(AuditChainState)).first()
    assert state.status == "ok" and state.known_breaks_recorded_at is not None
    entry = session.exec(select(AuditLog).where(AuditLog.action == KNOWN_BREAKS_ACTION)).one()
    assert [item["id"] for item in entry.event_metadata["breaks"]] == [second.id, third.id]
    assert [item["id"] for item in entry.event_metadata["leaves"]] == [first.id]
    # And it stays intact on the next check, with new rows on top.
    record_audit_event(session, user_id=None, action="later")
    session.commit()
    assert _verify(session).ok is True


def test_an_edited_row_does_not_match_and_cannot_be_recorded(session):
    import pytest
    from app.audit_integrity import NoRaceBreaksError, record_known_breaks

    _, second, _, _ = _lab_case(session)
    _set_cutoff(session, second.id)  # only `second` is in range
    second.action = "edited"
    session.add(second)
    session.commit()

    result = _verify(session)
    assert result.ok is False
    assert result.broken_row_id == second.id
    assert result.race_break_ids == []
    with pytest.raises(NoRaceBreaksError):
        record_known_breaks(KEY, session, user_id=None)


def test_an_edited_row_is_still_tampering_next_to_recordable_breaks(session):
    _, second, third, after = _lab_case(session)
    after.action = "edited"
    session.add(after)
    session.commit()

    result = _verify(session)

    assert result.broken_row_id == after.id
    assert result.race_break_ids == [second.id, third.id]
    assert session.exec(select(AuditChainState)).first().status == "broken"


def test_a_deleted_recorded_break_row_is_a_break(session):
    from app.audit_integrity import record_known_breaks

    _, second, _, _ = _lab_case(session)
    record_known_breaks(KEY, session, user_id=None)
    session.commit()
    second_id = second.id

    session.delete(second)
    session.commit()

    result = _verify(session)
    assert result.ok is False
    assert result.broken_row_id == second_id


def test_a_deleted_fork_leaf_is_a_break(session):
    """`first` is a leaf: nothing chains from it, so without recording it,
    deleting it would leave the chain looking intact."""
    from app.audit_integrity import record_known_breaks

    first, _, _, _ = _lab_case(session)
    record_known_breaks(KEY, session, user_id=None)
    session.commit()
    first_id = first.id

    session.delete(first)
    session.commit()

    result = _verify(session)
    assert result.ok is False
    assert result.broken_row_id == first_id


def test_a_deleted_row_that_was_not_recorded_is_still_a_break(session):
    from app.audit_integrity import record_known_breaks

    _, _, _, after = _lab_case(session)
    following = record_audit_event(session, user_id=None, action="following")
    session.commit()
    record_known_breaks(KEY, session, user_id=None)
    session.commit()

    session.delete(after)
    session.commit()

    result = _verify(session)
    assert result.ok is False
    assert result.broken_row_id == following.id


def test_a_fork_after_the_cutoff_is_not_offered_for_recording(session):
    import pytest
    from app.audit_integrity import NoRaceBreaksError, record_known_breaks

    _, second, _, _ = _lab_case(session)
    _set_cutoff(session, second.id - 1)

    result = _verify(session)
    assert result.race_break_ids == []
    assert session.exec(select(AuditChainState)).first().status == "broken"
    with pytest.raises(NoRaceBreaksError):
        record_known_breaks(KEY, session, user_id=None)


def test_without_a_cutoff_nothing_is_offered(session):
    """A fresh install on 0.5.3 or later can't have race breaks."""
    _lab_case(session)
    _set_cutoff(session, None)

    result = _verify(session)
    assert result.race_break_ids == []
    assert session.exec(select(AuditChainState)).first().status == "broken"


def test_recorded_breaks_survive_a_retention_purge_of_their_parent(session):
    from app.audit_integrity import record_known_breaks
    from sqlmodel import delete

    _, second, _, _ = _lab_case(session)
    record_known_breaks(KEY, session, user_id=None)
    session.commit()

    # What app.audit_purge does: anchor at the newest purged row, then
    # delete the prefix. Here that cuts through the fork.
    newest_purged = session.get(AuditLog, second.id - 1)
    state = session.exec(select(AuditChainState)).first()
    state.anchor_row_id = newest_purged.id
    state.anchor_hash = newest_purged.row_hash
    session.add(state)
    session.exec(delete(AuditLog).where(AuditLog.id < second.id))
    session.commit()

    assert _verify(session).ok is True


@pytest.fixture(params=[None, 1, 2], ids=["default-block", "block-1", "block-2"], autouse=True)
def _walk_block(request, monkeypatch):
    """#346: every test above verifies with the default block and with
    blocks small enough that the walk reads the chain in many queries, and
    must give the same result."""
    if request.param is not None:
        monkeypatch.setattr("app.audit_integrity.WALK_BLOCK", request.param)


def test_the_walk_reads_the_chain_in_blocks(session, monkeypatch):
    for action in "abcde":
        record_audit_event(session, user_id=None, action=action)
    session.commit()
    monkeypatch.setattr("app.audit_integrity.WALK_BLOCK", 2)

    statements = []

    from sqlalchemy import event

    def before(conn, cursor, statement, *args):
        table = AuditLog.__tablename__
        if f"FROM {table}" in statement and f"ORDER BY {table}.id" in statement:
            statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", before)
    try:
        result = verify_chain(KEY, session)
    finally:
        event.remove(engine, "before_cursor_execute", before)
    assert result.ok is True
    assert result.checked_count == 5
    # Blocks of 2, 2 and 1 rows.
    assert len(statements) == 3
