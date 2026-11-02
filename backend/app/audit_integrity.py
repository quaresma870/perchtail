"""Periodic verification of the AuditLog hash chain (see
app/audit_hash_chain.py) -- the scheduled half of tamper-evidence. A chain
that's never checked doesn't actually catch anything on its own; this is
what turns "detectable in principle" into "actually detected." Runs on its
own configurable cadence (Settings.audit_integrity_check_interval_days),
deliberately independent of both the purge job's cadence and the retention
window itself (see ROADMAP.md's security-hardening notes) -- retention
decides what's kept, this decides how often what's kept gets re-verified,
and the two shouldn't be coupled just because they both touch AuditLog.
"""

import hmac
from dataclasses import dataclass

from sqlmodel import Session, select

from app.audit_hash_chain import compute_row_hash
from app.auth.models import AuditChainState, AuditLog
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)


@dataclass
class ChainVerificationResult:
    ok: bool
    checked_count: int
    broken_row_id: int | None


def _hashes_match(stored: str | None, expected: str | None) -> bool:
    """`None` only ever means "the very first row in the chain" (no
    predecessor, or a fresh install with nothing to compare against yet)
    -- `is` correctly says "match" only when both sides are that same
    singleton, and "no match" the instant exactly one side is a string.
    Once both sides are real hash strings, compares them via
    hmac.compare_digest rather than `==`: this is verifying an HMAC, so a
    timing side-channel on the comparison itself shouldn't leak anything
    about the expected value, same reasoning as this codebase's other
    secret/token comparisons (e.g. app/api/monitoring.py's bearer token
    check)."""
    if stored is None or expected is None:
        return stored is expected
    return hmac.compare_digest(stored, expected)


def verify_chain(key: bytes, session: Session) -> ChainVerificationResult:
    """Walks every remaining AuditLog row in id order, recomputing each
    row's hash (keyed the same way chain_new_entry/backfill_chain_if_needed
    compute it) from its own fields plus the previous row's -- or the purge
    anchor's, for the first surviving row, see AuditChainState's docstring
    -- and comparing against what's stored. Stops at the first mismatch,
    since everything after an altered/deleted row is unverifiable
    regardless of whether it individually still "looks" consistent.
    Persists the result onto the singleton AuditChainState row (does not
    commit -- same "caller's transaction" convention as
    record_audit_event) for the Audit Log page's integrity banner to read."""
    state = session.exec(select(AuditChainState)).first()
    expected_prev = state.anchor_hash if state else None

    rows = session.exec(select(AuditLog).order_by(AuditLog.id.asc())).all()
    broken_row_id: int | None = None
    checked = 0
    for row in rows:
        checked += 1
        recomputed = compute_row_hash(
            key,
            expected_prev,
            user_id=row.user_id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            timestamp=row.timestamp,
            metadata=row.event_metadata,
        )
        if not _hashes_match(row.prev_hash, expected_prev) or not _hashes_match(
            row.row_hash, recomputed
        ):
            broken_row_id = row.id
            break
        expected_prev = row.row_hash

    ok = broken_row_id is None
    if state is None:
        state = AuditChainState()
        session.add(state)
    state.last_checked_at = utcnow()
    state.status = "ok" if ok else "broken"
    state.broken_row_id = broken_row_id
    return ChainVerificationResult(ok=ok, checked_count=checked, broken_row_id=broken_row_id)


def run_audit_integrity_check() -> None:
    """The APScheduler job (see app/main.py's lifespan)."""
    # Imported here, not at module load, so a test can monkeypatch
    # "app.db.engine" and have it picked up -- same pattern as
    # app/audit_purge.py's run_audit_purge_sweep().
    from app.crypto import audit_chain_key
    from app.db import engine

    with Session(engine) as session:
        result = verify_chain(audit_chain_key(), session)
        session.commit()
        if result.ok:
            logger.info("audit_integrity.check_complete", checked=result.checked_count)
        else:
            logger.error(
                "audit_integrity.chain_broken",
                checked=result.checked_count,
                broken_row_id=result.broken_row_id,
            )
