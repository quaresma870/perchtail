"""The audit outbox: events a migration has to record without the key the
audit hash chain is signed with (a migration has no access to it).

A migration writes such an event to the audit_outbox table (action,
metadata, when). The next start, once the chain has been backfilled and
checked (app/main.py), turns each entry into an ordinary audit entry with
app.audit.record_audit_event -- signed when written, at the end of the
chain, with no user -- and deletes it, in one transaction. No AuditLog row
is ever signed after it was written (app/audit_hash_chain.py), so whoever
can write to the outbox can only add new, visible entries at the end of
the chain, never rewrite what is already there.

Only the events in ACCEPTED are recorded, and only with the exact metadata
shape their migration writes. Any other entry is refused: logged at ERROR,
audited as `audit.outbox_rejected` (with the outbox id, the action, and
when it was written; not its metadata) and deleted.
"""

import json
from collections.abc import Callable

from sqlalchemy import text
from sqlmodel import Session

from app.audit import record_audit_event
from app.auth.models import AuthProviderType, Capability, ScopeType
from app.logging_config import get_logger

logger = get_logger(__name__)

REJECTED_ACTION = "audit.outbox_rejected"
# Longest action name and timestamp copied into the rejection entry.
_TEXT_MAX = 100


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _retention_raised(metadata: object) -> bool:
    """c7f2a9e4d136: a retention between 1 and 89 days raised to 90."""
    if not isinstance(metadata, dict) or set(metadata) != {"audit_retention_days"}:
        return False
    change = metadata["audit_retention_days"]
    if not isinstance(change, dict) or set(change) != {"old", "new"}:
        return False
    old, new = change["old"], change["new"]
    return _is_int(old) and _is_int(new) and 0 < old < new


_DUPLICATE_GRANT_KEYS = {
    "reason",
    "grant_id",
    "kept_grant_id",
    "role_id",
    "scope_type",
    "scope_id",
    "capabilities",
}


def _duplicate_grant_deleted(metadata: object) -> bool:
    """a8d3f5c27e14: a role's second grant for the same scope deleted, the
    one with the lowest id kept."""
    if not isinstance(metadata, dict) or set(metadata) != _DUPLICATE_GRANT_KEYS:
        return False
    if metadata["reason"] != "duplicate_scope":
        return False
    ids = (metadata[key] for key in ("grant_id", "kept_grant_id", "role_id", "scope_id"))
    if not all(_is_int(value) for value in ids):
        return False
    if not metadata["kept_grant_id"] < metadata["grant_id"]:
        return False
    if metadata["scope_type"] not in {scope.value for scope in ScopeType}:
        return False
    capabilities = metadata["capabilities"]
    allowed = {capability.value for capability in Capability}
    return isinstance(capabilities, list) and all(c in allowed for c in capabilities)


def _sso_password_cleared(metadata: object) -> bool:
    """e5c1b7a94d26: the password left on an SSO account removed."""
    if not isinstance(metadata, dict):
        return False
    if set(metadata) != {"reason", "user_id", "username", "auth_provider"}:
        return False
    sso_providers = {p.value for p in AuthProviderType if p != AuthProviderType.local}
    return (
        metadata["reason"] == "sso_account"
        and _is_int(metadata["user_id"])
        and isinstance(metadata["username"], str)
        and metadata["auth_provider"] in sso_providers
    )


def _source_customer_from_folder(metadata: object) -> bool:
    """a7c3e9d2f514: a source in a folder given the folder's customer."""
    if not isinstance(metadata, dict):
        return False
    if set(metadata) != {"reason", "source_id", "folder_id", "changes"}:
        return False
    if metadata["reason"] != "folder_customer":
        return False
    if not (_is_int(metadata["source_id"]) and _is_int(metadata["folder_id"])):
        return False
    changes = metadata["changes"]
    if not isinstance(changes, dict) or set(changes) != {"customer_id"}:
        return False
    change = changes["customer_id"]
    if not isinstance(change, dict) or set(change) != {"from", "to"}:
        return False
    old, new = change["from"], change["to"]
    return (old is None or _is_int(old)) and _is_int(new) and old != new


# action -> (target_type, check of the metadata, metadata key of the target
# id or None).
ACCEPTED: dict[str, tuple[str, Callable[[object], bool], str | None]] = {
    "system_settings.update": ("system_settings", _retention_raised, None),
    "role_grant.delete": ("role_grant", _duplicate_grant_deleted, "grant_id"),
    "user.password_cleared": ("user", _sso_password_cleared, "user_id"),
    "source.update": ("source", _source_customer_from_folder, "source_id"),
}


def _parse(raw: object) -> object:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _short(value: object) -> str | None:
    return None if value is None else str(value)[:_TEXT_MAX]


def drain_audit_outbox(session: Session) -> tuple[int, int]:
    """Records every outbox entry in the audit log, or refuses it, in id
    order, one transaction per entry (the audit entry and the deletion
    together). Read as raw text, so an entry written outside a migration
    with values that don't parse is refused rather than stopping the start.
    Commits. Returns (recorded, rejected)."""
    recorded = rejected = 0
    rows = session.execute(
        text("SELECT id, action, metadata, created_at FROM audit_outbox ORDER BY id")
    ).all()
    for outbox_id, action, raw_metadata, created_at in rows:
        accepted = ACCEPTED.get(action) if isinstance(action, str) else None
        metadata = _parse(raw_metadata)
        if accepted is not None and accepted[1](metadata):
            target_type, _check, target_key = accepted
            record_audit_event(
                session,
                user_id=None,
                action=action,
                target_type=target_type,
                target_id=metadata[target_key] if target_key else None,
                metadata=metadata,
            )
            recorded += 1
        else:
            fields = {
                "outbox_id": outbox_id,
                "action": _short(action),
                "created_at": _short(created_at),
            }
            logger.error("audit_outbox.rejected", **fields)
            record_audit_event(
                session,
                user_id=None,
                action=REJECTED_ACTION,
                target_type="audit_log",
                metadata=fields,
            )
            rejected += 1
        session.execute(text("DELETE FROM audit_outbox WHERE id = :id"), {"id": outbox_id})
        session.commit()
    return recorded, rejected
