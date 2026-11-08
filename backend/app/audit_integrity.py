"""Periodic verification of the AuditLog hash chain (see
app/audit_hash_chain.py) -- the scheduled half of tamper-evidence. A chain
that's never checked doesn't actually catch anything on its own; this is
what turns "detectable in principle" into "actually detected." Runs on its
own configurable cadence (Settings.audit_integrity_check_interval_days),
deliberately independent of both the purge job's cadence and the retention
window itself (see ROADMAP.md's security-hardening notes) -- retention
decides what's kept, this decides how often what's kept gets re-verified,
and the two shouldn't be coupled just because they both touch AuditLog.

Known breaks from the #213 race (#217). Before 0.5.2, two concurrent audit
writes could chain from the same tip, so the chain forked: rows whose
prev_hash is an earlier row's hash rather than the row just before them.
That's a break to a linear walk, but not tampering, and the HMAC key tells
the two apart. A race break has this signature:

- its row_hash is valid for its own content and its stored prev_hash, so
  the app wrote it (nobody without the key can make one);
- that prev_hash is the hash of an existing earlier row (or the purge
  anchor);
- it's no newer than AuditChainState.known_break_cutoff_row_id, the last
  row written before upgrading to 0.5.3. The race was fixed in 0.5.2.

Tolerating such rows on sight isn't enough: a fork leaves rows that nothing
chains from (leaves), and deleting a leaf would go unnoticed. So a super
admin records them (record_known_breaks): a chained audit entry lists each
break row, with its hash and parent, and each leaf, with its hash. From
then on the walk accepts exactly those break rows, and every recorded row
must still exist with the same hash (unless retention purged it). Any
other break is reported as tampering, as before.
"""

import hmac
from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.audit_hash_chain import compute_row_hash
from app.auth.models import AuditChainState, AuditLog
from app.json_values import as_int
from app.logging_config import get_logger
from app.timeutils import utcnow

logger = get_logger(__name__)

KNOWN_BREAKS_ACTION = "audit.chain.known_breaks_recorded"


@dataclass
class ChainVerificationResult:
    ok: bool
    checked_count: int
    # The first tampered row if any, else the first unrecorded race break.
    broken_row_id: int | None
    # Breaks that match the #213 race signature but aren't recorded yet.
    race_break_ids: list[int] = field(default_factory=list)
    # Recorded race breaks the walk accepted.
    known_break_count: int = 0


class NoRaceBreaksError(Exception):
    """record_known_breaks found no unrecorded break matching the #213
    race signature."""


@dataclass
class _RaceBreak:
    row_id: int
    row_hash: str
    parent_id: int | None


@dataclass
class _Walk:
    # How many rows were walked, and for each problem row (tampered or race
    # break) how many rows have an id up to its own.
    rows_checked: int
    checked_up_to: dict[int, int]
    tampered_ids: list[int]
    race_breaks: list[_RaceBreak]
    known_break_count: int


# Rows read per query while walking the chain (#346).
WALK_BLOCK = 2000


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


def _recorded_known_breaks(session: Session) -> tuple[dict[int, dict], dict[int, str]]:
    """Every row recorded by record_known_breaks so far: break rows (id ->
    {"row_hash", "parent_id"}) and leaves (id -> row_hash). The entries
    that hold them are audit rows themselves, so the walk verifies them
    like any other; an entry edited without the key is caught there."""
    breaks: dict[int, dict] = {}
    leaves: dict[int, str] = {}
    entries = session.exec(
        select(AuditLog.event_metadata).where(AuditLog.action == KNOWN_BREAKS_ACTION)
    ).all()
    for metadata in entries:
        if not isinstance(metadata, dict):
            continue
        for item in metadata.get("breaks") or []:
            row_id = as_int(item.get("id")) if isinstance(item, dict) else None
            row_hash = item.get("row_hash") if row_id is not None else None
            if isinstance(row_hash, str):
                breaks[row_id] = {"row_hash": row_hash, "parent_id": as_int(item.get("parent_id"))}
        for item in metadata.get("leaves") or []:
            row_id = as_int(item.get("id")) if isinstance(item, dict) else None
            row_hash = item.get("row_hash") if row_id is not None else None
            if isinstance(row_hash, str):
                leaves[row_id] = row_hash
    return breaks, leaves


