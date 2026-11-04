import hashlib
import ntpath
import posixpath
import re
import secrets
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app import source_roots
from app.agent_registry import get_agent_registry
from app.api.auth import get_current_active_user
from app.audit import record_audit_event
from app.auth.models import AuditLog, Capability, GlobalCapability, ScopeType, User
from app.auth.rbac import (
    FolderVisibilityContext,
    build_folder_visibility_context,
    delete_scope_grants,
    folder_path_boundary,
    require_capability,
    require_global_capability,
    resolve_capability,
    visible_source_ids,
)
from app.collectors import local as local_collector
from app.collectors import smb as smb_collector
from app.collectors import smb_shares
from app.collectors.base import SourceError, SourceNotFoundError
from app.collectors.errors import translate_smb, translate_winrm, translated
from app.collectors.registry import CONNECTORS
from app.crypto import encrypt_credential
from app.db import get_session
from app.logging_config import get_logger
from app.models import (
    Alert,
    Customer,
    Folder,
    Protocol,
    Rule,
    SeverityPattern,
    Source,
)
from app.search_index import delete_source_index

router = APIRouter(prefix="/sources", tags=["sources"])
logger = get_logger(__name__)

require_manage = require_global_capability(GlobalCapability.create_source, get_current_active_user)

_CONNECTORS = CONNECTORS


class FolderPathEntry(BaseModel):
    id: int
    name: str


class SourceRootModel(BaseModel):
    label: str
    path: str


class SourcePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    customer_id: int | None
    customer_name: str | None
    folder_id: int | None
    folder_name: str | None
    folder_path: list[FolderPathEntry]
    protocol: Protocol
    host: str
    port: int | None
    base_path: str
    roots: list[SourceRootModel] = []
    enabled: bool
    is_system: bool
    rule_count: int
    has_credential: bool
    has_agent_token: bool
    agent_connected: bool
    agent_last_seen_at: datetime | None
    search_indexing_enabled: bool
    # What the requesting user may do on this source (view, download,
    # manage_rules, run_now), so the UI can hide actions it would refuse --
    # e.g. Download buttons for a view-only role (#174). Informational only:
    # every endpoint still checks on its own.
    my_capabilities: list[Capability] = []


class SourceCreate(BaseModel):
    name: str
    customer_id: int | None = None
    folder_id: int | None = None
    protocol: Protocol
    host: str
    port: int | None = None
    base_path: str
    # Two or more folders read by one source (#161); see app/source_roots.py.
    # When given, base_path is ignored and the first root's path is used.
    roots: list[SourceRootModel] | None = None
    enabled: bool = True
    # Per-protocol JSON blob (SSH: username + private_key|password; SMB/WinRM:
    # username + password); omitted entirely for `local`, which needs none.
    credential: dict | None = None
    search_indexing_enabled: bool = False


class SourceUpdate(BaseModel):
    name: str | None = None
    customer_id: int | None = None
    folder_id: int | None = None
    host: str | None = None
    port: int | None = None
    base_path: str | None = None
    # [] or a single entry turns a multi-root source back into a plain one.
    roots: list[SourceRootModel] | None = None
    enabled: bool | None = None
    credential: dict | None = None
    search_indexing_enabled: bool | None = None


class ConnectionCheckResult(BaseModel):
    ok: bool
    detail: str


