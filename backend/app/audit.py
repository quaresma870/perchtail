from sqlmodel import Session

from app.audit_hash_chain import chain_new_entry
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
    added, same as every other AuditLog row."""
    entry = AuditLog(
        user_id=user_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        event_metadata=metadata,
    )
    chain_new_entry(audit_chain_key(), session, entry)
    session.add(entry)
    logger.info(action, user_id=user_id, target_type=target_type, target_id=target_id)
    return entry
