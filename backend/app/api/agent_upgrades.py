"""Upgrading push agents from the portal (#319): one source's agent, or
every outdated one picked in the Sources list. Only for users who can manage
sources (the same create_source capability that edits them and issues their
agent token), and only while Settings -> System allows it.

The server sends the agent an `upgrade` command carrying nothing but its own
version; see app/agent_upgrades.py for what the agent then checks, and how
the outcome is followed."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app import system_settings
from app.agent_registry import (
    AgentCommandError,
    AgentError,
    AgentTimeoutError,
    get_agent_registry,
)
from app.agent_upgrades import (
    TOO_OLD,
    UpgradeInProgressError,
    bundled_binaries,
    get_agent_upgrades,
)
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import GlobalCapability, User
from app.auth.rbac import require_global_capability
from app.db import get_session
from app.logging_config import get_logger
from app.models import Protocol, Source
from app.version import APP_VERSION
from app.versions import is_newer, parse_version

router = APIRouter(tags=["agent-upgrades"])
logger = get_logger(__name__)

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)

# How long the agent has to accept or refuse; it decides before downloading.
ACCEPT_TIMEOUT_SECONDS = 15.0
_MAX_BULK = 200
_BULK_PARALLEL = 8

SETTING_OFF = "Upgrading agents from the portal is off (Settings → System)"


class AgentUpgradePublic(BaseModel):
    """A source's latest upgrade from the portal, while this server runs."""

    status: Literal["requested", "running", "restarting", "succeeded", "failed"]
    stage: str | None
    detail: str | None
    from_version: str | None
    to_version: str
    started_at: datetime
    finished_at: datetime | None


def upgrade_public(source_id: int) -> AgentUpgradePublic | None:
    state = get_agent_upgrades().get(source_id)
    if state is None:
        return None
    return AgentUpgradePublic(
        status=state.status,
        stage=state.stage,
        detail=state.detail,
        from_version=state.from_version,
        to_version=state.to_version,
        started_at=state.started_at,
        finished_at=state.finished_at,
    )


class AgentUpgradeAvailability(BaseModel):
    enabled: bool
    # The version agents are upgraded to: this server's.
    version: str
    # Whether this server carries the signed agent binaries of its version;
    # without them agents download the release from GitHub.
    bundled: bool
    bundled_reason: str | None


@router.get("/agent-upgrades", response_model=AgentUpgradeAvailability)
def agent_upgrade_availability(
    user: User = Depends(require_manage), session: Session = Depends(get_session)
):
    bundled, reason = bundled_binaries()
    return AgentUpgradeAvailability(
        enabled=system_settings.get_bool(session, "agent_remote_upgrade_enabled"),
        version=APP_VERSION,
        bundled=bundled,
        bundled_reason=reason,
    )


def _not_upgradable(source: Source) -> str | None:
    """Why the source's agent can't be upgraded from here, or None."""
    if source.protocol != Protocol.agent:
        return "Only push-agent sources have an agent to upgrade"
    if not source.enabled:
        return "The source is disabled"
    if not get_agent_registry().is_connected(source.id):
        return "The agent isn't connected"
    if source.agent_version is None:
        # Agents report their version from 0.6 on.
        return TOO_OLD
    if parse_version(source.agent_version) is None:
        return (
            f"The agent reports version {source.agent_version!r}, not a release: "
            "upgrade it by hand"
        )
    if not is_newer(APP_VERSION, source.agent_version):
        return (
            f"The agent runs {source.agent_version}, which isn't older than this "
            f"server ({APP_VERSION})"
        )
    return None


Result = Literal["accepted", "refused", "too_old", "failed", "busy"]