def _folder_path(
    ctx: FolderVisibilityContext, folder_id: int | None, boundary_folder_id: int | None
) -> list[FolderPathEntry]:
    """Ancestor chain for `folder_id`, root-to-leaf, excluding the customer
    itself -- lets a client reconstruct the nested customer/folder tree for
    whatever sources it's RBAC-visible for, without a separate admin-gated
    `/folders` call. Stops at `boundary_folder_id` (see
    `rbac.folder_path_boundary`) rather than always walking to the very
    top, so a narrow folder- or source-scoped grant doesn't leak the names
    of ancestor folders above its own scope. Reads from `ctx.folders_by_id`
    (built once per request) instead of each folder's lazy-loaded
    `parent_folder` relationship, so this doesn't cost a DB round trip per
    ancestor per source when serializing a whole source list."""
    chain: list[Folder] = []
    node_id = folder_id
    while node_id is not None:
        folder = ctx.folders_by_id.get(node_id)
        if folder is None:
            break
        chain.append(folder)
        if folder.id == boundary_folder_id:
            break
        node_id = folder.parent_folder_id
    chain.reverse()
    return [FolderPathEntry(id=f.id, name=f.name) for f in chain]


def _to_public(
    session: Session,
    source: Source,
    folder_ctx: FolderVisibilityContext,
    user: User | None = None,
) -> SourcePublic:
    rule_count = len(session.exec(select(Rule).where(Rule.source_id == source.id)).all())
    boundary = folder_path_boundary(folder_ctx, source)
    return SourcePublic(
        id=source.id,
        name=source.name,
        customer_id=source.customer_id,
        customer_name=source.customer.name if source.customer else None,
        folder_id=source.folder_id,
        folder_name=source.folder.name if source.folder else None,
        folder_path=_folder_path(folder_ctx, source.folder_id, boundary),
        protocol=source.protocol,
        host=source.host,
        port=source.port,
        base_path=source.base_path,
        roots=[SourceRootModel(**r) for r in source_roots.roots_of(source)],
        enabled=source.enabled,
        is_system=source.is_system,
        rule_count=rule_count,
        has_credential=source.credential_ref is not None,
        has_agent_token=source.agent_token_hash is not None,
        agent_connected=(
            source.protocol == Protocol.agent and get_agent_registry().is_connected(source.id)
        ),
        agent_last_seen_at=source.agent_last_seen_at,
        search_indexing_enabled=source.search_indexing_enabled,
        my_capabilities=(
            [c for c in Capability if resolve_capability(session, user, source, c)]
            if user is not None
            else []
        ),
    )


def _require_not_system(source: Source) -> None:
    # Per CLAUDE.md's "Built-in log viewer" section: the seeded system source
    # is non-editable, non-deletable, and its rules aren't user-configurable.
    if source.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="System sources cannot be modified"
        )


def _require_allowed_local_base_path(base_path: str) -> None:
    """See Settings.local_source_roots: an admin-created local-disk source
    may only point under a directory the server operator has explicitly
    allowed via the environment -- never anywhere else on this host."""
    if not local_collector.base_path_allowed(base_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Local-disk sources must point under a directory the server operator "
                "has allowed via LOCAL_SOURCE_ROOTS"
            ),
        )


_DRIVE_PATH = re.compile(r"^[A-Za-z]:([\\/]|$)")
_ADMIN_SHARE = re.compile(r"^([A-Za-z]\$|ADMIN\$|IPC\$)$", re.IGNORECASE)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def _validate_base_path(protocol: Protocol, base_path: str) -> str:
    """Catches the base paths that can never work for a protocol, with a
    message that says what to enter instead (#159). Only checked when a
    path is set or changed, so existing sources keep loading."""
    path = base_path.strip()
    if any(ord(c) < 32 for c in path):
        raise _bad_request("Base path contains control characters")
    if protocol == Protocol.smb:
        if not path:
            raise _bad_request(
                "SMB Base path is the share name, optionally with a folder inside it "
                "(e.g. Logs or Logs\\App) -- use Browse to pick one"
            )
        if _DRIVE_PATH.match(path):
            raise _bad_request(
                "SMB Base path is a share name, not a drive path like C:\\Logs. Share the log "
                "folder on the server (see docs/source-setup.md), or use WinRM to read a drive path"
            )
        if path.startswith(("\\\\", "//")):
            raise _bad_request(
                "SMB Base path is the share name only -- the server goes in Host, not in the path"
            )
        share = re.split(r"[\\/]", path)[0]
        if _ADMIN_SHARE.match(share):
            raise _bad_request(
                f"{share} is an administrative share. Share exactly the log folder instead "
                "(see docs/source-setup.md) so the account can't read the whole disk"
            )
    elif protocol == Protocol.winrm:
        if not (_DRIVE_PATH.match(path) or path.startswith("\\\\")):
            raise _bad_request(
                "WinRM Base path must be an absolute Windows path, e.g. D:\\Logs\\App"
            )
    elif protocol == Protocol.local:
        _require_allowed_local_base_path(path)
    return source_roots.normalize_root_path(protocol, path)


