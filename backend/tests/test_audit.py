from app.audit import record_audit_event
from app.auth.models import AuditLog
from sqlmodel import select


def test_record_audit_event_sets_all_given_fields(session):
    entry = record_audit_event(
        session,
        user_id=7,
        action="source.create",
        target_type="source",
        target_id=42,
        metadata={"name": "prod-web-01"},
    )
    assert entry.user_id == 7
    assert entry.action == "source.create"
    assert entry.target_type == "source"
    assert entry.target_id == 42
    assert entry.event_metadata == {"name": "prod-web-01"}


def test_record_audit_event_defaults_optional_fields_to_none(session):
    entry = record_audit_event(session, user_id=None, action="login")
    assert entry.user_id is None
    assert entry.target_type is None
    assert entry.target_id is None
    assert entry.event_metadata is None


def test_record_audit_event_does_not_commit(session):
    record_audit_event(session, user_id=1, action="login")
    # Staged in this session's pending changes...
    assert any(isinstance(obj, AuditLog) for obj in session.new)
    # ...but not yet visible to a fresh query against the same transaction
    # unless the caller commits — record_audit_event is documented to leave
    # committing to the caller so it can share the caller's transaction.
    session.rollback()
    assert session.exec(select(AuditLog)).all() == []


def test_record_audit_event_returned_entry_is_queryable_after_commit(session):
    record_audit_event(session, user_id=3, action="role.create", target_type="role", target_id=1)
    session.commit()
    rows = session.exec(select(AuditLog)).all()
    assert len(rows) == 1
    assert rows[0].action == "role.create"
