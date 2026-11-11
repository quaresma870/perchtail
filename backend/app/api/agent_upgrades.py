"""Settings -> Agents (#319): the remote-upgrade policy and window, every
push agent with its version, policy and last attempt, and the actions --
upgrade one agent, every outdated one, resume a suspended one. Only for users
who can manage sources (the same create_source capability that edits them
and issues their agent token).

The server sends an agent an `upgrade` command carrying nothing but its own
version; see app/agent_upgrades.py for what the agent then checks, how the
attempt is followed, and the automatic upgrades, and
app/agent_upgrade_policy.py for the policies and the window."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlmodel import Session, col, select

from app import agent_upgrade_policy as policies
from app import system_settings
from app.agent_registry import get_agent_registry
from app.agent_upgrades import (
    ACTIVE,
    bundled_binaries,
    latest_run,
    latest_runs,
    not_upgradable,
    start_upgrades,
    window_summary,
)
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session
from app.logging_config import get_logger
from app.models import AgentUpgradeRun, Protocol, Source
from app.timeutils import utcnow
from app.version import APP_VERSION
from app.versions import is_newer

router = APIRouter(tags=["agent-upgrades"])
logger = get_logger(__name__)

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)
# The system-wide policy and window change every agent's behaviour, like the
# other System settings.
require_system_settings = require_global_capability(
    GlobalCapability.manage_system_settings, get_current_active_user
)

_MAX_BULK = 500
_MAX_PAGE = 100

POLICY_OFF = "Remote upgrades are off for this agent (Settings → Agents)"
SUSPENDED = (
    "Automatic upgrades of this agent are suspended after failing: resume it, "
    "or upgrade it on its own"
)

RunStatus = Literal[
    "requested",
    "accepted",
    "downloading",
    "installing",
    "restarting",
    "succeeded",
    "failed",
    "refused",
    "too_old",
    "timeout",
]


class AgentUpgradeRunPublic(BaseModel):
    id: int
    status: RunStatus
    message: str | None
    from_version: str | None
    to_version: str
    trigger: Literal["manual", "automatic"]
    # The user who asked, or None when the schedule did (or the user is gone).
    requested_by: str | None
    started_at: datetime
    finished_at: datetime | None
    # This failure reached the failure limit and suspended automatic upgrades.
    suspended_source: bool


def _run_public(run: AgentUpgradeRun, usernames: dict[int, str]) -> AgentUpgradeRunPublic:
    return AgentUpgradeRunPublic(
        id=run.id,
        status=run.status,
        message=run.message,
        from_version=run.from_version,
        to_version=run.to_version,
        trigger=run.trigger,
        requested_by=(
            usernames.get(run.requested_by_user_id)
            if run.requested_by_user_id is not None
            else None
        ),
        started_at=run.started_at,
        finished_at=run.finished_at,
        suspended_source=run.suspended_source,
    )


def _usernames(session: Session, runs: list[AgentUpgradeRun]) -> dict[int, str]:
    ids = {r.requested_by_user_id for r in runs if r.requested_by_user_id is not None}
    if not ids:
        return {}
    users = session.exec(select(User).where(col(User.id).in_(ids))).all()
    return {u.id: u.username for u in users}


# --- settings ------------------------------------------------------------------


class WindowSummary(BaseModel):
    start: datetime
    end: datetime
    upgraded: int
    failed: int
    suspended: int
    running: int


class AgentUpgradeOverview(BaseModel):
    policy: policies.SystemPolicy
    # Days of the week the window starts on, 0 = Monday.
    window_days: list[int]
    window_start: str  # HH:MM, server time
    window_hours: int
    failure_limit: int
    concurrency: int
    # The server's time zone, which the window is in.
    timezone: str
    window_open: bool
    next_window_start: datetime | None
    last_window: WindowSummary | None
    # The version agents are upgraded to: this server's.
    version: str
    # Whether this server carries the signed agent binaries of its version;
    # without them agents download the release from GitHub.
    bundled: bool
    bundled_reason: str | None


def _overview(session: Session) -> AgentUpgradeOverview:
    settings = policies.read_settings(session)
    zone, zone_name = policies.server_timezone()
    now = utcnow()
    bundled, reason = bundled_binaries()
    summary = window_summary(session)
    return AgentUpgradeOverview(
        policy=settings.policy,
        window_days=sorted(settings.window.days),
        window_start=settings.window.start_text,
        window_hours=settings.window.hours,
        failure_limit=settings.failure_limit,
        concurrency=settings.concurrency,
        timezone=zone_name,
        window_open=policies.window_bounds(settings.window, now, zone) is not None,
        next_window_start=policies.next_window_start(settings.window, now, zone),
        last_window=WindowSummary(**summary) if summary else None,
        version=APP_VERSION,
        bundled=bundled,
        bundled_reason=reason,
    )


@router.get("/agent-upgrades", response_model=AgentUpgradeOverview)
def agent_upgrade_overview(
    user: User = Depends(require_manage), session: Session = Depends(get_session)
):
    return _overview(session)


class AgentUpgradeSettingsUpdate(BaseModel):
    policy: policies.SystemPolicy | None = None
    window_days: list[int] | None = Field(default=None, max_length=7)
    window_start: str | None = Field(default=None, max_length=5)
    window_hours: int | None = Field(
        default=None, ge=policies.WINDOW_HOURS_MIN, le=policies.WINDOW_HOURS_MAX
    )
    failure_limit: int | None = Field(
        default=None, ge=policies.FAILURE_LIMIT_MIN, le=policies.FAILURE_LIMIT_MAX
    )
    concurrency: int | None = Field(
        default=None, ge=policies.CONCURRENCY_MIN, le=policies.CONCURRENCY_MAX
    )

    @field_validator("window_days")
    @classmethod
    def _days(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return None
        if any(day < 0 or day > 6 for day in value):
            raise ValueError("Days are 0 (Monday) to 6 (Sunday)")
        if not value:
            raise ValueError("Pick at least one day")
        return sorted(set(value))

    @field_validator("window_start")
    @classmethod
    def _start(cls, value: str | None) -> str | None:
        if value is None:
            return None
        policies.parse_start(value)
        return value


@router.patch("/agent-upgrades/settings", response_model=AgentUpgradeOverview)
def update_agent_upgrade_settings(
    payload: AgentUpgradeSettingsUpdate,
    user: User = Depends(require_system_settings),
    session: Session = Depends(get_session),
):
    changed = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None}
    if "policy" in changed:
        policies.set_system_policy(session, changed["policy"])
    if "window_days" in changed:
        system_settings.set_int(
            session, "agent_upgrade_window_days", policies.mask_from_days(changed["window_days"])
        )
    if "window_start" in changed:
        system_settings.set_int(
            session,
            "agent_upgrade_window_start_minutes",
            policies.parse_start(changed["window_start"]),
        )
    for field, key in (
        ("window_hours", "agent_upgrade_window_hours"),
        ("failure_limit", "agent_upgrade_failure_limit"),
        ("concurrency", "agent_upgrade_concurrency"),
    ):
        if field in changed:
            system_settings.set_int(session, key, changed[field])
    if changed:
        record_audit_event(
            session,
            user_id=user.id,
            action="system_settings.update",
            target_type="system_settings",
            metadata={f"agent_upgrade_{k}": v for k, v in changed.items()},
        )
    session.commit()
    return _overview(session)


# --- the agents ----------------------------------------------------------------


class AgentRow(BaseModel):
    source_id: int
    name: str
    customer: str | None
    enabled: bool
    connected: bool
    agent_version: str | None
    outdated: bool
    policy: policies.SourcePolicy
    effective_policy: policies.SystemPolicy
    failures: int
    # Automatic upgrades skip it until it's resumed.
    suspended: bool
    # Why Upgrade can't start now, or None.
    blocker: str | None
    last_run: AgentUpgradeRunPublic | None
    upgrading: bool


def _row(
    source: Source,
    settings: policies.UpgradeSettings,
    run: AgentUpgradeRun | None,
    usernames: dict[int, str],
) -> AgentRow:
    effective = policies.effective_policy(source.agent_upgrade_policy, settings.policy)
    upgrading = run is not None and run.status in ACTIVE
    blocker = POLICY_OFF if effective == "off" else not_upgradable(source)
    if blocker is None and upgrading:
        blocker = f"An upgrade to {run.to_version} is already in progress"
    failures = source.agent_upgrade_failures or 0
    return AgentRow(
        source_id=source.id,
        name=source.name,
        customer=source.customer.name if source.customer else None,
        enabled=source.enabled,
        connected=get_agent_registry().is_connected(source.id),
        agent_version=source.agent_version,
        outdated=is_newer(APP_VERSION, source.agent_version),
        policy=(
            source.agent_upgrade_policy
            if source.agent_upgrade_policy in policies.SOURCE_POLICIES
            else "inherit"
        ),
        effective_policy=effective,
        failures=failures,
        suspended=policies.is_suspended(failures, settings.failure_limit),
        blocker=blocker,
        last_run=_run_public(run, usernames) if run else None,
        upgrading=upgrading,
    )


def _agent_sources(session: Session) -> list[Source]:
    return list(
        session.exec(
            select(Source)
            .where(Source.protocol == Protocol.agent, Source.is_system == False)  # noqa: E712
            .order_by(Source.name, Source.id)
        ).all()
    )


def _agent_source(session: Session, source_id: int) -> Source:
    source = session.get(Source, source_id)
    if source is None or source.is_system:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if source.protocol != Protocol.agent:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only push-agent sources have an agent to upgrade",
        )
    return source


def _row_for(session: Session, source: Source) -> AgentRow:
    run = latest_run(session, source.id)
    return _row(
        source, policies.read_settings(session), run, _usernames(session, [run] if run else [])
    )


@router.get("/agent-upgrades/agents", response_model=list[AgentRow])
def list_agents(user: User = Depends(require_manage), session: Session = Depends(get_session)):
    sources = _agent_sources(session)
    settings = policies.read_settings(session)
    runs = latest_runs(session, [s.id for s in sources])
    usernames = _usernames(session, list(runs.values()))
    return [_row(s, settings, runs.get(s.id), usernames) for s in sources]


class SourcePolicyUpdate(BaseModel):
    policy: policies.SourcePolicy


@router.put("/sources/{source_id}/agent-upgrade-policy", response_model=AgentRow)
def set_source_policy(
    source_id: int,
    payload: SourcePolicyUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    source = _agent_source(session, source_id)
    previous = source.agent_upgrade_policy
    if previous != payload.policy:
        source.agent_upgrade_policy = payload.policy
        session.add(source)
        record_audit_event(
            session,
            user_id=user.id,
            action="source.update",
            target_type="source",
            target_id=source.id,
            metadata={
                "changes": {"agent_upgrade_policy": {"from": previous, "to": payload.policy}}
            },
        )
        session.commit()
        session.refresh(source)
    return _row_for(session, source)


@router.post("/sources/{source_id}/agent-upgrade/resume", response_model=AgentRow)
def resume_agent_upgrades(
    source_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Clears the source's consecutive failure count, so automatic upgrades
    include it again."""
    source = _agent_source(session, source_id)
    failures = source.agent_upgrade_failures or 0
    source.agent_upgrade_failures = 0
    session.add(source)
    record_audit_event(
        session,
        user_id=user.id,
        action="agent.upgrade_resume",
        target_type="source",
        target_id=source.id,
        metadata={"failures": failures},
    )
    session.commit()
    session.refresh(source)
    return _row_for(session, source)