def _validate_roots(protocol: Protocol, roots: list[SourceRootModel]) -> list[dict] | None:
    """None for zero or one root (a plain single-folder source)."""
    if len(roots) <= 1:
        return None
    if protocol == Protocol.agent:
        raise _bad_request("A push-agent source serves a single folder; it can't have several")
    if len(roots) > source_roots.MAX_ROOTS:
        raise _bad_request(f"A source can read at most {source_roots.MAX_ROOTS} folders")
    seen: set[str] = set()
    out = []
    for root in roots:
        label = root.label.strip()
        if not source_roots.valid_label(label):
            raise _bad_request(
                f"Folder label {label!r} must be 1-64 letters, digits, spaces, dots, dashes "
                "or underscores, starting with a letter or digit"
            )
        if label.lower() in seen:
            raise _bad_request(f"Folder label {label!r} is used twice")
        seen.add(label.lower())
        out.append({"label": label, "path": _validate_base_path(protocol, root.path)})
    return out


def _location(source: Source) -> dict:
    return {
        "host": source.host,
        "port": source.port,
        "base_path": source.base_path,
        "roots": source.roots,
    }


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
    folder_ctx = build_folder_visibility_context(session, user)
    return [_to_public(session, s, folder_ctx, user) for s in sources]


@router.get("/recent", response_model=list[SourcePublic])
def list_recent_sources(
    limit: int = 8,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    """Sources this user has opened recently (browsed to the root of),
    most-recent-first and deduped to one entry per source -- powers the
    connections home page's "Recent" column (see archive.py's browse(),
    which is what records the underlying `source.open` AuditLog events).
    Re-checks current visibility so a revoked grant can't leak a source
    through here even if it was opened before the grant was pulled. Must be
    registered before GET /{source_id} so "recent" isn't swallowed as a
    source_id path param.
    """
    visible = visible_source_ids(session, user)
    if visible is not None and not visible:
        return []

    # A generous but bounded window, not the user's whole history -- same
    # "safety valve, not a design goal" spirit as the scratch size guard.
    events = session.exec(
        select(AuditLog)
        .where(AuditLog.user_id == user.id, AuditLog.action == "source.open")
        .order_by(AuditLog.timestamp.desc())
        .limit(max(limit * 20, 100))
    ).all()

    ordered_ids: list[int] = []
    seen: set[int] = set()
    for event in events:
        source_id = event.target_id
        if source_id is None or source_id in seen:
            continue
        if visible is not None and source_id not in visible:
            continue
        seen.add(source_id)
        ordered_ids.append(source_id)
        if len(ordered_ids) >= limit:
            break

    if not ordered_ids:
        return []
    sources_by_id = {
        s.id: s for s in session.exec(select(Source).where(Source.id.in_(ordered_ids))).all()
    }
    folder_ctx = build_folder_visibility_context(session, user)
    return [
        _to_public(session, sources_by_id[sid], folder_ctx, user)
        for sid in ordered_ids
        if sid in sources_by_id
    ]


@router.get("/{source_id}", response_model=SourcePublic)
def get_source(
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
):
    return _to_public(session, source, build_folder_visibility_context(session, user), user)


@router.post("", response_model=SourcePublic, status_code=status.HTTP_201_CREATED)
def create_source(
    payload: SourceCreate,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    _validate_scope(session, customer_id=payload.customer_id, folder_id=payload.folder_id)
    roots = _validate_roots(payload.protocol, payload.roots or [])
    if roots:
        base_path = roots[0]["path"]
    elif payload.roots:
        base_path = _validate_base_path(payload.protocol, payload.roots[0].path)
    elif payload.protocol == Protocol.agent:
        base_path = payload.base_path
    else:
        base_path = _validate_base_path(payload.protocol, payload.base_path)

    source = Source(
        name=payload.name,
        customer_id=payload.customer_id,
        folder_id=payload.folder_id,
        protocol=payload.protocol,
        host=payload.host,
        port=payload.port,
        base_path=base_path,
        roots=roots,
        enabled=payload.enabled,
        credential_ref=encrypt_credential(payload.credential) if payload.credential else None,
        search_indexing_enabled=payload.search_indexing_enabled,
    )
    session.add(source)
    session.flush()
    record_audit_event(
        session,
        user_id=user.id,
        action="source.create",
        target_type="source",
        target_id=source.id,
        metadata={
            "name": source.name,
            "protocol": source.protocol,
            "host": source.host,
            "base_path": source.base_path,
            **({"roots": roots} if roots else {}),
        },
    )
    session.commit()
    session.refresh(source)
    return _to_public(session, source, build_folder_visibility_context(session, user), user)


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
    before = {"name": source.name, "enabled": source.enabled, **_location(source)}
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
    if payload.roots is not None:
        roots = _validate_roots(source.protocol, payload.roots)
        source.roots = roots
        if roots:
            source.base_path = roots[0]["path"]
        elif payload.roots:
            source.base_path = _validate_base_path(source.protocol, payload.roots[0].path)
    if payload.base_path is not None and not source_roots.is_multi_root(source):
        if not source_roots.same_path(source.protocol, payload.base_path, source.base_path):
            source.base_path = (
                payload.base_path
                if source.protocol == Protocol.agent
                else _validate_base_path(source.protocol, payload.base_path)
            )
    if payload.enabled is not None:
        source.enabled = payload.enabled
    if payload.credential is not None:
        source.credential_ref = encrypt_credential(payload.credential)
    if payload.search_indexing_enabled is not None:
        source.search_indexing_enabled = payload.search_indexing_enabled
    after = {"name": source.name, "enabled": source.enabled, **_location(source)}
    location_changed = any(before[k] != after[k] for k in _location(source))
    if location_changed or not (source.enabled and source.search_indexing_enabled):
        # Opting out of search (or disabling the source) means its content
        # stops being kept: drop the stored snippets now, not never. A new
        # host or path is a different set of files (#159), so what was
        # indexed (and how far alerts got into each file) no longer applies.
        delete_source_index(session, source.id)

    changes = {k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]}
    if payload.credential is not None:
        changes["credential"] = "replaced"
    session.add(source)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.update",
        target_type="source",
        target_id=source.id,
        metadata={"changes": changes} if changes else None,
    )
    session.commit()
    session.refresh(source)
    return _to_public(session, source, build_folder_visibility_context(session, user), user)


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

    # Everything that points at this source by id goes with it -- nothing may
    # outlive it and resurface under a later source (see app.models.NO_ID_REUSE).
    delete_scope_grants(session, ScopeType.source, source_id)
    delete_source_index(session, source_id)
    for model in (Alert, SeverityPattern):
        for row in session.exec(select(model).where(model.source_id == source_id)).all():
            session.delete(row)
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
    user: User = Depends(get_current_active_user),
):
    """On-demand reachability check — lists the source's base path through
    its connector. Replaces the pre-pivot "run-now"/"last run" concept from
    CLAUDE.md's original Web UI wording: there is no scheduled sync anymore,
    so there is nothing to run, only a live connection to test (see
    ROADMAP.md's M7 notes).

    Anyone with `view` can run it, so the answer is the mapped, actionable
    message (SourceError.public_message) plus the effective base path; the
    raw library error, which can carry UNC paths, host keys or internals,
    is added only for users who can manage sources (#170)."""
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        return ConnectionCheckResult(ok=False, detail=f"protocol {source.protocol} not supported")

    # Each root of a multi-root source is checked on its own (#161), so the
    # answer names the folder that fails rather than just "failed".
    roots = source_roots.roots_of(source) or [{"label": None, "path": source.base_path}]
    failures = []
    for root in roots:
        target = source_roots.with_base_path(source, root["path"])
        try:
            connector.list_directory(target, [], "")
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller, not swallowed
            logger.warning(
                "source.check.failed", source_id=source.id, root=root["label"], error=str(exc)
            )
            failures.append(_check_failure_message(source, root, exc, user))

    if failures:
        return ConnectionCheckResult(ok=False, detail="; ".join(failures))
    if len(roots) > 1:
        return ConnectionCheckResult(ok=True, detail=f"Connected ({len(roots)} folders)")
    return ConnectionCheckResult(ok=True, detail="Connected")


