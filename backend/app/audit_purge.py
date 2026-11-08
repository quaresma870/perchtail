from datetime import timedelta

from sqlmodel import Session, delete, func, select

from app import system_settings
from app.audit_hash_chain import chain_maintenance_lock
from app.auth.models import AuditChainState, AuditLog
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)

# Rows deleted per transaction (#346).
CHUNK_SIZE = 5000


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
        # later sweep instead of purged now. The rows up to the boundary go
        # in chunks, see _purge_chunk.
        first_not_old_id = session.exec(
            select(func.min(AuditLog.id)).where(AuditLog.timestamp >= cutoff)
        ).one()
        if first_not_old_id is not None:
            last_id = first_not_old_id - 1
        else:
            # Every row is old enough: purge up to the newest one there is
            # now, so a row written while the chunks below run is never
            # touched.
            last_id = session.exec(select(func.max(AuditLog.id))).one()

    deleted = 0
    if last_id is not None:
        while True:
            # Per chunk, so a verification waits for one chunk at most,
            # never for the whole purge.
            with chain_maintenance_lock:
                count = _purge_chunk(engine, last_id)
            if count == 0:
                break
            deleted += count
    logger.info(
        "audit_purge.sweep_complete",
        deleted=deleted,
        retention_days=retention_days,
    )


def _purge_chunk(engine, last_id: int) -> int:
    """Deletes the oldest CHUNK_SIZE rows with an id up to `last_id` in one
    short transaction (#346), so the purge never holds SQLite's write lock
    for long, and returns how many went.

    The chain anchor advances to the newest row of the chunk *before* it
    is deleted, in the same transaction -- see AuditChainState's docstring
    in app/auth/models.py -- so app.audit_integrity.verify_chain() knows
    what hash the first surviving row should chain from, instead of this
    purge itself looking like a tamper break on the next check. Each chunk
    is an id prefix of what's left, so the anchor stays valid between
    chunks too."""
    with Session(engine) as session:
        chunk_end = session.exec(
            select(func.max(AuditLog.id)).where(
                AuditLog.id.in_(
                    select(AuditLog.id)
                    .where(AuditLog.id <= last_id)
                    .order_by(AuditLog.id.asc())
                    .limit(CHUNK_SIZE)
                )
            )
        ).one()
        if chunk_end is None:
            return 0
        newest_purged = session.get(AuditLog, chunk_end)
        state = session.exec(select(AuditChainState)).first()
        if state is None:
            state = AuditChainState()
            session.add(state)
        state.anchor_row_id = newest_purged.id
        state.anchor_hash = newest_purged.row_hash
        result = session.exec(delete(AuditLog).where(AuditLog.id <= chunk_end))
        session.commit()
        return result.rowcount
