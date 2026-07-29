import hashlib
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.agent_registry import get_agent_registry
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import require_capability, require_global_capability, visible_source_ids
from app.collectors import agent as agent_collector
from app.collectors import local as local_collector
from app.collectors import smb as smb_collector
from app.collectors import ssh as ssh_collector
from app.collectors import winrm as winrm_collector
from app.crypto import encrypt_credential
from app.db import get_session
from app.logging_config import get_logger
from app.models import Customer, Folder, Protocol, Rule, Source

router = APIRouter(prefix="/sources", tags=["sources"])
logger = get_logger(__name__)

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)

_CONNECTORS = {
    Protocol.ssh: ssh_collector,
    Protocol.smb: smb_collector,
    Protocol.winrm: winrm_collector,
    Protocol.local: local_collector,
    Protocol.agent: agent_collector,
}


class SourcePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    customer_id: int | None
    folder_id: int | None
    protocol: Protocol
    host: str
    port: int | None
    base_path: str
    enabled: bool
    is_system: bool
    rule_count: int
    has_credential: bool
    agent_connected: bool
    agent_last_seen_at: datetime | None


class SourceCreate(BaseModel):
    name: str
    customer_id: int | None = None
    folder_id: int | None = None
    protocol: Protocol
    host: str
    port: int | None = None
    base_path: str
    enabled: bool = True
    # Per-protocol JSON blob (SSH: username + private_key|password; SMB/WinRM:
    # username + password); omitted entirely for `local`, which needs none.
    credential: dict | None = None


class SourceUpdate(BaseModel):
    name: str | None = None
    customer_id: int | None = None
    folder_id: int | None = None
    host: str | None = None
    port: int | None = None
    base_path: str | None = None
    enabled: bool | None = None
    credential: dict | None = None


class ConnectionCheckResult(BaseModel):
    ok: bool
    detail: str


def _to_public(session: Session, source: Source) -> SourcePublic:
    rule_count = len(session.exec(select(Rule).where(Rule.source_id == source.id)).all())
    return SourcePublic(
        id=source.id,
        name=source.name,
        customer_id=source.customer_id,
        folder_id=source.folder_id,
        protocol=source.protocol,
        host=source.host,
        port=source.port,
        base_path=source.base_path,
        enabled=source.enabled,
        is_system=source.is_system,
        rule_count=rule_count,
        has_credential=source.credential_ref is not None,
        agent_connected=(
            source.protocol == Protocol.agent and get_agent_registry().is_connected(source.id)
        ),
        agent_last_seen_at=source.agent_last_seen_at,
    )


def _require_not_system(source: Source) -> None:
    # Per CLAUDE.md's "Built-in log viewer" section: the seeded system source
    # is non-editable, non-deletable, and its rules aren't user-configurable.
    if source.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="System sources cannot be modified"
        )


def _validate_scope(session: Session, *, customer_id: int | None, folder_id: int | None) -> None:
    if folder_id is not None:
        folder = session.get(Folder, folder_id)
        if folder is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found")
        if customer_id is not None and folder.customer_id != customer_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="folder_id must belong to customer_id",
            )
    elif customer_id is not None:
        if session.get(Customer, customer_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")


@router.get("", response_model=list[SourcePublic])
def list_sources(
    user: User = Depends(get_current_active_user), session: Session = Depends(get_session)
):
    ids = visible_source_ids(session, user)
    query = select(Source)
    if ids is not None:
        if not ids:
            return []
        query = query.where(Source.id.in_(ids))
    else:
        query = query.order_by(Source.id)
    sources = session.exec(query).all()
    return [_to_public(session, s) for s in sources]


@router.get("/{source_id}", response_model=SourcePublic)
def get_source(
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    session: Session = Depends(get_session),
):
    return _to_public(session, source)


@router.post("", response_model=SourcePublic, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    _validate_scope(session, customer_id=payload.customer_id, folder_id=payload.folder_id)

    source = Source(
        name=payload.name,
        customer_id=payload.customer_id,
        folder_id=payload.folder_id,
        protocol=payload.protocol,
        host=payload.host,
        port=payload.port,
        base_path=payload.base_path,
        enabled=payload.enabled,
        credential_ref=encrypt_credential(payload.credential) if payload.credential else None,
    )
    session.add(source)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="source.create",
        target_type="source",
        target_id=source.id,
        metadata={"name": source.name, "protocol": source.protocol},
    )
    session.commit()
    session.refresh(source)
    return _to_public(session, source)


@router.patch("/{source_id}", response_model=SourcePublic)
def update_source(
    source_id: int,
    payload: SourceUpdate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    _require_not_system(source)

    fields_set = payload.model_fields_set
    new_customer_id = payload.customer_id if "customer_id" in fields_set else source.customer_id
    new_folder_id = payload.folder_id if "folder_id" in fields_set else source.folder_id
    if "customer_id" in fields_set or "folder_id" in fields_set:
        _validate_scope(session, customer_id=new_customer_id, folder_id=new_folder_id)
        source.customer_id = new_customer_id
        source.folder_id = new_folder_id

    if payload.name is not None:
        source.name = payload.name
    if payload.host is not None:
        source.host = payload.host
    if "port" in fields_set:
        source.port = payload.port
    if payload.base_path is not None:
        source.base_path = payload.base_path
    if payload.enabled is not None:
        source.enabled = payload.enabled
    if payload.credential is not None:
        source.credential_ref = encrypt_credential(payload.credential)

    session.add(source)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.update",
        target_type="source",
        target_id=source.id,
    )
    session.commit()
    session.refresh(source)
    return _to_public(session, source)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(
    source_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    _require_not_system(source)

    session.delete(source)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.delete",
        target_type="source",
        target_id=source_id,
    )
    session.commit()


@router.post("/{source_id}/check", response_model=ConnectionCheckResult)
def check_connection(
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
):
    """On-demand reachability check — lists the source's base path through
    its connector. Replaces the pre-pivot "run-now"/"last run" concept from
    CLAUDE.md's original Web UI wording: there is no scheduled sync anymore,
    so there is nothing to run, only a live connection to test (see
    ROADMAP.md's M7 notes)."""
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        return ConnectionCheckResult(ok=False, detail=f"protocol {source.protocol} not supported")

    try:
        connector.list_directory(source, [], "")
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
        logger.warning("source.check.failed", source_id=source.id, error=str(exc))
        return ConnectionCheckResult(ok=False, detail=str(exc))

    return ConnectionCheckResult(ok=True, detail="Connected")


class AgentTokenResult(BaseModel):
    token: str


@router.post("/{source_id}/agent-token", response_model=AgentTokenResult)
def regenerate_agent_token(
    source_id: int,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Issues a fresh enrollment token for an agent-protocol source (see
    Protocol.agent's docstring in app/models.py). Only the hash is stored —
    same pattern as auth/sessions.py's session tokens — so the plaintext is
    returned here once and never again; regenerating invalidates whatever
    token the agent config was using before, same UX as an admin resetting a
    user's password."""
    source = session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if source.protocol != Protocol.agent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only agent-protocol sources have an enrollment token",
        )

    token = secrets.token_urlsafe(32)
    source.agent_token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    session.add(source)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.agent_token_regenerate",
        target_type="source",
        target_id=source.id,
    )
    session.commit()
    return AgentTokenResult(token=token)