def _check_failure_message(source: Source, root: dict, exc: Exception, user: User) -> str:
    where = f"base path: {root['path'] or '/'}"
    if root["label"]:
        where = f"folder {root['label']}: {root['path']}"
    if isinstance(exc, SourceError):
        message = f"{exc.public_message} ({where})"
        if source.protocol == Protocol.ssh and isinstance(exc, SourceNotFoundError):
            message += (
                ". If this account has a ChrootDirectory, Base path is relative to the "
                "chroot (usually /)."
            )
    else:
        message = f"The connection check failed ({where})"
    if _can_manage_sources(user):
        message += f" — {exc}"
    return message


def _can_manage_sources(user: User) -> bool:
    role = user.role
    return role.is_super_admin or GlobalCapability.create_source in role.global_capabilities


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


# --- Base path picker (#159) ---------------------------------------------

_PICKER_PROTOCOLS = (Protocol.ssh, Protocol.smb, Protocol.winrm)
_PICKER_MAX_ENTRIES = 1000


class FolderPickRequest(BaseModel):
    """What to connect to. Either a saved source (`source_id`, whose host,
    port and credential are used unless overridden) or everything needed
    for one that isn't saved yet. `path` is where to list: "" is the top
    (`/` over SSH, the drives over WinRM, the shares over SMB)."""

    source_id: int | None = None
    protocol: Protocol | None = None
    host: str | None = None
    port: int | None = None
    credential: dict | None = None
    path: str = ""


