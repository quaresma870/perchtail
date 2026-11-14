"""The hash format per row (app/audit_hash_chain.py): rows written before
0.7.0 verify by the fields they had, new rows also cover where the action
came from."""

import pytest
from app import request_context
from app.audit import record_audit_event
from app.audit_hash_chain import CURRENT_HASH_FORMAT, backfill_chain_if_needed
from app.audit_integrity import record_failed_check, verify_chain
from app.auth.models import AuditLog
from sqlmodel import select

KEY = b"unit-test-key-not-a-real-secret"


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    monkeypatch.setattr("app.audit.audit_chain_key", lambda: KEY)


@pytest.fixture()
def in_request():
    """record_audit_event as if inside a request from 198.51.100.7."""
    token = request_context._current.set(
        request_context.RequestContext(
            client_ip="198.51.100.7", user_agent="Browser/1.0", session_id="0123456789abcdef"
        )
    )
    yield
    request_context._current.reset(token)


def _legacy_and_new_rows(session):
    # Written before 0.7.0: no origin, no hash format, chained at startup.
    session.add(AuditLog(user_id=None, action="source.create"))
    session.add(AuditLog(user_id=None, action="source.update"))
    session.commit()
    backfill_chain_if_needed(KEY, session)
    session.commit()


def test_old_and_new_rows_verify_together(session, in_request):
    _legacy_and_new_rows(session)
    entry = record_audit_event(session, user_id=None, action="user.logout")
    session.commit()

    rows = session.exec(select(AuditLog).order_by(AuditLog.id)).all()
    assert [row.hash_format for row in rows] == [None, None, CURRENT_HASH_FORMAT]
    assert (entry.client_ip, entry.user_agent, entry.session_id) == (
        "198.51.100.7",
        "Browser/1.0",
        "0123456789abcdef",
    )
    result = verify_chain(KEY, session)
    assert result.ok
    assert result.checked_count == 3


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("client_ip", "203.0.113.1"),
        ("user_agent", "Other/2.0"),
        ("session_id", "ffffffffffffffff"),
        ("hash_format", None),
    ],
)
def test_changing_a_new_rows_origin_is_detected(session, in_request, field, value):
    _legacy_and_new_rows(session)
    entry = record_audit_event(session, user_id=None, action="user.logout")
    session.commit()

    setattr(entry, field, value)
    session.add(entry)
    session.commit()
    result = verify_chain(KEY, session)
    assert not result.ok
    assert result.broken_row_id == entry.id


def test_a_changed_old_row_is_still_detected(session):
    _legacy_and_new_rows(session)
    first = session.exec(select(AuditLog).order_by(AuditLog.id)).first()
    first.action = "source.delete"
    session.add(first)
    session.commit()
    assert not verify_chain(KEY, session).ok


def test_outside_a_request_the_origin_is_empty(session):
    entry = record_audit_event(session, user_id=None, action="audit.purge")
    session.commit()
    assert (entry.client_ip, entry.user_agent, entry.session_id) == (None, None, None)
    assert entry.hash_format == CURRENT_HASH_FORMAT
    assert verify_chain(KEY, session).ok


def test_a_failed_verification_is_audited(session):
    _legacy_and_new_rows(session)
    first = session.exec(select(AuditLog).order_by(AuditLog.id)).first()
    first.action = "source.delete"
    session.add(first)
    session.commit()

    result = verify_chain(KEY, session)
    session.commit()
    record_failed_check(session, result, user_id=None)
    session.commit()

    [entry] = session.exec(
        select(AuditLog).where(AuditLog.action == "audit.integrity_check_failed")
    ).all()
    assert entry.event_metadata["status"] == "broken"
    assert entry.event_metadata["broken_row_id"] == first.id


def test_the_scheduled_check_audits_a_failure(monkeypatch, session):
    from app.audit_integrity import run_audit_integrity_check

    monkeypatch.setattr("app.db.engine", session.get_bind())
    monkeypatch.setattr("app.crypto.audit_chain_key", lambda: KEY)
    _legacy_and_new_rows(session)
    first = session.exec(select(AuditLog).order_by(AuditLog.id)).first()
    first.action = "source.delete"
    session.add(first)
    session.commit()

    run_audit_integrity_check()
    session.expire_all()
    assert session.exec(
        select(AuditLog).where(AuditLog.action == "audit.integrity_check_failed")
    ).all()


def test_the_scheduled_check_writes_nothing_when_intact(monkeypatch, session):
    from app.audit_integrity import run_audit_integrity_check

    monkeypatch.setattr("app.db.engine", session.get_bind())
    monkeypatch.setattr("app.crypto.audit_chain_key", lambda: KEY)
    _legacy_and_new_rows(session)

    run_audit_integrity_check()
    session.expire_all()
    assert (
        session.exec(
            select(AuditLog).where(AuditLog.action == "audit.integrity_check_failed")
        ).all()
        == []
    )
