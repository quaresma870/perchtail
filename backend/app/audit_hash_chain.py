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
at startup, once, before the chain starts) share
the same hashing function, so a fresh row and a backfilled historical one
are indistinguishable to verification.

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
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, select

from app.auth.models import AuditChainState, AuditLog

_chain_write_lock = threading.Lock()

# Held by a verification for its whole walk (app/audit_integrity.py) and by
# the audit purge for each chunk it deletes (app/audit_purge.py): the walk
# reads the log in blocks, so a chunk deleted between two blocks, with the
# anchor moved, would look like a break that isn't there. Reentrant, since
# recording known breaks verifies again.
chain_maintenance_lock = threading.RLock()


# The hash format new rows are written in. Format 1 (stored as null, every
# row written before 0.7.0) covers the row's original fields; format 2 also
# covers where the action came from (client_ip, user_agent, session_id) and
# the format number itself. Each row is verified by its own format, so rows
# written before an upgrade keep verifying unchanged.
CURRENT_HASH_FORMAT = 2


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
    hash_format: int | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
    session_id: str | None = None,
) -> str:
    """Sorted keys and fixed separators so the same row always hashes the
    same way regardless of dict ordering; the timestamp is normalized to
    its own isoformat() string rather than relying on datetime's default
    str() conversion via json.dumps's `default`. `hash_format` picks the
    fields covered (see CURRENT_HASH_FORMAT); an unknown format hashes as
    format 2 with its own number, so it can never verify by accident."""
    fields = {
        "prev_hash": prev_hash,
        "user_id": user_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "timestamp": timestamp.isoformat(),
        "metadata": metadata,
    }
    if hash_format is not None and hash_format != 1:
        fields.update(
            {
                "hash_format": hash_format,
                "client_ip": client_ip,
                "user_agent": user_agent,
                "session_id": session_id,
            }
        )
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hmac.new(key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()


def row_hash_for(key: bytes, prev_hash: str | None, row) -> str:
    """compute_row_hash over an AuditLog row, or any object with its
    columns (a row read with select()), by the row's own hash format."""
    return compute_row_hash(
        key,
        prev_hash,
        user_id=row.user_id,
        action=row.action,
        target_type=row.target_type,
        target_id=row.target_id,
        timestamp=row.timestamp,
        metadata=row.event_metadata,
        hash_format=row.hash_format,
        client_ip=row.client_ip,
        user_agent=row.user_agent,
        session_id=row.session_id,
    )


def current_chain_tip(session: Session) -> str | None:
    """The hash the next AuditLog row should chain from: the most recently
    written row's own hash if any row exists, else the purge anchor's hash
    if everything before it was already purged under retention (see
    app.audit_purge.run_audit_purge_sweep), else None for a chain that
    hasn't started yet (a brand-new install, or before the first backfill).
    Rows without a hash are skipped: a row written outside the app is never
    part of the chain (verification reports it), so the next entry chains
    from the last signed one.
    Doesn't need the HMAC key itself -- it only reads an already-computed
    hash, never recomputes one."""
    last_hash = session.exec(
        select(AuditLog.row_hash)
        .where(AuditLog.row_hash.is_not(None))
        .order_by(AuditLog.id.desc())
        .limit(1)
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
        entry.hash_format = CURRENT_HASH_FORMAT
        entry.row_hash = row_hash_for(key, prev_hash, entry)


def _has_anchor(state: AuditChainState | None) -> bool:
    return state is not None and (state.anchor_hash is not None or state.anchor_row_id is not None)


def first_signed_row_id(session: Session) -> int | None:
    """The lowest id among the rows that have a hash, or None."""
    return session.exec(
        select(AuditLog.id)
        .where(AuditLog.row_hash.is_not(None))
        .order_by(AuditLog.id.asc())
        .limit(1)
    ).first()


def chain_started_in_database(session: Session, state: AuditChainState | None) -> bool:
    """Whether the database says a row was ever signed: a signed row is
    there, or a purge left an anchor (only a signed chain is ever purged).
    Whoever can write to the database controls both, so this alone never
    decides that the chain hasn't started: see ChainMarker."""
    return _has_anchor(state) or first_signed_row_id(session) is not None


# The chain marker: a file in DATA_DIR, outside the database, written when
# the chain starts (the first start of a version that has it, once the
# startup backfill and seeding have run) and never removed by the app.
# With it present, a database without hashes nor anchor isn't a chain that
# hasn't started yet but one whose hashes were taken away: nothing is
# signed late, and verification reports it (reason "chain_reset",
# app.audit_integrity). A restore (app/backup.py) writes it again when the
# restored database has a chain.
CHAIN_MARKER_NAME = ".audit-chain-started"


@dataclass(frozen=True)
class ChainMarker:
    path: Path
    started_at: str | None
    # The lowest signed row when the marker was written; None when the
    # audit log was still empty then.
    first_row_id: int | None


def chain_marker_path(data_dir: str | Path | None = None) -> Path:
    if data_dir is None:
        from app.config import get_settings  # deferred: tests rebuild Settings

        data_dir = get_settings().data_dir
    return Path(data_dir) / CHAIN_MARKER_NAME


def read_chain_marker(data_dir: str | Path | None = None) -> ChainMarker | None:
    """The marker in DATA_DIR, or None when there is none. A marker that
    can't be read or parsed still counts as present (fail closed): what
    matters is that the chain was started here."""
    path = chain_marker_path(data_dir)
    if not path.exists():
        return None
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        content = None
    if not isinstance(content, dict):
        return ChainMarker(path=path, started_at=None, first_row_id=None)
    first = content.get("first_row_id")
    started_at = content.get("started_at")
    return ChainMarker(
        path=path,
        started_at=started_at if isinstance(started_at, str) else None,
        first_row_id=first if isinstance(first, int) and not isinstance(first, bool) else None,
    )


def write_chain_marker(path: Path, first_row_id: int | None, started_at: str | None = None) -> None:
    """Writes the marker at path (0600), through a temporary file renamed
    into place."""
    content = {
        "started_at": started_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "first_row_id": first_row_id,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(content))
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def ensure_chain_marker(session: Session, data_dir: str | Path | None = None) -> bool:
    """Run at startup (app/main.py), once the backfill and seeding have
    run and committed: writes the marker when there is none, with the
    lowest signed row, and fills that row in on a marker written while the
    audit log was still empty (unless a row without a hash is there, which
    verification reports). Never removes it. Returns whether it wrote the
    file."""
    marker = read_chain_marker(data_dir)
    first = first_signed_row_id(session)
    if marker is None:
        write_chain_marker(chain_marker_path(data_dir), first)
        return True
    if marker.first_row_id is None and first is not None and not unsigned_row_ids(session, 1):
        write_chain_marker(marker.path, first, marker.started_at)
        return True
    return False


def chain_reset_row_id(
    session: Session, state: AuditChainState | None, marker: ChainMarker | None
) -> int | None:
    """With the marker present, the row to report when the database no
    longer holds the chain the marker says was started: no purge anchor
    and no signed row at or before the marker's first one (the hashes were
    taken away, or everything was deleted). The first row left, or the
    marker's first row when none is. None when there's no such reset.
    A purge anchor is checked elsewhere (app.audit_integrity)."""
    if marker is None or _has_anchor(state):
        return None
    first_signed = first_signed_row_id(session)
    first_row = session.exec(select(AuditLog.id).order_by(AuditLog.id.asc()).limit(1)).first()
    if marker.first_row_id is None:
        return first_row if first_signed is None else None
    if first_signed is not None and first_signed <= marker.first_row_id:
        return None
    return first_row if first_row is not None else marker.first_row_id


def backfill_chain_if_needed(
    key: bytes, session: Session, *, marker: ChainMarker | None = None
) -> int:
    """Signs the AuditLog rows written before this feature existed, in id
    order, once: only while the chain hasn't started yet (no row signed, no
    purge anchor, and no chain marker in DATA_DIR, `marker`, see
    ChainMarker), i.e. the first start after upgrading to the version that
    brought it.

    That is the only time a row is ever signed after it was written. Any
    other row without a hash is left as it is: nothing the app writes lacks
    one, so verification reports it as tampering (unsigned_row_ids and
    chain_reset_row_id, app.audit_integrity). An event a migration has to
    record goes through the audit outbox instead (app/audit_outbox.py).
    Idempotent. Must run before anything else in this process can call
    chain_new_entry/record_audit_event -- app.main's lifespan calls this
    first in its startup session, before any of the seed_* calls. Doesn't
    commit. Returns the number of rows signed, for the caller to log."""
    if marker is not None:
        # The marker says the chain was started: rows without a hash now
        # had theirs taken away (or were never the app's), and are never
        # signed.
        return 0
    state = session.exec(select(AuditChainState)).first()
    if chain_started_in_database(session, state):
        return 0
    rows = session.exec(
        select(AuditLog).where(AuditLog.row_hash.is_(None)).order_by(AuditLog.id.asc())
    ).all()
    if not rows:
        return 0

    prev_hash = current_chain_tip(session)
    for row in rows:
        # Their hash covers their own (original) fields, whatever format
        # they were stored with.
        row.prev_hash = prev_hash
        row.row_hash = row_hash_for(key, prev_hash, row)
        session.add(row)
        prev_hash = row.row_hash
    return len(rows)


def unsigned_row_ids(session: Session, limit: int | None = None) -> list[int]:
    """AuditLog rows with no hash, in id order. After the startup backfill
    there should be none: each one was written outside the app."""
    query = select(AuditLog.id).where(AuditLog.row_hash.is_(None)).order_by(AuditLog.id.asc())
    if limit is not None:
        query = query.limit(limit)
    return list(session.exec(query).all())


def resign_chain(key: bytes, session: Session) -> int:
    """Recomputes every row's hash under `key`, in id order from the purge
    anchor (whose stored hash is kept as it is): the credential key the
    chain is keyed from was rotated (app/rotate_credential_key.py). The
    caller verifies the chain under the old key first -- re-signing a
    tampered chain would vouch for the tampering. Doesn't commit. Returns
    the number of rows re-signed."""
    state = session.exec(select(AuditChainState)).first()
    prev_hash = state.anchor_hash if state else None
    from app.audit_integrity import KNOWN_BREAKS_ACTION  # deferred: it imports this module

    count = 0
    last_id = 0
    # Row id -> its new hash, for the known-break entries below.
    new_hashes: dict[int, str] = {}
    while True:
        # In blocks, not one cursor held open while rows are updated.
        rows = session.exec(
            select(AuditLog).where(AuditLog.id > last_id).order_by(AuditLog.id.asc()).limit(1000)
        ).all()
        if not rows:
            return count
        for row in rows:
            if row.action == KNOWN_BREAKS_ACTION and isinstance(row.event_metadata, dict):
                # A recorded known break names rows by their hash; those
                # rows were all re-signed above it, so it names the new
                # hashes (a new dict, so the JSON column is saved).
                row.event_metadata = _with_new_hashes(row.event_metadata, new_hashes)
            row.prev_hash = prev_hash
            row.row_hash = row_hash_for(key, prev_hash, row)
            session.add(row)
            prev_hash = row.row_hash
            new_hashes[row.id] = row.row_hash
            count += 1
        last_id = rows[-1].id


def _with_new_hashes(metadata: dict, new_hashes: dict[int, str]) -> dict:
    updated = dict(metadata)
    for key in ("breaks", "leaves"):
        items = metadata.get(key)
        if isinstance(items, list):
            updated[key] = [
                (
                    {**item, "row_hash": new_hashes.get(item.get("id"), item.get("row_hash"))}
                    if isinstance(item, dict)
                    else item
                )
                for item in items
            ]
    return updated
