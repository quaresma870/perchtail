from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, func, select

from app.api.auth import get_current_active_user
from app.auth.models import AuditLog, GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session

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


class AuditLogPage(BaseModel):
    items: list[AuditLogEntryPublic]
    total: int


class AuditLogFilterOptions(BaseModel):
    """Distinct actions/target_types actually present in AuditLog right now,
    not a hardcoded list -- new action namespaces slot in automatically as
    they're written, the same "always accurate, not a guess that goes
    stale" reasoning already used for Search's source-name matching (see
    ROADMAP.md's full-text search notes)."""

    actions: list[str]
    target_types: list[str]


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
    user_id: int | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    user: User = Depends(require_view),
    session: Session = Depends(get_session),
):
    conditions = []
    if action:
        conditions.append(or_(*(_action_condition(a) for a in action)))
    if target_type:
        conditions.append(AuditLog.target_type.in_(target_type))
    if user_id is not None:
        conditions.append(AuditLog.user_id == user_id)
    if since is not None:
        conditions.append(AuditLog.timestamp >= since)
    if until is not None:
        conditions.append(AuditLog.timestamp <= until)

    count_query = select(func.count()).select_from(AuditLog)
    rows_query = select(AuditLog).order_by(AuditLog.timestamp.desc(), AuditLog.id.desc())
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

    items = [
        AuditLogEntryPublic(
            id=row.id,
            user_id=row.user_id,
            username=usernames.get(row.user_id) if row.user_id is not None else None,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            timestamp=row.timestamp,
            metadata=row.event_metadata,
        )
        for row in rows
    ]
    return AuditLogPage(items=items, total=total)


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
    return AuditLogFilterOptions(actions=actions, target_types=target_types)
