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


# action -> (target_type, check of the metadata).
ACCEPTED: dict[str, tuple[str, Callable[[object], bool]]] = {
    "system_settings.update": ("system_settings", _retention_raised),
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
            record_audit_event(
                session,
                user_id=None,
                action=action,
                target_type=accepted[0],
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
