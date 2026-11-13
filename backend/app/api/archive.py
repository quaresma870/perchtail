import hashlib
import shutil
import tarfile
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.background import BackgroundTask

from app import source_roots
from app.api.auth import get_current_active_user
from app.archives import (
    DecompressionLimitError,
    decompress_gzip,
    extract_member,
    is_archive,
    is_transparent_gzip,
    list_members,
    normalize_member_name,
    resolve_member_name,
    tree_children,
)
from app.audit import record_audit_event
from app.auth.models import Capability, User
from app.auth.rbac import require_capability
from app.collectors import local as local_collector
from app.collectors.base import (
    RANGE_MAX_BYTES,
    DirEntry,
    FetchTooLargeError,
    RangeNotSupportedError,
    max_fetch_bytes,
)
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.db import get_session
from app.logging_config import get_logger
from app.models import Protocol, Rule, RuleType, Source
from app.rules import (
    is_dir_visible,
    is_entry_visible,
    is_explicitly_excluded,
    is_safe_relative_path,
    is_safe_windows_relative_path,
    is_visible,
    rules_case_insensitive,
)
from app.scratch import get_scratch_store

router = APIRouter(prefix="/sources/{source_id}", tags=["archive"])
logger = get_logger(__name__)

_CONNECTORS = CONNECTORS


class BrowseEntry(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    is_archive: bool
    # Last modification, seconds since the epoch (#273); null when the
    # source doesn't say.
    mtime: float | None = None


def _connector(source: Source):
    _require_local_source_allowed(source)
    connector = _CONNECTORS.get(source.protocol)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail=f"protocol {source.protocol} not yet supported",
        )
    return connector


def _require_local_source_allowed(source: Source) -> None:
    """See Settings.local_source_roots -- refused up front with a clean 403
    rather than surfacing as a connector error mid-request."""
    if (
        source.protocol == Protocol.local
        and not source.is_system
        and not all(
            local_collector.base_path_allowed(path)
            for path in [source.base_path] + [r["path"] for r in source_roots.roots_of(source)]
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This local-disk source is outside the server's allowed directories",
        )


def _rules_for(session: Session, source_id: int) -> list[Rule]:
    return list(session.exec(select(Rule).where(Rule.source_id == source_id)).all())


def _scratch_key(source_id: int, path: str, member: str | None) -> str:
    raw = f"{source_id}:{path}:{member or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require_safe_path(source: Source, path: str) -> None:
    """Rejects anything but a canonical path (see rules.is_safe_relative_path)
    -- rules are matched against the path as written, so a non-canonical
    spelling of an excluded file must never get as far as a rule check."""
    safe = is_safe_relative_path(path)
    if safe and rules_case_insensitive(source):
        safe = is_safe_windows_relative_path(path)
    if not safe:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid path")


def _split_archive_path(path: str) -> tuple[str, str] | None:
    """For a browse path that goes into an archive (`logs/a.zip` or
    `logs/a.zip/sub/dir`), returns (archive path, member directory inside
    it); None for an ordinary directory."""
    if not path:
        return None
    segments = path.split("/")
    for i, segment in enumerate(segments):
        if is_archive(segment):
            return "/".join(segments[: i + 1]), "/".join(segments[i + 1 :])
    return None


def _dir_visible(source: Source, path: str, rules: list[Rule]) -> bool:
    return is_dir_visible(path, rules, case_insensitive=rules_case_insensitive(source))


def _entry_visible(source: Source, path: str, is_dir: bool, rules: list[Rule]) -> bool:
    return is_entry_visible(path, is_dir, rules, case_insensitive=rules_case_insensitive(source))


def _visible(source: Source, path: str, rules: list[Rule]) -> bool:
    return is_visible(path, rules, case_insensitive=rules_case_insensitive(source))


def _max_decompressed_bytes() -> int:
    return int(get_settings().max_decompressed_mb * 1024**2)


def _too_large(exc: DecompressionLimitError) -> HTTPException:
    logger.warning("archive.decompression_limit", error=str(exc))
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail="File is too large once decompressed (MAX_DECOMPRESSED_MB)",
    )