def _chain_rows(session: Session):
    """Every AuditLog row in id order, read WALK_BLOCK rows at a time
    (#346): the walk never holds the whole table in memory."""
    after = None
    while True:
        query = select(
            AuditLog.id,
            AuditLog.prev_hash,
            AuditLog.row_hash,
            AuditLog.user_id,
            AuditLog.action,
            AuditLog.target_type,
            AuditLog.target_id,
            AuditLog.timestamp,
            AuditLog.event_metadata,
        )
        if after is not None:
            query = query.where(AuditLog.id > after)
        block = session.exec(query.order_by(AuditLog.id.asc()).limit(WALK_BLOCK)).all()
        yield from block
        if len(block) < WALK_BLOCK:
            return
        after = block[-1].id


def _walk(key: bytes, session: Session, state: AuditChainState | None) -> _Walk:
    """Walks every remaining AuditLog row in id order, recomputing each
    row's hash (keyed the same way chain_new_entry/backfill_chain_if_needed
    compute it) from its own fields and stored prev_hash, and checking that
    prev_hash links to the row before it -- or to the purge anchor, for the
    first surviving row, see AuditChainState's docstring. Unlike a plain
    linear check it carries on past a break, from the row's own stored
    hash, so every break is found and classified (see the module
    docstring).

    Rows are streamed (#346), and only what a later row can need is kept:
    the hashes of rows up to the newest one a break can be (the race cutoff
    or a recorded break), which is where a break's parent can be, and the
    hashes of the recorded rows."""
    anchor_hash = state.anchor_hash if state else None
    anchor_row_id = state.anchor_row_id if state else None
    cutoff = state.known_break_cutoff_row_id if state else None
    recorded_breaks, recorded_leaves = _recorded_known_breaks(session)

    # Every recorded row must still be there, unchanged: that's what makes a
    # deleted fork leaf (or a deleted break row) show up.
    purged_up_to = anchor_row_id or 0
    expected_present = {row_id: entry["row_hash"] for row_id, entry in recorded_breaks.items()}
    expected_present.update(recorded_leaves)
    expected_present = {k: v for k, v in expected_present.items() if k > purged_up_to}
    # Recorded ids in order, and how many rows have an id up to each.
    expected_ids = sorted(expected_present)
    expected_up_to: dict[int, int] = {}
    next_expected = 0

    # No break can be newer than this, so no parent lookup needs a newer hash.
    parents_up_to = max([cutoff or 0, *recorded_breaks])

    expected_prev = anchor_hash
    # Hash -> id of every row seen so far up to parents_up_to (and the
    # anchor): where a break's prev_hash may legitimately point.
    earlier: dict[str, int | None] = {}
    if anchor_hash is not None:
        earlier[anchor_hash] = anchor_row_id
    present: dict[int, str | None] = {}
    count = 0
    checked_up_to: dict[int, int] = {}
    tampered: list[int] = []
    race_breaks: list[_RaceBreak] = []
    known = 0

    for row in _chain_rows(session):
        while next_expected < len(expected_ids) and expected_ids[next_expected] < row.id:
            expected_up_to[expected_ids[next_expected]] = count
            next_expected += 1
        count += 1
        recomputed = compute_row_hash(
            key,
            row.prev_hash,
            user_id=row.user_id,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            timestamp=row.timestamp,
            metadata=row.event_metadata,
        )
        valid = _hashes_match(row.row_hash, recomputed)
        linked = _hashes_match(row.prev_hash, expected_prev)
        recorded = recorded_breaks.get(row.id)
        parent_known = row.prev_hash is not None and row.prev_hash in earlier

        if valid and linked:
            pass
        elif (
            valid
            and recorded is not None
            and _hashes_match(row.row_hash, recorded["row_hash"])
            and (
                parent_known
                # Its parent was purged under retention since it was recorded.
                or (
                    recorded["parent_id"] is not None
                    and anchor_row_id is not None
                    and recorded["parent_id"] <= anchor_row_id
                )
            )
        ):
            known += 1
        elif valid and parent_known and cutoff is not None and row.id <= cutoff:
            race_breaks.append(_RaceBreak(row.id, row.row_hash, earlier[row.prev_hash]))
            checked_up_to[row.id] = count
        else:
            tampered.append(row.id)
            checked_up_to[row.id] = count

        if row.id in expected_present:
            present[row.id] = row.row_hash
        if row.row_hash is not None and row.id <= parents_up_to:
            earlier.setdefault(row.row_hash, row.id)
        expected_prev = row.row_hash

    for row_id in expected_ids[next_expected:]:
        expected_up_to[row_id] = count

    for row_id, row_hash in expected_present.items():
        if not _hashes_match(present.get(row_id), row_hash):
            if row_id not in tampered:
                tampered.append(row_id)
                checked_up_to[row_id] = expected_up_to[row_id]

    return _Walk(
        rows_checked=count,
        checked_up_to=checked_up_to,
        tampered_ids=sorted(tampered),
        race_breaks=race_breaks,
        known_break_count=known,
    )


