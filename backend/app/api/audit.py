import threading
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, or_
from sqlmodel import Session, func, select

from app.api.auth import get_current_active_user, require_super_admin
from app.audit_integrity import NoRaceBreaksError, record_known_breaks, verify_chain
from app.audit_labels import NAMED_TARGET_TYPES, describe_target, prefetch_targets
from app.auth.models import AuditChainState, AuditLog, GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.crypto import audit_chain_key
from app.db import get_session
from app.timeutils import utcnow

router = APIRouter(prefix="/audit", tags=["audit"])

require_view = require_global_capability(GlobalCapability.view_audit_log, get_current_active_user)

# A safety valve for load (an unbounded page could pull the whole table into
# memory), not a design goal -- same spirit as the scratch store's size guard
# and the search indexer's max-file-size cap.
MAX_LIMIT = 200


class AuditLogEntryPublic(BaseModel):
    id: int
    user_id: int | None
    username: str | None
    action: str
    target_type: str | None
    target_id: int | None
    timestamp: datetime
    metadata: dict | None
    # What the entry is about, by name (#239): as recorded when it was
    # written, or, for entries older than 0.5.3, the object's current name
    # (target_name_is_current), since those can't be backfilled.
    target_name: str | None = None
    target_context: str | None = None
    target_name_is_current: bool = False
    # Whether the object still exists, so the page can link to it.
    target_exists: bool = False


class AuditLogPage(BaseModel):
    items: list[AuditLogEntryPublic]
    total: int


class AuditTargetOption(BaseModel):
    id: int
    name: str


class AuditLogFilterOptions(BaseModel):
    """Distinct actions/target_types actually present in AuditLog right now,
    not a hardcoded list -- new action namespaces slot in automatically as
    they're written, the same "always accurate, not a guess that goes
    stale" reasoning already used for Search's source-name matching (see
    ROADMAP.md's full-text search notes)."""

    actions: list[str]
    target_types: list[str]
    # Per named target type, the targets that appear in the log, newest
    # first (#358): their current name, or the name recorded when they were
    # last touched if they're gone since.
    targets: dict[str, list[AuditTargetOption]] = {}


# Targets listed per type in the filter; older ones can still be reached
# by filtering on the whole type.
MAX_TARGET_OPTIONS = 500


def _action_condition(action: str):
    """A trailing ".*" (e.g. "source.*") matches every action under that
    namespace via a prefix match; anything else matches exactly -- lets the
    frontend offer both a coarse "Sources" filter and a specific
    "source.delete" one from the same distinct-actions list."""
    if action.endswith(".*"):
        # autoescape=True: the prefix can itself contain a literal "_" (e.g.
        # "role_grant.*"), which SQL LIKE otherwise treats as a single-char
        # wildcard, not a literal underscore -- autoescape backslash-escapes
        # it (and any literal "%") before building the LIKE pattern.
        return AuditLog.action.startswith(action[:-1], autoescape=True)
    return AuditLog.action == action