def _fetch_too_large(exc: FetchTooLargeError, *, folder: bool = False) -> HTTPException:
    logger.warning("archive.fetch_limit", error=str(exc), size_bytes=exc.size, folder=folder)
    limit = exc.limit if exc.limit is not None else max_fetch_bytes()
    limit_mb = limit / 1024**2
    what = "folder" if folder else "file"
    if exc.size is not None:
        message = (
            f"This {what} is {exc.size / 1024**2:.1f} MB, over this server's "
            f"{limit_mb:.0f} MB limit (MAX_FETCH_MB)."
        )
    else:
        message = f"This {what} is over this server's {limit_mb:.0f} MB limit (MAX_FETCH_MB)."
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={
            "message": message,
            "error_code": "fetch_too_large",
            "size_bytes": exc.size,
            "limit_bytes": limit,
        },
    )


# Deliberately neutral: neither the refusal nor SKIPPED.txt says why, so they
# don't reveal that the archive holds something the rules hide.
WHOLE_ARCHIVE_REFUSED_MESSAGE = (
    "This archive can't be downloaded whole: open or download its files one by one."
)
WHOLE_ARCHIVE_REFUSED_ERROR_CODE = "archive_not_downloadable_whole"
_SKIPPED_REASON = "can't be included whole; open or download its files one by one"
# The reason recorded in the audit log, which only view_audit_log can read.
_AUDIT_REFUSAL_REASON = "archive_hidden_members"


class WholeArchiveRefused(HTTPException):
    """403 for a whole archive that holds a file the rules hide; /open and
    /download audit it before passing it on."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": WHOLE_ARCHIVE_REFUSED_MESSAGE,
                "error_code": WHOLE_ARCHIVE_REFUSED_ERROR_CODE,
            },
        )


def _archive_has_hidden_members(
    source: Source, path: str, local_archive: Path, rules: list[Rule]
) -> bool:
    """Whether the archive at `path` (already copied to `local_archive`)
    holds a file the rules hide on purpose: the last rule matching its
    virtual path (`<archive>/<member>`, the same path the explorer and
    /open check) is an exclude. Members no rule matches don't count -- a
    rule that shows only the archive still lets it be downloaded whole. An
    archive that can't be read as one, or that holds another archive (whose
    own members the rules are never checked against), counts as hiding
    something whenever the source has an exclude rule. Every entry counts,
    a tar's links, devices and FIFOs included: they are never listed or
    opened, but the whole archive still carries their names."""
    has_exclude = any(rule.type == RuleType.exclude for rule in rules)
    filename = path.rsplit("/", 1)[-1]
    try:
        members = list_members(local_archive, filename, include_special=True)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, zlib.error, ValueError):
        return has_exclude
    case_insensitive = rules_case_insensitive(source)
    for member in members:
        if member.is_dir:
            continue
        name = normalize_member_name(member.name)
        if has_exclude and is_archive(name.rsplit("/", 1)[-1]):
            return True
        if is_explicitly_excluded(f"{path}/{name}", rules, case_insensitive=case_insensitive):
            return True
    return False


def _require_whole_archive_allowed(
    source: Source, path: str, local_archive: Path, rules: list[Rule]
) -> None:
    """A whole archive carries every member's bytes, so it is only served
    when none of them is hidden by the rules (_archive_has_hidden_members);
    its visible files stay reachable one by one through `member=`."""
    if _archive_has_hidden_members(source, path, local_archive, rules):
        logger.warning("archive.whole_archive_refused", source_id=source.id, path=path)
        raise WholeArchiveRefused()


def _materialize(
    source: Source, path: str, member: str | None, rules: list[Rule], destination: Path
) -> str:
    """Fetches `path` (or `member` inside it, if `path` is an archive) into
    `destination`, applying transparent .gz decompression. Returns the
    filename to present to the client. Every call is a fresh fetch — see
    CLAUDE.md's "Live browsing & ephemeral fetch behavior"."""
    if not _visible(source, path, rules):
        # Rules gate which files are reachable independent of RBAC — a user
        # may have view/download on the source overall but not on this
        # specific path.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    connector = _connector(source)
    filename = path.rsplit("/", 1)[-1]

    if member is not None:
        if not is_archive(filename):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not an archive")
        # The archive's own path passing is_visible above is not enough — a
        # rule can scope down to specific members inside it (matched via the
        # combined virtual path, same convention /browse uses), and a client
        # calling /open or /download directly bypasses whatever the browse
        # listing filtered out, so this has to be checked again here.
        member = normalize_member_name(member)
        if not member or not _visible(source, f"{path}/{member}", rules):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        try:
            with connector.local_copy(source, path) as archive_local:
                raw_name = resolve_member_name(archive_local, filename, member)
                extract_member(
                    archive_local, filename, raw_name, destination, _max_decompressed_bytes()
                )
            if is_archive(member.rsplit("/", 1)[-1]):
                # A member that is itself an archive is served whole, so it
                # gets the same check as any whole archive, at its virtual
                # path.
                _require_whole_archive_allowed(source, f"{path}/{member}", destination, rules)
        except DecompressionLimitError as exc:
            raise _too_large(exc) from exc
        except KeyError as exc:
            # Not in the archive, or not a regular file (a link, a device):
            # either way there's nothing to open under that name.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found") from exc
        return member.rsplit("/", 1)[-1]

    if is_transparent_gzip(filename):
        try:
            with connector.local_copy(source, path) as raw_local:
                decompress_gzip(raw_local, destination, _max_decompressed_bytes())
        except DecompressionLimitError as exc:
            raise _too_large(exc) from exc
        return filename[: -len(".gz")]

    if is_archive(filename):
        # Read from the source once: the same copy is checked and served.
        with connector.local_copy(source, path) as archive_local:
            _require_whole_archive_allowed(source, path, Path(archive_local), rules)
            shutil.copyfile(archive_local, destination)
        return filename

    connector.fetch_file(source, path, destination)
    return filename


