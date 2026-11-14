from sqlmodel import Session

from app import request_context
from app.audit_hash_chain import chain_new_entry
from app.audit_labels import audit_labels
from app.auth.models import AuditLog
from app.crypto import audit_chain_key
from app.logging_config import get_logger

logger = get_logger(__name__)


def record_audit_event(
    session: Session,
    *,
    user_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: int | None = None,
    metadata: dict | None = None,
) -> AuditLog:
    """Every AuditLog write also emits a structured INFO log line, so ops can
    grep for an action without querying SQLite (see CLAUDE.md's Application
    logging section). Does not commit — callers include this in their own
    transaction alongside whatever triggered it. Also chained into the
    tamper-evidence hash chain (app/audit_hash_chain.py) before being
    added, same as every other AuditLog row.

    The target's name, and the names next to related ids, are added to the
    metadata as the event is written (#239, see app/audit_labels.py), so
    the entry still says what it was about after a rename or a delete.
    Keys the caller sets win."""
    # Before chain_new_entry: the name lookups must run before it takes
    # SQLite's write lock, so they never lengthen the time it's held.
    labels = audit_labels(session, target_type, target_id, metadata)
    if labels:
        metadata = {**labels, **(metadata or {})}
    # Where it came from (app/request_context.py): empty outside a request.
    context = request_context.current()
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        event_metadata=metadata,
        client_ip=context.client_ip if context else None,
        user_agent=context.user_agent if context else None,
        session_id=context.session_id if context else None,
    )
    chain_new_entry(audit_chain_key(), session, entry)
    session.add(entry)
    logger.info(
        action,
        user_id=user_id,
        target_type=target_type,
        target_id=target_id,
        client_ip=entry.client_ip,
        session_id=entry.session_id,
    )
    return entry