class FolderPickEntry(BaseModel):
    name: str
    path: str


class FolderPickResult(BaseModel):
    protocol: Protocol
    path: str
    parent: str | None
    # "shares" and "drives" are the SMB and WinRM top levels; everything
    # else is "folders".
    kind: Literal["folders", "shares", "drives"]
    entries: list[FolderPickEntry]
    truncated: bool


def _picker_source(session: Session, payload: FolderPickRequest) -> Source:
    saved = session.get(Source, payload.source_id) if payload.source_id is not None else None
    if payload.source_id is not None and saved is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    if saved is not None:
        _require_not_system(saved)
    protocol = saved.protocol if saved is not None else payload.protocol
    if protocol not in _PICKER_PROTOCOLS:
        raise _bad_request("Browsing for a folder works with SSH, SMB and WinRM sources")
    host = (payload.host or (saved.host if saved else "")).strip()
    if not host:
        raise _bad_request("Enter the host first")
    port = payload.port if "port" in payload.model_fields_set else (saved.port if saved else None)

    if payload.credential is not None:
        credential_ref = encrypt_credential(payload.credential)
    elif saved is not None and saved.credential_ref is not None:
        # The saved credential is only ever sent to the host it was saved
        # for -- pointing the picker somewhere else needs it typed again.
        if (host, port) != (saved.host, saved.port):
            raise _bad_request("Enter the credential again to browse a different host or port")
        credential_ref = saved.credential_ref
    else:
        raise _bad_request("Enter the credential first")

    return Source(
        id=saved.id if saved else None,
        name=saved.name if saved else "picker",
        protocol=protocol,
        host=host,
        port=port,
        credential_ref=credential_ref,
        base_path="",
    )