def _send_upgrade(
    source_id: int, from_version: str | None, user_id: int | None
) -> tuple[Result, str | None]:
    """Asks the agent to upgrade and waits for it to accept or refuse.
    Thread-safe (no database): the bulk action runs several at once."""
    upgrades = get_agent_upgrades()
    command_id = f"upgrade-{uuid.uuid4()}"
    try:
        upgrades.begin(
            source_id,
            user_id=user_id,
            command_id=command_id,
            from_version=from_version,
            to_version=APP_VERSION,
        )
    except UpgradeInProgressError as exc:
        return "busy", str(exc)
    try:
        get_agent_registry().send_command_sync(
            source_id,
            "upgrade",
            timeout=ACCEPT_TIMEOUT_SECONDS,
            command_id=command_id,
            version=APP_VERSION,
        )
    except AgentCommandError as exc:
        if exc.message_type == "unknown_error":
            result, reason = "too_old", TOO_OLD
        else:
            result, reason = "refused", f"The agent refused: {exc}"
        upgrades.refused(source_id, command_id, reason)
        return result, reason
    except AgentTimeoutError:
        reason = "The agent didn't answer the upgrade request"
        upgrades.refused(source_id, command_id, reason)
        return "failed", reason
    except AgentError as exc:
        reason = f"The upgrade request didn't reach the agent: {exc}"
        upgrades.refused(source_id, command_id, reason)
        return "failed", reason
    upgrades.accepted(source_id, command_id)
    return "accepted", None


def _audit_request(
    session: Session, user: User, source: Source, result: str, reason: str | None
) -> None:
    metadata = {"from": source.agent_version, "to": APP_VERSION, "result": result}
    if reason:
        metadata["reason"] = reason
    record_audit_event(
        session,
        user_id=user.id,
        action="agent.upgrade",
        target_type="source",
        target_id=source.id,
        metadata=metadata,
    )


def _require_enabled(session: Session) -> None:
    if not system_settings.get_bool(session, "agent_remote_upgrade_enabled"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SETTING_OFF)


class AgentUpgradeStarted(BaseModel):
    result: Result
    detail: str | None
    upgrade: AgentUpgradePublic | None


@router.post("/sources/{source_id}/agent-upgrade", response_model=AgentUpgradeStarted)
def upgrade_agent(
    source_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Upgrades the source's agent to this server's version. Answers once
    the agent accepted (the source then shows the progress) or refused."""
    _require_enabled(session)
    source = session.get(Source, source_id)
    if source is None or source.is_system:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    reason = _not_upgradable(source)
    if reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    result, detail = _send_upgrade(source.id, source.agent_version, user.id)
    if result != "busy":
        _audit_request(session, user, source, result, detail)
        session.commit()
    return AgentUpgradeStarted(result=result, detail=detail, upgrade=upgrade_public(source_id))


class BulkAgentUpgradeRequest(BaseModel):
    source_ids: list[int] = Field(min_length=1, max_length=_MAX_BULK)


class BulkAgentUpgradeResult(BaseModel):
    source_id: int
    name: str | None
    result: Result | Literal["skipped"]
    detail: str | None = None


@router.post("/agent-upgrades", response_model=list[BulkAgentUpgradeResult])
def upgrade_agents(
    payload: BulkAgentUpgradeRequest,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Upgrades the agents of several sources (the outdated ones picked in
    the Sources list). Sources that can't be upgraded are skipped, with the
    reason; the others are asked at the same time, a few at once."""
    _require_enabled(session)
    results: dict[int, BulkAgentUpgradeResult] = {}
    to_send: list[Source] = []
    for source_id in dict.fromkeys(payload.source_ids):
        source = session.get(Source, source_id)
        if source is None or source.is_system:
            results[source_id] = BulkAgentUpgradeResult(
                source_id=source_id, name=None, result="skipped", detail="Not found"
            )
            continue
        reason = _not_upgradable(source)
        if reason is not None:
            results[source_id] = BulkAgentUpgradeResult(
                source_id=source_id, name=source.name, result="skipped", detail=reason
            )
            continue
        to_send.append(source)

    # Plain values only in the worker threads: the ORM objects belong to
    # this request's session.
    user_id = user.id
    targets = [(s.id, s.agent_version) for s in to_send]
    with ThreadPoolExecutor(max_workers=_BULK_PARALLEL) as pool:
        sent = list(pool.map(lambda t: _send_upgrade(t[0], t[1], user_id), targets))
    for source, (result, detail) in zip(to_send, sent, strict=True):
        if result != "busy":
            _audit_request(session, user, source, result, detail)
        results[source.id] = BulkAgentUpgradeResult(
            source_id=source.id, name=source.name, result=result, detail=detail
        )
    session.commit()
    return [results[source_id] for source_id in dict.fromkeys(payload.source_ids)]