@router.get("/browse", response_model=list[BrowseEntry])
def browse(
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    path: str = "",
    # Set by the explorer's refresh (#272): re-listing a source the user
    # already has open isn't opening it again, so it isn't audited.
    refresh: bool = False,
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> list[BrowseEntry]:
    _require_safe_path(source, path)
    if path == "" and not refresh:
        # Root browse == "opened this source" -- the connections home page's
        # Recent column (GET /sources/recent) reads this back. Deliberately
        # not logged on every sub-directory expand, only the first hop into
        # a source, to keep this proportionate to real navigation events
        # rather than growing with every click.
        record_audit_event(
            session,
            user_id=user.id,
            action="source.open",
            target_type="source",
            target_id=source.id,
        )
        session.commit()
    rules = _rules_for(session, source.id)
    connector = _connector(source)

    split = _split_archive_path(path)
    if split is not None:
        archive_path, member_dir = split
        archive_name = archive_path.rsplit("/", 1)[-1]
        if not _visible(source, archive_path, rules):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        if member_dir and not _dir_visible(source, path, rules):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        with connector.local_copy(source, archive_path) as archive_local:
            members = list_members(archive_local, archive_name)
        children = tree_children(members, member_dir)
        return [
            BrowseEntry(
                name=child.name.rsplit("/", 1)[-1],
                path=f"{archive_path}/{child.name}",
                is_dir=child.is_dir,
                size=child.size,
                is_archive=False,
                mtime=child.mtime,
            )
            for child in children
            # Same filter as regular listings: files through the rule chain,
            # folders only when something below them could be visible. A
            # rule scoped to show just the archive itself doesn't leak its
            # members.
            if _entry_visible(source, f"{archive_path}/{child.name}", child.is_dir, rules)
        ]

    if path and not _dir_visible(source, path, rules):
        # A folder the rules hide is not browsable at all -- otherwise its
        # sub-folder names could still be enumerated by calling the API.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    entries = connector.list_directory(source, rules, path)
    return [
        BrowseEntry(
            name=entry.name,
            path=entry.path,
            is_dir=entry.is_dir,
            size=entry.size,
            is_archive=(not entry.is_dir) and is_archive(entry.name),
            mtime=entry.mtime,
        )
        for entry in entries
    ]


def _resolve_content(
    source: Source, path: str, member: str | None, rules: list[Rule]
) -> tuple[Path, str, str | None]:
    """Returns (path_to_serve, filename, scratch_key). scratch_key is None
    when nothing was fetched/copied — currently only a local, plain file
    (not an archive member, not transparent-gzip), served straight from its
    real path with no ephemeral scratch involved at all (CLAUDE.md's Built-
    in log viewer note: "a file already on the same machine ... is
    inherently zero-latency and always fresh"). Everything else (remote
    protocols always; local .gz/archive-member, since those produce derived
    bytes that must live somewhere) goes through the scratch store."""
    if not path:
        # Browsing the root is "", but opening or downloading needs a file.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    if not _visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    filename = path.rsplit("/", 1)[-1]
    needs_scratch = (
        source.protocol != Protocol.local or member is not None or is_transparent_gzip(filename)
    )

    if not needs_scratch:
        _require_local_source_allowed(source)
        if source_roots.is_multi_root(source):
            rooted, inner = source_roots.split(source, path)
            resolved = local_collector.resolve_path(rooted, inner)
        else:
            resolved = local_collector.resolve_path(source, path)
        if is_archive(filename):
            _require_whole_archive_allowed(source, path, resolved, rules)
        return resolved, filename, None

    store = get_scratch_store()
    key = _scratch_key(source.id, path, member)
    destination = store.acquire(key)
    try:
        served_filename = _materialize(source, path, member, rules, destination)
    except FetchTooLargeError as exc:
        store.release(key)
        raise _fetch_too_large(exc) from exc
    except Exception:
        store.release(key)
        raise
    return destination, served_filename, key


def _resolve_content_audited(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    user: User,
    session: Session,
    denied_action: str,
) -> tuple[Path, str, str | None]:
    """_resolve_content, recording a refused whole archive in the audit log
    (`denied_action`) before the 403 goes out, so every attempt leaves a
    trace. For an archive inside an archive (`member=`), the path recorded
    is the refused archive's virtual path, `<archive>/<member>`."""
    try:
        return _resolve_content(source, path, member, rules)
    except WholeArchiveRefused:
        refused_path = path if member is None else f"{path}/{normalize_member_name(member)}"
        record_audit_event(
            session,
            user_id=user.id,
            action=denied_action,
            target_type="source",
            target_id=source.id,
            metadata={"path": refused_path, "reason": _AUDIT_REFUSAL_REASON},
        )
        session.commit()
        raise


@router.get("/open")
def open_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """Fetches into scratch and holds it (refcounted) until a matching
    /close call — for a persistent viewer session, not a one-shot download.
    (Local plain files skip scratch entirely — see _resolve_content.)

    Viewing necessarily hands the client the file's bytes, so `view` can
    read a whole file; `download` gates bulk export (/download, the folder
    ZIP). Every open is audited so reads are as traceable as downloads."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    served_path, filename, key = _resolve_content_audited(
        source, path, member, rules, user, session, "file.open_denied"
    )
    record_audit_event(
        session,
        user_id=user.id,
        action="file.open",
        target_type="source",
        target_id=source.id,
        metadata={"path": path, "member": member} if member else {"path": path},
    )
    session.commit()
    headers = {"X-Scratch-Key": key} if key else {}
    if member is None and not is_transparent_gzip(path.rsplit("/", 1)[-1]):
        # What the first Follow poll compares against (#254), so a file
        # rotated between this open and that poll is caught too.
        size = served_path.stat().st_size
        with served_path.open("rb") as served:
            headers["X-Follow-Head"] = _head_hash(served.read(FOLLOW_HEAD_BYTES), size)
    # Inline, not an attachment: this is what the viewer reads to display a
    # file, not a download endpoint (downloads go through /download, gated
    # by the `download` capability -- #174). nosniff (set app-wide) keeps
    # a browser from rendering it as HTML.
    return FileResponse(
        served_path,
        filename=filename,
        headers=headers,
        media_type="text/plain; charset=utf-8",
        content_disposition_type="inline",
    )


# Incremental follow (#254). The start of the file identifies it: a rotated
# or truncated log almost always starts differently, even when it has grown
# back past the viewer's offset by the next poll.
FOLLOW_HEAD_BYTES = 64


def _follow_unsupported(message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"message": message, "error_code": "follow_unsupported"},
    )


def _head_hash(start: bytes, offset: int) -> str:
    """Identifies the file by the bytes the viewer already has from its
    start: the first min(FOLLOW_HEAD_BYTES, offset)."""
    return hashlib.sha256(start[: min(FOLLOW_HEAD_BYTES, offset)]).hexdigest()[:32]


@router.get("/follow")
def follow_file(
    path: str,
    # Below 2**62: an agent reads it as an int64.
    offset: int = Query(ge=0, lt=2**62),
    head: str | None = None,
    start: bool = False,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> Response:
    """Incremental follow (#254): what was appended to `path` after `offset`
    bytes, at most RANGE_MAX_BYTES at a time, read fresh from the source
    without transferring the rest of the file.

    `head` is the X-Follow-Head of the previous answer. Headers:
    X-File-Size (bytes now), X-Follow-Offset (the offset to ask from
    next), X-Follow-Head, X-Follow-More: 1 when more is waiting already,
    X-Follow-Reset: 1 (no body) when the file shrank or starts differently
    -- rotated or truncated -- so the viewer reloads it from the start.

    409 follow_unsupported for .gz files and archive members, and for an
    agent older than 0.6: the viewer reloads the whole file instead.
    Audited when following starts (`start`) and whenever a read begins at
    the start of the file, so a whole file can't be read through here
    without a record; not on every poll."""
    _require_safe_path(source, path)
    if not path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    rules = _rules_for(session, source.id)
    if not _visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    filename = path.rsplit("/", 1)[-1]
    if is_transparent_gzip(filename) or is_archive(filename):
        raise _follow_unsupported("Compressed files and archives can only be reloaded whole")

    connector = _connector(source)
    try:
        # One round trip: the size, the start of the file and the new bytes.
        size, first, data = connector.read_range(
            source, path, offset, RANGE_MAX_BYTES, FOLLOW_HEAD_BYTES
        )
    except RangeNotSupportedError as exc:
        raise _follow_unsupported(
            "This source's agent is older than 0.6 and can't send only the new lines"
        ) from exc

    changed = head is not None and offset > 0 and head != _head_hash(first, offset)
    if offset > size or changed:
        return Response(
            content=b"",
            media_type="application/octet-stream",
            headers={"X-Follow-Reset": "1", "X-File-Size": str(size)},
        )
    next_offset = offset + len(data)
    # What the viewer holds from the start once it appends `data`: the head
    # as read, extended by the new bytes while the file is still short.
    start_bytes = first[:offset] + data if offset < FOLLOW_HEAD_BYTES else first

    if start or offset == 0:
        record_audit_event(
            session,
            user_id=user.id,
            action="file.follow",
            target_type="source",
            target_id=source.id,
            metadata={"path": path},
        )
        session.commit()
    headers = {
        "X-File-Size": str(size),
        "X-Follow-Offset": str(next_offset),
        "X-Follow-Head": _head_hash(start_bytes, next_offset),
    }
    if next_offset < size:
        headers["X-Follow-More"] = "1"
    return Response(content=data, media_type="application/octet-stream", headers=headers)


class CloseRequest(BaseModel):
    path: str
    member: str | None = None


@router.post("/close", status_code=status.HTTP_204_NO_CONTENT)
def close_file(
    payload: CloseRequest,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
) -> None:
    # The scratch key is a hash, not a filesystem path, so an unsafe path
    # here can't actually escape anywhere — checked anyway for consistency
    # with every other endpoint that takes a client-supplied path.
    _require_safe_path(source, payload.path)
    store = get_scratch_store()
    key = _scratch_key(source.id, payload.path, payload.member)
    store.release(key)


@router.get("/download")
def download_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(require_capability(Capability.download, get_current_active_user)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """One-shot: fetches, streams, and releases within the same request —
    no persistent session, so no /close call is needed afterward."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    served_path, filename, key = _resolve_content_audited(
        source, path, member, rules, user, session, "file.download_denied"
    )
    # CLAUDE.md's Application logging section names file download as one of
    # the minimum AuditLog-worthy actions -- this was the one gap where that
    # never actually got wired up.
    record_audit_event(
        session,
        user_id=user.id,
        action="file.download",
        target_type="source",
        target_id=source.id,
        metadata={"path": path, "member": member} if member else {"path": path},
    )
    session.commit()
    background = BackgroundTask(get_scratch_store().release, key) if key else None
    return FileResponse(served_path, filename=filename, background=background)


@dataclass
class _ZipBudget:
    """Bytes a folder ZIP may still collect -- the whole ZIP counts against
    MAX_FETCH_MB, not just each file in it."""

    remaining: int


def _zip_entries(connector, source: Source, rules: list[Rule], current_dir: str) -> list[DirEntry]:
    """Every rule-visible file under `current_dir`, from listings alone --
    nothing is fetched yet, so the folder's listed total can be checked
    against MAX_FETCH_MB before the first download (#191). Nested
    .zip/.tar.gz files are included as raw files rather than expanded --
    expanding archives-within-a-bulk-zip isn't worth the complexity for
    what CLAUDE.md asks for here (download a folder). An archive holding a
    file the rules hide is left out once fetched (see _zip_directory)."""
    out: list[DirEntry] = []
    for entry in connector.list_directory(source, rules, current_dir):
        if entry.is_dir:
            out.extend(_zip_entries(connector, source, rules, entry.path))
        elif _visible(source, entry.path, rules):
            out.append(entry)
    return out


def _arcname(path: str, request_root: str) -> str:
    return path[len(request_root) + 1 :] if request_root else path


def _zip_directory(
    connector,
    source: Source,
    rules: list[Rule],
    entries: list[DirEntry],
    request_root: str,
    zf: zipfile.ZipFile,
    tmp_dir: Path,
    budget: "_ZipBudget",
) -> list[str]:
    """Fetches each of `entries` fresh, straight into the zip -- same
    always-fresh rule as every other read in this project, just batched into
    one archive for convenience. Arcnames are relative to `request_root`.
    The fetched size is what counts against the budget: a file may have
    grown since it was listed. An archive holding a file the rules hide
    (_archive_has_hidden_members) is left out; returns the arcnames of the
    archives left out."""
    skipped: list[str] = []
    for entry in entries:
        arcname = _arcname(entry.path, request_root)
        with tempfile.NamedTemporaryFile(dir=tmp_dir, delete=False) as tmp_file:
            destination = Path(tmp_file.name)
        connector.fetch_file(source, entry.path, destination)
        if is_archive(entry.name) and _archive_has_hidden_members(
            source, entry.path, destination, rules
        ):
            destination.unlink()
            skipped.append(arcname)
            continue
        budget.remaining -= destination.stat().st_size
        if budget.remaining < 0:
            destination.unlink()
            raise FetchTooLargeError("folder is over the MAX_FETCH_MB limit")
        zf.write(destination, arcname=arcname)
        destination.unlink()
    return skipped


def _skipped_list_name(entries: list[DirEntry], request_root: str) -> str:
    """SKIPPED.txt, unless the folder already has a file by that name at its
    root (compared ignoring case, so extracting on Windows can't collide)."""
    root_names = {_arcname(entry.path, request_root).lower() for entry in entries}
    return "SKIPPED (PerchTail).txt" if "skipped.txt" in root_names else "SKIPPED.txt"


@router.get("/download-zip")
def download_folder_zip(
    path: str = "",
    source: Source = Depends(require_capability(Capability.download, get_current_active_user)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """Zips an entire folder for download (CLAUDE.md's Viewer section:
    download "single file or a zipped folder"), fetching each contained
    file fresh — no different from any other read here, just batched."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    if path and not _dir_visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    connector = _connector(source)

    limit = max_fetch_bytes()
    entries = _zip_entries(connector, source, rules, path)
    listed = sum(entry.size for entry in entries)
    if listed > limit:
        raise _fetch_too_large(
            FetchTooLargeError("folder is over the MAX_FETCH_MB limit", size=listed, limit=limit),
            folder=True,
        )

    tmp_dir = Path(tempfile.mkdtemp(prefix="perchtail-zip-"))
    zip_path = tmp_dir / "download.zip"
    budget = _ZipBudget(remaining=limit)
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            skipped = _zip_directory(connector, source, rules, entries, path, zf, tmp_dir, budget)
            file_count = len(zf.namelist())
            if skipped:
                zf.writestr(
                    _skipped_list_name(entries, path),
                    "".join(f"{arcname}: {_SKIPPED_REASON}\n" for arcname in skipped),
                )
    except FetchTooLargeError as exc:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise _fetch_too_large(exc, folder=True) from exc
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    record_audit_event(
        session,
        user_id=user.id,
        action="folder.download",
        target_type="source",
        target_id=source.id,
        metadata=(
            {"path": path, "file_count": file_count, "skipped_archives": skipped}
            if skipped
            else {"path": path, "file_count": file_count}
        ),
    )
    session.commit()

    folder_name = path.rsplit("/", 1)[-1] if path else source.name
    background = BackgroundTask(shutil.rmtree, tmp_dir, ignore_errors=True)
    return FileResponse(zip_path, filename=f"{folder_name}.zip", background=background)