def _list_folders(connector, source: Source, path: str) -> list[str]:
    target = source_roots.with_base_path(source, path)
    return sorted(
        (e.name for e in connector.list_directory(target, source_roots.ALLOW_ALL, "") if e.is_dir),
        key=str.lower,
    )


def _pick(source: Source, path: str) -> tuple[str, str | None, str, list[FolderPickEntry]]:
    connector = _CONNECTORS[source.protocol]
    if source.protocol == Protocol.ssh:
        path = posixpath.normpath(path) if path else "/"
        if not path.startswith("/"):
            path = "/" + path
        names = _list_folders(connector, source, path)
        parent = None if path == "/" else posixpath.dirname(path)
        return (
            path,
            parent,
            "folders",
            [FolderPickEntry(name=n, path=posixpath.join(path, n)) for n in names],
        )

    path = path.strip().replace("/", "\\").rstrip("\\")
    if source.protocol == Protocol.winrm:
        if not path:
            with translated(translate_winrm):
                drives = connector.list_drives(source)
            return (
                "",
                None,
                "drives",
                [FolderPickEntry(name=d, path=d.rstrip("\\") + "\\") for d in drives],
            )
        if not _DRIVE_PATH.match(path) and not path.startswith("\\\\"):
            raise _bad_request("Pick a drive first, e.g. D:\\")
        listed = path + "\\" if re.fullmatch(r"[A-Za-z]:", path) else path
        names = _list_folders(connector, source, listed)
        parent = "" if re.fullmatch(r"[A-Za-z]:", path) else ntpath.dirname(path)
        if re.fullmatch(r"[A-Za-z]:", parent):
            parent += "\\"
        return (
            listed,
            parent,
            "folders",
            [FolderPickEntry(name=n, path=f"{path}\\{n}") for n in names],
        )

    # SMB: the top level is the server's shares.
    if not path:
        with translated(translate_smb):
            shares = smb_shares.list_shares(source.host, smb_collector._connect_kwargs(source))
        return "", None, "shares", [FolderPickEntry(name=s.name, path=s.name) for s in shares]
    names = _list_folders(connector, source, path)
    parent = path.rsplit("\\", 1)[0] if "\\" in path else ""
    return path, parent, "folders", [FolderPickEntry(name=n, path=f"{path}\\{n}") for n in names]


@router.post("/pick-folder", response_model=FolderPickResult)
def pick_folder(
    payload: FolderPickRequest,
    user: User = Depends(require_manage),
    session: Session = Depends(get_session),
):
    """Lists the folders at `path` on a source's host so the Base path can
    be picked instead of typed (#159). Folders only, never file names, and
    only for users who can manage sources; each listing is audited, since
    it reaches outside any configured base path."""
    source = _picker_source(session, payload)
    path, parent, kind, entries = _pick(source, payload.path)
    record_audit_event(
        session,
        user_id=user.id,
        action="source.pick_folder",
        target_type="source" if source.id is not None else None,
        target_id=source.id,
        metadata={"protocol": source.protocol, "host": source.host, "path": path},
    )
    session.commit()
    return FolderPickResult(
        protocol=source.protocol,
        path=path,
        parent=parent,
        kind=kind,
        entries=entries[:_PICKER_MAX_ENTRIES],
        truncated=len(entries) > _PICKER_MAX_ENTRIES,
    )
