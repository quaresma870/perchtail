"""The audit outbox (app/audit_outbox.py): events a migration recorded
without the chain key become ordinary signed entries at the end of the
chain; anything else in it is refused and audited."""

import pytest
from app.audit import record_audit_event
from app.audit_integrity import verify_chain
from app.audit_outbox import REJECTED_ACTION, drain_audit_outbox
from app.auth.models import AuditLog, AuditOutbox
from app.crypto import audit_chain_key
from sqlalchemy import text
from sqlmodel import select

KEY = audit_chain_key()
RAISED = {"audit_retention_days": {"old": 30, "new": 90}}


def _last(session) -> AuditLog:
    return session.exec(select(AuditLog).order_by(AuditLog.id.desc())).first()


def _outbox_ids(session) -> list[int]:
    return list(session.exec(select(AuditOutbox.id)).all())


def test_an_accepted_entry_is_signed_at_the_end_of_the_chain_and_removed(session):
    first = record_audit_event(session, user_id=None, action="role.create")
    session.commit()
    session.add(AuditOutbox(action="system_settings.update", event_metadata=RAISED))
    session.commit()

    assert drain_audit_outbox(session) == (1, 0)

    entry = _last(session)
    assert entry.action == "system_settings.update"
    assert entry.target_type == "system_settings"
    assert entry.user_id is None
    assert entry.event_metadata == RAISED
    assert entry.prev_hash == first.row_hash
    assert entry.row_hash is not None
    assert _outbox_ids(session) == []
    assert verify_chain(KEY, session).ok
    # Nothing left for the next start.
    assert drain_audit_outbox(session) == (0, 0)


@pytest.mark.parametrize(
    ("action", "metadata"),
    [
        ("user.delete", {"username": "x"}),
        ("audit.purge", None),
        ("system_settings.update", {"audit_view_enabled": {"old": True, "new": False}}),
        ("system_settings.update", {**RAISED, "extra": 1}),
        ("system_settings.update", {"audit_retention_days": {"old": 90, "new": 30}}),
        ("system_settings.update", {"audit_retention_days": {"old": True, "new": 90}}),
        ("system_settings.update", {"audit_retention_days": "changed"}),
    ],
)
def test_anything_else_is_refused_audited_and_removed(session, action, metadata):
    session.add(AuditOutbox(action=action, event_metadata=metadata))
    session.commit()
    [outbox_id] = _outbox_ids(session)

    assert drain_audit_outbox(session) == (0, 1)

    entry = _last(session)
    assert entry.action == REJECTED_ACTION
    assert entry.user_id is None
    assert entry.event_metadata["outbox_id"] == outbox_id
    assert entry.event_metadata["action"] == action
    assert entry.event_metadata["created_at"]
    # The refused entry's own metadata isn't copied into the audit log.
    assert set(entry.event_metadata) == {"outbox_id", "action", "created_at"}
    assert session.exec(select(AuditLog).where(AuditLog.action == action)).all() == []
    assert _outbox_ids(session) == []
    assert verify_chain(KEY, session).ok


def test_values_that_dont_parse_are_refused_without_stopping(session):
    session.execute(
        text(
            "INSERT INTO audit_outbox (action, metadata, created_at) "
            "VALUES ('system_settings.update', 'not json', 'not a date')"
        )
    )
    session.commit()

    assert drain_audit_outbox(session) == (0, 1)
    entry = _last(session)
    assert entry.action == REJECTED_ACTION
    assert entry.event_metadata["created_at"] == "not a date"
    assert _outbox_ids(session) == []


def test_entries_are_recorded_in_order(session):
    session.add(AuditOutbox(action="system_settings.update", event_metadata=RAISED))
    session.add(AuditOutbox(action="forged", event_metadata=None))
    session.commit()

    assert drain_audit_outbox(session) == (1, 1)
    actions = [row.action for row in session.exec(select(AuditLog).order_by(AuditLog.id)).all()]
    assert actions == ["system_settings.update", REJECTED_ACTION]
    assert verify_chain(KEY, session).ok