def _get_or_create_state(session: Session) -> AuditChainState:
    state = session.exec(select(AuditChainState)).first()
    if state is None:
        state = AuditChainState()
        session.add(state)
    return state


def verify_chain(key: bytes, session: Session) -> ChainVerificationResult:
    """Verifies the whole chain (see _walk) and persists the result onto
    the singleton AuditChainState row (does not commit -- same "caller's
    transaction" convention as record_audit_event) for the Audit Log page's
    integrity banner to read.

    Status: "ok" when every break is a recorded known break; "broken" when
    any break isn't explained (tampering), with broken_row_id the first
    one; "race_breaks" when the only unexplained breaks match the #213 race
    signature and can be recorded."""
    state = session.exec(select(AuditChainState)).first()
    walk = _walk(key, session, state)
    race_ids = [race.row_id for race in walk.race_breaks]

    if walk.tampered_ids:
        status, broken_row_id = "broken", walk.tampered_ids[0]
    elif race_ids:
        status, broken_row_id = "race_breaks", race_ids[0]
    else:
        status, broken_row_id = "ok", None

    # Rows up to and including the first problem, as before #217: what
    # comes after a break isn't vouched for by the rows before it.
    if broken_row_id is None:
        checked = walk.rows_checked
    else:
        checked = walk.checked_up_to[broken_row_id]

    if state is None:
        state = _get_or_create_state(session)
    state.last_checked_at = utcnow()
    state.status = status
    state.broken_row_id = broken_row_id
    state.race_break_count = len(race_ids)
    state.known_break_count = walk.known_break_count
    return ChainVerificationResult(
        ok=status == "ok",
        checked_count=checked,
        broken_row_id=broken_row_id,
        race_break_ids=race_ids,
        known_break_count=walk.known_break_count,
    )


def _leaf_rows(session: Session, up_to: int, exclude: set[int]) -> list[tuple[int, str]]:
    """Rows up to `up_to` that no other row chains from, other than the
    newest row overall (the chain's tip, which has no successor yet)."""
    rows = session.exec(
        select(AuditLog.id, AuditLog.prev_hash, AuditLog.row_hash).order_by(AuditLog.id.asc())
    ).all()
    if not rows:
        return []
    parents = {prev_hash for _, prev_hash, _ in rows if prev_hash is not None}
    tip_id = rows[-1][0]
    return [
        (row_id, row_hash)
        for row_id, _, row_hash in rows
        if row_id <= up_to
        and row_id != tip_id
        and row_id not in exclude
        and row_hash is not None
        and row_hash not in parents
    ]


def record_known_breaks(
    key: bytes, session: Session, user_id: int | None
) -> ChainVerificationResult:
    """Records every unrecorded break that matches the #213 race signature,
    plus the fork's leaves, in a chained `audit.chain.known_breaks_recorded`
    entry, then re-verifies. Only rows that match the signature can be
    recorded; raises NoRaceBreaksError if there are none. Does not commit."""
    # Deferred: app.audit imports app.crypto, which tests reconfigure.
    from app.audit import record_audit_event

    state = _get_or_create_state(session)
    walk = _walk(key, session, state)
    if not walk.race_breaks:
        raise NoRaceBreaksError()

    break_ids = {race.row_id for race in walk.race_breaks}
    leaves = _leaf_rows(session, state.known_break_cutoff_row_id or 0, break_ids)
    record_audit_event(
        session,
        user_id=user_id,
        action=KNOWN_BREAKS_ACTION,
        target_type="audit_log",
        metadata={
            "breaks": [
                {"id": race.row_id, "row_hash": race.row_hash, "parent_id": race.parent_id}
                for race in walk.race_breaks
            ],
            "leaves": [{"id": row_id, "row_hash": row_hash} for row_id, row_hash in leaves],
        },
    )
    state.known_breaks_recorded_at = utcnow()
    logger.info(
        "audit_integrity.known_breaks_recorded",
        breaks=sorted(break_ids),
        leaves=[row_id for row_id, _ in leaves],
    )
    return verify_chain(key, session)


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
            logger.info(
                "audit_integrity.check_complete",
                checked=result.checked_count,
                known_breaks=result.known_break_count,
            )
        elif result.race_break_ids and result.broken_row_id in result.race_break_ids:
            logger.warning(
                "audit_integrity.race_breaks_unrecorded",
                checked=result.checked_count,
                race_break_ids=result.race_break_ids,
            )
        else:
            logger.error(
                "audit_integrity.chain_broken",
                checked=result.checked_count,
                broken_row_id=result.broken_row_id,
            )
