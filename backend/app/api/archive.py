import hashlib
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from app.api.auth import get_current_active_user
from app.archives import (
    decompress_gzip,
    extract_member,
    is_archive,
    is_transparent_gzip,
    list_members,
)
from app.auth.models import Capability
from app.auth.rbac import require_capability
from app.collectors import ssh as ssh_collector
from app.db import get_session
from app.models import Protocol, Rule, Source
from app.rules import is_safe_relative_path, is_visible
from app.scratch import get_scratch_store

router = APIRouter(prefix="/sources/{source_id}", tags=["archive"])

_CONNECTORS = {Protocol.ssh: ssh_collector}


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    is_archive: bool


def _connector(source: Source):
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"protocol {source.protocol} not yet supported",
        )
    return connector


def _rules_for(session: Session, source_id: int) -> list[Rule]:
    return list(session.exec(select(Rule).where(Rule.source_id == source_id)).all())


def _scratch_key(source_id: int, path: str, member: str | None) -> str:
    raw = f"{source_id}:{path}:{member or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_safe_path(path: str) -> None:
    if not is_safe_relative_path(path):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")


def _materialize(
    source: Source, path: str, member: str | None, rules: list[Rule], destination: Path
) -> str:
    """Fetches `path` (or `member` inside it, if `path` is an archive) into
    `destination`, applying transparent .gz decompression. Returns the
    filename to present to the client. Every call is a fresh fetch — see
    CLAUDE.md's "Live browsing & ephemeral fetch behavior"."""
    if not is_visible(path, rules):
        # Rules gate which files are reachable independent of RBAC — a user
        # may have view/download on the source overall but not on this
        # specific path.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    connector = _connector(source)
    filename = path.rsplit("/", 1)[-1]

    if member is not None:
        if not is_archive(filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not an archive")
        with tempfile.TemporaryDirectory() as tmp:
            archive_local = Path(tmp) / "archive"
            connector.fetch_file(source, path, archive_local)
            extract_member(archive_local, filename, member, destination)
        return member.rsplit("/", 1)[-1]

    if is_transparent_gzip(filename):
        with tempfile.TemporaryDirectory() as tmp:
            raw_local = Path(tmp) / "raw"
            connector.fetch_file(source, path, raw_local)
            decompress_gzip(raw_local, destination)
        return filename[: -len(".gz")]

    connector.fetch_file(source, path, destination)
    return filename


@router.get("/browse", response_model=list[BrowseEntry])
def browse(
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    path: str = "",
    session: Session = Depends(get_session),
) -> list[BrowseEntry]:
    _require_safe_path(path)
    rules = _rules_for(session, source.id)
    connector = _connector(source)

    filename = path.rsplit("/", 1)[-1] if path else ""
    if path and is_archive(filename):
        if not is_visible(path, rules):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        with tempfile.TemporaryDirectory() as tmp:
            archive_local = Path(tmp) / "archive"
            connector.fetch_file(source, path, archive_local)
            members = list_members(archive_local, filename)
        return [
            BrowseEntry(
                name=member.name.rsplit("/", 1)[-1],
                path=f"{path}/{member.name}",
                is_dir=member.is_dir,
                size=member.size,
                is_archive=False,
            )
            for member in members
        ]

    entries = connector.list_directory(source, rules, path)
    return [
        BrowseEntry(
            name=entry.name,
            path=entry.path,
            is_dir=entry.is_dir,
            size=entry.size,
            is_archive=(not entry.is_dir) and is_archive(entry.name),
        )
        for entry in entries
    ]


@router.get("/open")
def open_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    session: Session = Depends(get_session),
) -> FileResponse:
    """Fetches into scratch and holds it (refcounted) until a matching
    /close call — for a persistent viewer session, not a one-shot
    download."""
    _require_safe_path(path)
    rules = _rules_for(session, source.id)

    store = get_scratch_store()
    key = _scratch_key(source.id, path, member)
    destination = store.acquire(key)
    try:
        filename = _materialize(source, path, member, rules, destination)
    except Exception:
        store.release(key)
        raise
    return FileResponse(destination, filename=filename, headers={"X-Scratch-Key": key})


class CloseRequest(BaseModel):
    path: str
    member: str | None = None


@router.post("/close", status_code=status.HTTP_204_NO_CONTENT)
def close_file(
    payload: CloseRequest,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
) -> None:
    store = get_scratch_store()
    key = _scratch_key(source.id, payload.path, payload.member)
    store.release(key)


@router.get("/download")
def download_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(require_capability(Capability.download, get_current_active_user)),
    session: Session = Depends(get_session),
) -> FileResponse:
    """One-shot: fetches, streams, and releases within the same request —
    no persistent session, so no /close call is needed afterward."""
    _require_safe_path(path)
    rules = _rules_for(session, source.id)

    store = get_scratch_store()
    key = _scratch_key(source.id, path, member)
    destination = store.acquire(key)
    try:
        filename = _materialize(source, path, member, rules, destination)
    except Exception:
        store.release(key)
        raise
    return FileResponse(
        destination,
        filename=filename,
        background=BackgroundTask(store.release, key),
    )
