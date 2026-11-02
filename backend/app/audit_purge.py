from datetime import timedelta

from sqlalchemy import true
from sqlmodel import Session, delete, func, select

from app import system_settings
from app.auth.models import AuditChainState, AuditLog
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

        # Purge a genuine id-prefix of AuditLog, not just "whatever matches
        # timestamp < cutoff" directly -- app.audit_hash_chain's chain
        # links rows by id order, so the chain anchor below is only valid
        # if what's deleted is a clean prefix of it. A row's timestamp is
        # captured at construction, before it's actually inserted/
        # committed, so under concurrent writes (this app runs FastAPI sync
        # endpoints in a thread pool -- see ROADMAP.md's agent-registry
        # notes) two rows' timestamp order and id (commit) order can, very
        # rarely, diverge. first_not_old_id -- the smallest id among rows
        # NOT old enough to purge -- is, by that same minimality, always a
        # safe boundary: every row below it is guaranteed to already be
        # older than cutoff, even if a handful of genuinely-old rows above
        # it (an out-of-order straggler) get conservatively left for a
        # later sweep instead of purged now.
        first_not_old_id = session.exec(
            select(func.min(AuditLog.id)).where(AuditLog.timestamp >= cutoff)
        ).one()
        prefix_condition = (
            AuditLog.id < first_not_old_id if first_not_old_id is not None else true()
        )

        # Advance the chain anchor to the newest row about to be deleted,
        # *before* deleting -- see AuditChainState's docstring in
        # app/auth/models.py -- so app.audit_integrity.verify_chain() knows
        # what hash the first surviving row should chain from, instead of
        # this purge itself looking like a tamper break on the next check.
        # Only the single newest matching row is actually needed here, so
        # this fetches just that one rather than loading every row about to
        # be deleted into memory (the delete() below re-applies the same
        # condition as its own bulk statement, not by iterating this list).
        newest_purged = session.exec(
            select(AuditLog).where(prefix_condition).order_by(AuditLog.id.desc()).limit(1)
        ).first()
        if newest_purged is not None:
            state = session.exec(select(AuditChainState)).first()
            if state is None:
                state = AuditChainState()
                session.add(state)
            state.anchor_row_id = newest_purged.id
            state.anchor_hash = newest_purged.row_hash

        result = session.exec(delete(AuditLog).where(prefix_condition))
        session.commit()
        logger.info(
            "audit_purge.sweep_complete",
            deleted=result.rowcount,
            retention_days=retention_days,
        )