@router.get("", response_model=AuditLogPage)
def list_audit_log(
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    action: list[str] | None = Query(None),
    target_type: list[str] | None = Query(None),
    target: list[str] | None = Query(None),
    user_id: int | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    sort: Literal["time", "user", "action", "target"] = Query("time"),
    order: Literal["asc", "desc"] = Query("desc"),
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    """A page of the audit log, filtered and sorted. Sorting is done here,
    not in the browser, so it covers every matching entry and not only the
    page on screen (#358). Ties keep the newest first."""
    conditions = []
    if action:
        conditions.append(or_(*(_action_condition(a) for a in action)))
    # Whole types and single targets ("source:5") both narrow the target;
    # an entry matching any of them is shown.
    target_conditions = []
    if target_type:
        target_conditions.append(AuditLog.target_type.in_(target_type))
    for item in target or []:
        kind, _, raw_id = item.partition(":")
        if not raw_id.isdigit():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"target must be type:id, got {item!r}",
            )
        target_conditions.append(
            and_(AuditLog.target_type == kind, AuditLog.target_id == int(raw_id))
        )
    if target_conditions:
        conditions.append(or_(*target_conditions))
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if since is not None:
        conditions.append(AuditLog.timestamp >= since)
    if until is not None:
        conditions.append(AuditLog.timestamp <= until)

    count_query = select(func.count()).select_from(AuditLog)
    direction = (lambda column: column.asc()) if order == "asc" else (lambda column: column.desc())
    rows_query = select(AuditLog)
    if sort == "user":
        # By username; entries without a user (failed sign-ins of unknown
        # names, system events) sort as an empty name.
        rows_query = rows_query.outerjoin(User, User.id == AuditLog.user_id)
        keys = [direction(func.coalesce(User.username, ""))]
    elif sort == "action":
        keys = [direction(AuditLog.action)]
    elif sort == "target":
        keys = [direction(AuditLog.target_type), direction(AuditLog.target_id)]
    else:
        keys = [direction(AuditLog.timestamp)]
    rows_query = rows_query.order_by(*keys, AuditLog.timestamp.desc(), AuditLog.id.desc())
    for condition in conditions:
        count_query = count_query.where(condition)
        rows_query = rows_query.where(condition)

    total = session.exec(count_query).one()
    rows = session.exec(rows_query.offset(offset).limit(limit)).all()

    user_ids = {row.user_id for row in rows if row.user_id is not None}
    usernames: dict[int, str] = {}
    if user_ids:
        usernames = {
            u.id: u.username for u in session.exec(select(User).where(User.id.in_(user_ids))).all()
        }

    # Rows for every target on the page, in a few queries (see prefetch_targets).
    _loaded = prefetch_targets(session, [(row.target_type, row.target_id) for row in rows])
    current: dict[tuple, dict] = {}
    items = []
    for row in rows:
        key = (row.target_type, row.target_id)
        if row.target_id is not None and key not in current:
            current[key] = describe_target(session, row.target_type, row.target_id)
        now = current.get(key, {})
        recorded = row.event_metadata if isinstance(row.event_metadata, dict) else {}
        use_current = "target_name" not in recorded and "target_name" in now
        names = now if use_current else recorded
        items.append(
            AuditLogEntryPublic(
                id=row.id,
                user_id=row.user_id,
                username=usernames.get(row.user_id) if row.user_id is not None else None,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                timestamp=row.timestamp,
                metadata=row.event_metadata,
                target_name=_as_text(names.get("target_name")),
                target_context=_as_text(names.get("target_context")),
                target_name_is_current=use_current,
                target_exists="target_name" in now,
            )
        )
    return AuditLogPage(items=items, total=total)


def _as_text(value) -> str | None:
    return value if isinstance(value, str) else None


