from datetime import timedelta

from sqlmodel import Session, delete

from app import system_settings
from app.auth.models import AuditLog
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)


def run_audit_purge_sweep() -> None:
    """The APScheduler job (see app/main.py's lifespan) enforcing the
    admin-configurable audit-log retention window (Settings -> System). A
    retention of 0 means "keep forever" -- see
    system_settings.INT_DEFAULTS's docstring -- so this no-ops rather than
    deleting anything in that case, same "explicit opt-in, conservative
    default" spirit as the rule engine's zero-rules-matches-nothing rule."""
    # Imported here, not at module load, so a test can monkeypatch
    # "app.db.engine" and have it picked up -- same pattern as
    # app/search_index.py's run_indexing_sweep().
    from app.db import engine

    with Session(engine) as session:
        retention_days = system_settings.get_int(session, "audit_retention_days")
        if retention_days <= 0:
            return

        cutoff = utcnow() - timedelta(days=retention_days)
        result = session.exec(delete(AuditLog).where(AuditLog.timestamp < cutoff))
        session.commit()
        logger.info(
            "audit_purge.sweep_complete",
            deleted=result.rowcount,
            retention_days=retention_days,
        )
