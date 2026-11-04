"""Tamper-evidence for AuditLog: an HMAC-SHA256 hash chain over its rows
(see ROADMAP.md's "Security hardening (pre-1.0)" section). Each row's
row_hash covers its own content plus the previous row's hash, so altering
or deleting a row anywhere in the chain changes every hash after it --
detectable by app.audit_integrity.verify_chain(). Keyed (via
app.crypto.audit_chain_key(), not a bare hashlib.sha256) so the chain can't
be silently recomputed by anyone who only has the SQLite file itself (a
stolen backup, or limited SQL access) using this project's own public
source -- a keyless hash chain is fully reproducible from public data with
the same public algorithm, which would defeat the point against exactly
the "compromised admin account" threat this feature exists for. Writing
(chain_new_entry, called from app.audit.record_audit_event) and backfilling
rows written before this feature existed (backfill_chain_if_needed, called
once at startup) share the same hashing function, so a fresh row and a
backfilled historical one are indistinguishable to verification.

Note on concurrency: two requests writing audit rows at once must not both
chain from the same tip, or verification reports a break that is no
tampering at all (#213; e2e caught it once SQLite ran in WAL mode, where
readers no longer wait for writers). So chain_new_entry first takes
SQLite's write lock for the caller's transaction (a no-op UPDATE), then
reads the tip: a second writer blocks at that UPDATE until the first
commits, and then sees its row. The process-wide lock is taken only after
the database lock, so it can't deadlock against a transaction that already
holds the write lock and audits a second time.
"""

import hashlib
import hmac
import json
import threading
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session, select

from app.auth.models import AuditChainState, AuditLog

_chain_write_lock = threading.Lock()


def compute_row_hash(
    key: bytes,
    prev_hash: str | None,
    *,
    user_id: int | None,
    action: str,
    target_type: str | None,
    target_id: int | None,
    timestamp: datetime,
    metadata: dict | None,
) -> str:
    """Sorted keys and fixed separators so the same row always hashes the
    same way regardless of dict ordering; the timestamp is normalized to
    its own isoformat() string rather than relying on datetime's default
    str() conversion via json.dumps's `default`."""
    canonical = json.dumps(
        {
            "prev_hash": prev_hash,
            "user_id": user_id,
            "action": action,
            "target_type": target_type,
            "target_id": target_id,
            "timestamp": timestamp.isoformat(),
            "metadata": metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def current_chain_tip(session: Session) -> str | None:
    """The hash the next AuditLog row should chain from: the most recently
    written row's own hash if any row exists, else the purge anchor's hash
    if everything before it was already purged under retention (see
    app.audit_purge.run_audit_purge_sweep), else None for a chain that
    hasn't started yet (a brand-new install, or before the first backfill).
    Doesn't need the HMAC key itself -- it only reads an already-computed
    hash, never recomputes one."""
    last_hash = session.exec(
        select(AuditLog.row_hash).order_by(AuditLog.id.desc()).limit(1)
    ).first()
    if last_hash is not None:
        return last_hash
    state = session.exec(select(AuditChainState)).first()
    return state.anchor_hash if state else None


def _take_write_lock(session: Session) -> None:
    """Starts (or joins) the caller's write transaction before the tip is
    read, so the tip can't change under it before it commits. On SQLite an
    UPDATE matching no rows still takes the write lock, which is then held
    until the caller's commit or rollback."""
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("UPDATE audit_chain_state SET id = id WHERE 0"))


def chain_new_entry(key: bytes, session: Session, entry: AuditLog) -> None:
    """Sets prev_hash/row_hash on a not-yet-added AuditLog entry, in place.
    Relies on the session's default autoflush: if an earlier
    record_audit_event call in the same transaction already added a row,
    current_chain_tip's query flushes it first, so two audit writes in one
    request still chain correctly relative to each other. Also holds a
    process-wide lock across the read-tip-then-set step -- see this
    module's own docstring for what that does and doesn't protect against
    across separate transactions."""
    _take_write_lock(session)
    with _chain_write_lock:
        prev_hash = current_chain_tip(session)
        entry.prev_hash = prev_hash
        entry.row_hash = compute_row_hash(
            key,
            prev_hash,
            user_id=entry.user_id,
            action=entry.action,
            target_type=entry.target_type,
            target_id=entry.target_id,
            timestamp=entry.timestamp,
            metadata=entry.event_metadata,
        )


def backfill_chain_if_needed(key: bytes, session: Session) -> int:
    """Chains any AuditLog rows still missing a hash -- rows written before
    this feature existed -- in id order, starting from whatever the chain's
    tip already is (None for a brand-new install with no anchor yet).
    Idempotent: a second call with nothing left to backfill is a no-op.
    Must run before anything else in this process can call
    chain_new_entry/record_audit_event, or a freshly-written row (highest
    id, already hashed) would get mistaken for the chain's tip ahead of the
    still-unhashed legacy rows below it -- app.main's lifespan calls this
    first in its startup session, before any of the seed_* calls. Returns
    the number of rows backfilled, for the caller to log."""
    rows = session.exec(
        select(AuditLog).where(AuditLog.row_hash.is_(None)).order_by(AuditLog.id.asc())
    ).all()
    if not rows:
        return 0

    prev_hash = current_chain_tip(session)
    for row in rows:
        row.prev_hash = prev_hash
        row.row_hash = compute_row_hash(
            key,
            prev_hash,
            user_id=row.user_id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            timestamp=row.timestamp,
            metadata=row.event_metadata,
        )
        session.add(row)
        prev_hash = row.row_hash
    return len(rows)