@router.get("/filters", response_model=AuditLogFilterOptions)
def get_audit_log_filters(
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    actions = session.exec(select(AuditLog.action).distinct().order_by(AuditLog.action)).all()
    target_types = session.exec(
        select(AuditLog.target_type)
        .where(AuditLog.target_type.is_not(None))
        .distinct()
        .order_by(AuditLog.target_type)
    ).all()
    return AuditLogFilterOptions(
        actions=actions,
        target_types=target_types,
        targets={
            kind: _target_options(session, kind)
            for kind in target_types
            if kind in NAMED_TARGET_TYPES
        },
    )


def _target_options(session: Session, kind: str) -> list[AuditTargetOption]:
    """The targets of one type in the log, newest first, named as they are
    now or, when gone, as they were last recorded."""
    latest = (
        select(AuditLog.target_id, func.max(AuditLog.id).label("last_id"))
        .where(AuditLog.target_type == kind, AuditLog.target_id.is_not(None))
        .group_by(AuditLog.target_id)
        .order_by(func.max(AuditLog.id).desc())
        .limit(MAX_TARGET_OPTIONS)
    )
    rows = session.exec(latest).all()
    if not rows:
        return []
    last_rows = {
        row.id: row
        for row in session.exec(
            select(AuditLog).where(AuditLog.id.in_([last_id for _target, last_id in rows]))
        ).all()
    }
    kept = prefetch_targets(session, [(kind, target_id) for target_id, _ in rows])  # noqa: F841
    options = []
    for target_id, last_id in rows:
        name = describe_target(session, kind, target_id).get("target_name")
        if name is None:
            recorded = (last_rows[last_id].event_metadata or {}).get("target_name")
            name = recorded if isinstance(recorded, str) else None
        options.append(AuditTargetOption(id=target_id, name=name or f"#{target_id}"))
    return options


class AuditIntegrityStatus(BaseModel):
    # "unknown" until the first check ever runs -- see AuditChainState's
    # docstring in app/auth/models.py.
    # "race_breaks": the only breaks match the #213 race and can be
    # recorded as known (#217, see app/audit_integrity.py).
    status: str
    last_checked_at: datetime | None
    broken_row_id: int | None
    race_break_count: int = 0
    known_break_count: int = 0
    known_breaks_recorded_at: datetime | None = None


def _read_integrity_status(session: Session) -> AuditIntegrityStatus:
    state = session.exec(select(AuditChainState)).first()
    if state is None:
        return AuditIntegrityStatus(status="unknown", last_checked_at=None, broken_row_id=None)
    return AuditIntegrityStatus(
        status=state.status,
        last_checked_at=state.last_checked_at,
        broken_row_id=state.broken_row_id,
        race_break_count=state.race_break_count,
        known_break_count=state.known_break_count,
        known_breaks_recorded_at=state.known_breaks_recorded_at,
    )


@router.get("/integrity", response_model=AuditIntegrityStatus)
def get_audit_integrity_status(
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    """Read-only: the result of the most recent verify_chain() pass,
    whether that ran on its own schedule (app/audit_integrity.py,
    Settings.audit_integrity_check_interval_days) or via the manual
    POST below. Backs the Audit Log page's integrity banner."""
    return _read_integrity_status(session)


# A safety valve for load, not a design goal -- same spirit as MAX_LIMIT
# above: a full verify_chain() pass is an O(n) HMAC recompute over every
# surviving AuditLog row, and this endpoint's gate (view_audit_log) is a
# read-level capability, not a manage-level one, so a low-trust viewer
# role could otherwise script rapid repeated calls into real CPU cost on
# an install with a large, long-retained log. A trivial re-check within
# this window just returns the already-current status instead of
# recomputing it.
_MANUAL_VERIFY_COOLDOWN_SECONDS = 30

# Guards the check-then-act cooldown below across the whole process, not
# just within one request -- without it, two concurrent POSTs can both
# read the same stale last_checked_at, both pass the cooldown check, and
# both run a full verify_chain() pass at once, defeating the point of the
# cooldown for exactly the burst-of-requests case it exists for.
_manual_verify_lock = threading.Lock()


@router.post("/integrity/verify", response_model=AuditIntegrityStatus)
def run_audit_integrity_verification(
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    """Runs the same check the scheduled job does, on demand -- with a
    default check interval as long as a year (deliberately decoupled from
    retention, see Settings.audit_integrity_check_interval_days), an admin
    who wants to confirm the chain is intact right now shouldn't have to
    wait for the next scheduled pass. Gated by view_audit_log, not a
    manage-level capability: this only recomputes and stores a status,
    never alters AuditLog content itself."""
    with _manual_verify_lock:
        state = session.exec(select(AuditChainState)).first()
        if state is not None and state.last_checked_at is not None:
            since_last_check = utcnow() - state.last_checked_at
            if since_last_check < timedelta(seconds=_MANUAL_VERIFY_COOLDOWN_SECONDS):
                return _read_integrity_status(session)

        verify_chain(audit_chain_key(), session)
        session.commit()
        return _read_integrity_status(session)


@router.post("/integrity/known-breaks", response_model=AuditIntegrityStatus)
def record_audit_known_breaks(
    user: User = Depends(require_super_admin),
    session: Session = Depends(get_session),
):
    """Records the breaks that match the #213 race as known (#217), so
    verification accepts exactly those rows from then on. Super admins
    only: unlike "Verify now", this changes what verification accepts.
    Only breaks that match the race signature can be recorded; anything
    else stays reported as tampering."""
    with _manual_verify_lock:
        try:
            record_known_breaks(audit_chain_key(), session, user.id)
        except NoRaceBreaksError:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No breaks match the #213 race, so there is nothing to record.",
            ) from None
        session.commit()
        return _read_integrity_status(session)