class AgentUpgradeHistory(BaseModel):
    total: int
    items: list[AgentUpgradeRunPublic]


@router.get("/sources/{source_id}/agent-upgrades", response_model=AgentUpgradeHistory)
def agent_upgrade_history(
    source_id: int,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=_MAX_PAGE),
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """The source's upgrade attempts, most recent first."""
    _agent_source(session, source_id)
    total = session.exec(
        select(func.count())
        .select_from(AgentUpgradeRun)
        .where(AgentUpgradeRun.source_id == source_id)
    ).one()
    runs = list(
        session.exec(
            select(AgentUpgradeRun)
            .where(AgentUpgradeRun.source_id == source_id)
            .order_by(col(AgentUpgradeRun.id).desc())
            .offset(offset)
            .limit(limit)
        ).all()
    )
    usernames = _usernames(session, runs)
    return AgentUpgradeHistory(total=total, items=[_run_public(r, usernames) for r in runs])


# --- upgrading -----------------------------------------------------------------

Result = Literal["accepted", "refused", "too_old", "failed", "busy"]


class AgentUpgradeStarted(BaseModel):
    result: Result
    detail: str | None
    run: AgentUpgradeRunPublic | None


@router.post("/sources/{source_id}/agent-upgrade", response_model=AgentUpgradeStarted)
def upgrade_agent(
    source_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Upgrades the source's agent to this server's version, unless its
    policy is off. Also on a suspended source: an admin's explicit choice.
    Answers once the agent accepted (the Agents page then follows it) or
    refused."""
    source = _agent_source(session, source_id)
    settings = policies.read_settings(session)
    if policies.effective_policy(source.agent_upgrade_policy, settings.policy) == "off":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=POLICY_OFF)
    reason = not_upgradable(source)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    [started] = start_upgrades(
        session, [(source.id, source.agent_version)], trigger="manual", user_id=user.id
    )
    run = session.get(AgentUpgradeRun, started.run_id) if started.run_id else None
    return AgentUpgradeStarted(
        result=started.result,
        detail=started.detail,
        run=_run_public(run, _usernames(session, [run])) if run else None,
    )


class BulkAgentUpgradeRequest(BaseModel):
    # None: every agent source.
    source_ids: list[int] | None = Field(default=None, min_length=1, max_length=_MAX_BULK)


class BulkAgentUpgradeResult(BaseModel):
    source_id: int
    name: str | None
    result: Result | Literal["skipped"]
    detail: str | None = None


@router.post("/agent-upgrades", response_model=list[BulkAgentUpgradeResult])
def upgrade_agents(
    payload: BulkAgentUpgradeRequest | None = None,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Upgrade all outdated agents: those of `source_ids`, or every enabled
    agent source with an outdated agent. Each source's effective policy applies: off and suspended
    sources are skipped, with the reason, as are those that can't be
    upgraded now (not connected, up to date...); the others are asked at the
    same time."""
    settings = policies.read_settings(session)
    if payload is not None and payload.source_ids is not None:
        wanted = list(dict.fromkeys(payload.source_ids))
        found = {s.id: s for s in session.exec(select(Source).where(col(Source.id).in_(wanted)))}
    else:
        found = {
            s.id: s
            for s in _agent_sources(session)
            if s.enabled and is_newer(APP_VERSION, s.agent_version)
        }
        wanted = list(found)
    results: dict[int, BulkAgentUpgradeResult] = {}
    targets: list[tuple[int, str | None]] = []
    for source_id in wanted:
        source = found.get(source_id)
        if source is None or source.is_system:
            results[source_id] = BulkAgentUpgradeResult(
                source_id=source_id, name=None, result="skipped", detail="Not found"
            )
            continue
        reason = not_upgradable(source)
        if reason is None:
            effective = policies.effective_policy(source.agent_upgrade_policy, settings.policy)
            if effective == "off":
                reason = POLICY_OFF
            elif policies.is_suspended(source.agent_upgrade_failures or 0, settings.failure_limit):
                reason = SUSPENDED
        if reason is not None:
            results[source_id] = BulkAgentUpgradeResult(
                source_id=source_id, name=source.name, result="skipped", detail=reason
            )
            continue
        targets.append((source.id, source.agent_version))
    names = {source_id: found[source_id].name for source_id, _v in targets}
    for started in start_upgrades(session, targets, trigger="manual", user_id=user.id):
        results[started.source_id] = BulkAgentUpgradeResult(
            source_id=started.source_id,
            name=names[started.source_id],
            result=started.result,
            detail=started.detail,
        )
    return [results[source_id] for source_id in wanted]
