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
# a8d3f5c27e14: a duplicate grant deleted.
DUPLICATE = {
    "reason": "duplicate_scope",
    "grant_id": 11,
    "kept_grant_id": 10,
    "role_id": 1,
    "scope_type": "customer",
    "scope_id": 5,
    "capabilities": ["view", "download"],
}
# e5c1b7a94d26: the password left on an SSO account removed.
SSO_CLEARED = {
    "reason": "sso_account",
    "user_id": 7,
    "username": "sso@example.com",
    "auth_provider": "oidc",
}
# a7c3e9d2f514: a source in a folder given the folder's customer.
FOLDER_CUSTOMER = {
    "reason": "folder_customer",
    "source_id": 4,
    "folder_id": 2,
    "changes": {"customer_id": {"from": None, "to": 3}},
}


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
        ("role_grant.delete", {**DUPLICATE, "reason": "other"}),
        ("role_grant.delete", {**DUPLICATE, "kept_grant_id": 12}),
        ("role_grant.delete", {**DUPLICATE, "scope_type": "planet"}),
        ("role_grant.delete", {**DUPLICATE, "capabilities": ["root"]}),
        ("role_grant.delete", {**DUPLICATE, "capabilities": "view"}),
        ("role_grant.delete", {**DUPLICATE, "grant_id": True}),
        ("role_grant.delete", {**DUPLICATE, "extra": 1}),
        ("role_grant.delete", None),
        ("user.password_cleared", {**SSO_CLEARED, "reason": "other"}),
        ("user.password_cleared", {**SSO_CLEARED, "auth_provider": "local"}),
        ("user.password_cleared", {**SSO_CLEARED, "user_id": "7"}),
        ("user.password_cleared", {**SSO_CLEARED, "username": None}),
        ("user.password_cleared", {**SSO_CLEARED, "extra": 1}),
        ("user.password_cleared", None),
        ("source.update", {**FOLDER_CUSTOMER, "reason": "other"}),
        ("source.update", {**FOLDER_CUSTOMER, "source_id": "4"}),
        ("source.update", {**FOLDER_CUSTOMER, "folder_id": None}),
        ("source.update", {**FOLDER_CUSTOMER, "extra": 1}),
        ("source.update", {**FOLDER_CUSTOMER, "changes": {"customer_id": {"from": 3, "to": 3}}}),
        ("source.update", {**FOLDER_CUSTOMER, "changes": {"customer_id": {"from": 1, "to": None}}}),
        ("source.update", {**FOLDER_CUSTOMER, "changes": {"host": {"from": "a", "to": "b"}}}),
        ("source.update", {**FOLDER_CUSTOMER, "changes": {"customer_id": 3}}),
        ("source.update", None),
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


def test_a_deleted_duplicate_grant_is_recorded_against_the_grant(session):
    session.add(AuditOutbox(action="role_grant.delete", event_metadata=DUPLICATE))
    session.commit()

    assert drain_audit_outbox(session) == (1, 0)

    entry = _last(session)
    assert (entry.action, entry.target_type, entry.target_id) == (
        "role_grant.delete",
        "role_grant",
        11,
    )
    assert entry.user_id is None
    assert {key: entry.event_metadata[key] for key in DUPLICATE} == DUPLICATE
    assert verify_chain(KEY, session).ok


def test_a_cleared_sso_password_is_recorded_against_the_user(session):
    session.add(AuditOutbox(action="user.password_cleared", event_metadata=SSO_CLEARED))
    session.commit()

    assert drain_audit_outbox(session) == (1, 0)

    entry = _last(session)
    assert (entry.action, entry.target_type, entry.target_id, entry.user_id) == (
        "user.password_cleared",
        "user",
        7,
        None,
    )
    assert {key: entry.event_metadata[key] for key in SSO_CLEARED} == SSO_CLEARED
    assert verify_chain(KEY, session).ok


def test_a_source_given_its_folder_customer_is_recorded_against_the_source(session):
    session.add(AuditOutbox(action="source.update", event_metadata=FOLDER_CUSTOMER))
    session.commit()

    assert drain_audit_outbox(session) == (1, 0)

    entry = _last(session)
    assert (entry.action, entry.target_type, entry.target_id, entry.user_id) == (
        "source.update",
        "source",
        4,
        None,
    )
    assert {key: entry.event_metadata[key] for key in FOLDER_CUSTOMER} == FOLDER_CUSTOMER
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
