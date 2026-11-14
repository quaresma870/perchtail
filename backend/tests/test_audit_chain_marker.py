"""The audit chain marker in DATA_DIR (app.audit_hash_chain.ChainMarker):
once the chain has started, a database whose hashes were taken away is
never signed again, and verification reports it."""

import json
import os
import stat

from app import audit_hash_chain
from app.audit import record_audit_event
from app.audit_hash_chain import (
    CHAIN_MARKER_NAME,
    backfill_chain_if_needed,
    ensure_chain_marker,
    read_chain_marker,
    unsigned_row_ids,
)
from app.audit_integrity import (
    FAILED_CHECK_ACTION,
    report_unsigned_audit_rows,
    verify_chain,
)
from app.audit_outbox import drain_audit_outbox
from app.audit_purge import sign_legacy_anchor
from app.auth.models import AuditChainState, AuditLog
from app.crypto import audit_chain_key
from sqlalchemy import text
from sqlmodel import select

KEY = audit_chain_key()


def chain_marker_path(data_dir=None):
    # Through the module: each test has its own (conftest.py).
    return audit_hash_chain.chain_marker_path(data_dir)


def _start(session) -> int:
    """What app.main's lifespan does with the audit log at startup."""
    marker = read_chain_marker()
    signed = backfill_chain_if_needed(KEY, session, marker=marker)
    sign_legacy_anchor(session, marker_present=marker is not None)
    session.commit()
    report_unsigned_audit_rows(KEY, session)
    drain_audit_outbox(session)
    session.commit()
    ensure_chain_marker(session)
    return signed


def _strip_the_chain(session) -> None:
    """The review's scenario: whoever can write to the database takes every
    hash and the anchor away, so the chain looks like it never started."""
    session.execute(text("UPDATE auditlog SET row_hash = NULL, prev_hash = NULL"))
    session.execute(text("UPDATE audit_chain_state SET anchor_hash = NULL, anchor_row_id = NULL"))
    session.commit()
    session.expire_all()


def test_a_new_install_writes_the_marker_with_its_first_row(session):
    assert read_chain_marker() is None
    # Seeding a new install audits the first role and user.
    first = record_audit_event(session, user_id=None, action="role.create")
    record_audit_event(session, user_id=None, action="user.create")
    session.commit()

    assert _start(session) == 0

    marker = read_chain_marker()
    assert marker is not None
    assert marker.first_row_id == first.id
    content = json.loads(chain_marker_path().read_text(encoding="utf-8"))
    assert content["first_row_id"] == first.id
    assert content["started_at"]
    if os.name == "posix":
        assert stat.S_IMODE(chain_marker_path().stat().st_mode) == 0o600
    # Written once; later starts leave it alone.
    assert ensure_chain_marker(session) is False


def test_an_old_install_without_a_chain_is_signed_once_and_gets_the_marker(session):
    rows = [AuditLog(user_id=None, action=f"legacy.{i}") for i in range(3)]
    for row in rows:
        session.add(row)
    session.commit()

    assert _start(session) == 3
    assert unsigned_row_ids(session) == []
    assert read_chain_marker().first_row_id == rows[0].id
    assert verify_chain(KEY, session).ok

    # A second start signs nothing.
    assert _start(session) == 0


def test_with_the_marker_a_stripped_chain_is_detected_and_nothing_is_signed(session):
    for action in ("a", "b", "c"):
        record_audit_event(session, user_id=None, action=action)
    session.commit()
    _start(session)
    original_ids = [row.id for row in session.exec(select(AuditLog)).all()]
    first_row = original_ids[0]

    _strip_the_chain(session)
    # Rewrite history at will, now that nothing is signed.
    session.execute(text("UPDATE auditlog SET action = 'rewritten' WHERE action = 'b'"))
    session.commit()

    assert _start(session) == 0
    # Nothing that was there was signed again.
    for row_id in original_ids:
        assert session.get(AuditLog, row_id).row_hash is None

    state = session.exec(select(AuditChainState)).one()
    assert state.status == "broken"
    assert state.broken_row_id == first_row
    failed = session.exec(select(AuditLog).where(AuditLog.action == FAILED_CHECK_ACTION)).all()
    assert failed[-1].event_metadata["reason"] == "chain_reset"
    assert failed[-1].event_metadata["broken_row_id"] == first_row

    # Still detected on the next start and by the scheduled check, even
    # though the failed check entry is now a signed row.
    _start(session)
    result = verify_chain(KEY, session)
    assert not result.ok
    assert result.reason == "chain_reset"
    assert result.checked_count == 0
    for row_id in original_ids:
        assert session.get(AuditLog, row_id).row_hash is None


def test_with_the_marker_an_emptied_audit_log_is_detected(session):
    record_audit_event(session, user_id=None, action="a")
    session.commit()
    _start(session)
    first_row = read_chain_marker().first_row_id

    session.execute(text("DELETE FROM auditlog"))
    session.commit()

    result = verify_chain(KEY, session)
    assert not result.ok
    assert result.reason == "chain_reset"
    assert result.broken_row_id == first_row


def test_with_the_marker_rows_above_the_first_that_lost_their_hashes_stay_unsigned(session):
    # The second review's scenario: the marker's first row F stays signed,
    # every row above it loses its hash and is rewritten at will.
    for action in ("a", "b", "c", "d"):
        record_audit_event(session, user_id=None, action=action)
    session.commit()
    _start(session)
    first_row = read_chain_marker().first_row_id
    session.execute(
        text("UPDATE auditlog SET row_hash = NULL, prev_hash = NULL WHERE id > :f"),
        {"f": first_row},
    )
    session.execute(text("UPDATE auditlog SET action = 'rewritten' WHERE action = 'c'"))
    session.commit()
    session.expire_all()
    stripped = unsigned_row_ids(session)
    assert stripped

    assert _start(session) == 0
    for row_id in stripped:
        assert session.get(AuditLog, row_id).row_hash is None
    state = session.exec(select(AuditChainState)).one()
    assert state.status == "broken"
    assert state.broken_row_id == stripped[0]
    failed = session.exec(select(AuditLog).where(AuditLog.action == FAILED_CHECK_ACTION)).all()
    assert failed[-1].event_metadata["reason"] == "unsigned_row"
    assert failed[-1].event_metadata["broken_row_id"] == stripped[0]


def test_with_the_marker_a_legacy_anchor_is_not_recorded(session):
    session.add(AuditChainState(anchor_row_id=9, anchor_hash="anchor-hash"))
    session.commit()

    assert sign_legacy_anchor(session, marker_present=True) is False
    assert sign_legacy_anchor(session, marker_present=False) is True


def test_an_unreadable_marker_still_counts(session):
    chain_marker_path().write_text("not json", encoding="utf-8")
    session.add(AuditLog(user_id=None, action="legacy"))
    session.commit()

    marker = read_chain_marker()
    assert marker is not None and marker.first_row_id is None
    assert backfill_chain_if_needed(KEY, session, marker=marker) == 0
    result = verify_chain(KEY, session)
    assert result.reason == "chain_reset"


def test_the_marker_lives_in_data_dir(tmp_path):
    assert chain_marker_path(tmp_path / "db") == tmp_path / "db" / CHAIN_MARKER_NAME
