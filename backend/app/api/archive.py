import hashlib
import io
import os
import shutil
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.types import Receive, Scope, Send

from app import request_context, source_roots, system_settings
from app.api.auth import get_current_active_user
from app.archives import (
    ArchiveMember,
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
    is_truncated,
    max_fetch_bytes,
)
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.crypto import follow_head_key
from app.db import get_session
from app.limits import Limits, Slot, current_limits, get_limiter
from app.logging_config import get_logger
from app.models import Protocol, Rule, RuleType, Source
from app.name_index import MAX_DEPTH
from app.redaction import (
    HEAD_BYTES,
    CompiledRules,
    RedactionRuleError,
    UnsupportedEncodingError,
    compile_rules,
    detect_encoding,
    head_digest,
    long_line_label,
    marker,
    redact_stream,
    redact_window,
)
from app.redaction_rules import effective_redaction_rules, engine_rules
from app.rules import (
    is_dir_visible,
    is_entry_visible,
    is_explicitly_excluded,
    is_safe_relative_path,
    is_safe_windows_relative_path,
    is_visible,
    rules_case_insensitive,
)
from app.scratch import get_scratch_store, make_scratch_dir

router = APIRouter(prefix="/sources/{source_id}", tags=["archive"])


def readable_source(capability: Capability):
    """The source, when the user has `capability` on it and it's enabled.
    A disabled source keeps its configuration but is off: nothing is listed,
    opened, followed or downloaded from it (409), whatever the user's
    grants. Closing a tab still works, so its scratch is released."""
    allowed = require_capability(capability, get_current_active_user)

    def dependency(source: Source = Depends(allowed)) -> Source:
        if not source.enabled:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SOURCE_DISABLED)
        return source

    return dependency


SOURCE_DISABLED = "This source is disabled. An admin can turn it back on in its settings."
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
# The reasons recorded in the audit log, which only view_audit_log can read.
_AUDIT_REFUSAL_REASON = "archive_hidden_members"
# Redaction is on and a whole archive would carry its members unredacted.
_AUDIT_REDACTION_ARCHIVE_REASON = "redaction_whole_archive"
# Redaction is on and the content is in an encoding it can't redact (UTF-32).
_AUDIT_REDACTION_ENCODING_REASON = "redaction_unsupported_encoding"


class AuditedRefusal(HTTPException):
    """A refusal that /open, /follow and /download record in the audit log
    themselves (file.open_denied, file.download_denied), with `reason`,
    before passing it on."""

    reason: str = ""


class WholeArchiveRefused(AuditedRefusal):
    """403 for a whole archive that holds a file the rules hide, or that
    redaction can't serve (`reason`)."""

    def __init__(self, reason: str = _AUDIT_REFUSAL_REASON) -> None:
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "message": WHOLE_ARCHIVE_REFUSED_MESSAGE,
                "error_code": WHOLE_ARCHIVE_REFUSED_ERROR_CODE,
            },
        )
        self.reason = reason


REDACTION_ENCODING_MESSAGE = (
    "This file is in an encoding redaction doesn't support (UTF-32), so it can't be shown."
)
REDACTION_ENCODING_ERROR_CODE = "redaction_unsupported_encoding"


class RedactionEncodingRefused(AuditedRefusal):
    """409 for content redaction can't read (UTF-32): refused rather than
    served unredacted."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": REDACTION_ENCODING_MESSAGE,
                "error_code": REDACTION_ENCODING_ERROR_CODE,
            },
        )
        self.reason = _AUDIT_REDACTION_ENCODING_REASON


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


# Listings are capped (#347): at most `listing_max_entries` (a System
# setting) entries come back, folders first, then by name; these headers
# say when there were more.
TRUNCATED_HEADER = "X-Listing-Truncated"
LIMIT_HEADER = "X-Listing-Limit"


def _listing_max_entries(session: Session) -> int:
    return system_settings.get_int(session, "listing_max_entries")


def _capped(entries: list[BrowseEntry], limit: int, *, flat: bool = False):
    """The first `limit` entries: folders first, then by name; for an
    archive's flat member list (`flat`), by path, so a folder always comes
    before what's in it. Returns (entries, truncated). A listing within the
    limit keeps its order (the explorer sorts it)."""
    if flat:
        ordered = sorted(entries, key=lambda e: e.path)
    elif len(entries) <= limit:
        return entries, False
    else:
        ordered = sorted(entries, key=lambda e: (not e.is_dir, e.name.lower(), e.name))
    return ordered[:limit], len(ordered) > limit


def _set_listing_headers(response: Response, truncated: bool, limit: int) -> None:
    response.headers[LIMIT_HEADER] = str(limit)
    if truncated:
        response.headers[TRUNCATED_HEADER] = "1"


def _archive_flat_entries(
    source: Source, archive_path: str, members: list[ArchiveMember], rules: list[Rule]
) -> list[BrowseEntry]:
    """Every rule-visible member of an archive, folders included (named or
    implied by a deeper member), as one flat list (#347): the explorer
    builds the tree from it and keeps it for the open tab instead of
    fetching the archive again for every folder clicked inside it. Same
    rule filtering as the folder-by-folder listing."""
    folders: set[str] = set()
    files: dict[str, ArchiveMember] = {}
    for member in members:
        name = normalize_member_name(member.name)
        if not name:
            continue
        parts = name.split("/")
        for i in range(1, len(parts)):
            folders.add("/".join(parts[:i]))
        if member.is_dir:
            folders.add(name)
        else:
            files[name] = member
    out = [
        BrowseEntry(
            name=folder.rsplit("/", 1)[-1],
            path=f"{archive_path}/{folder}",
            is_dir=True,
            size=0,
            is_archive=False,
        )
        for folder in folders
        if _entry_visible(source, f"{archive_path}/{folder}", True, rules)
    ]
    out.extend(
        BrowseEntry(
            name=name.rsplit("/", 1)[-1],
            path=f"{archive_path}/{name}",
            is_dir=False,
            size=member.size,
            is_archive=False,
            mtime=member.mtime,
        )
        for name, member in files.items()
        if name not in folders and _entry_visible(source, f"{archive_path}/{name}", False, rules)
    )
    return out


@router.get("/browse", response_model=list[BrowseEntry])
def browse(
    response: Response,
    source: Source = Depends(readable_source(Capability.view)),
    path: str = "",
    # Set by the explorer's refresh (#272): re-listing a source the user
    # already has open isn't opening it again, so it isn't audited.
    refresh: bool = False,
    # For an archive (#347): every member at once, as a flat list, instead
    # of one folder inside it.
    all_members: bool = Query(default=False, alias="all"),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> list[BrowseEntry]:
    """A folder's entries, or an archive's members, live and through the
    rules. Capped at the `listing_max_entries` System setting (#347): the
    X-Listing-Truncated header says when there were more. Takes a
    connector slot (#344): 503 with Retry-After when none is free."""
    _require_safe_path(source, path)
    limit = _listing_max_entries(session)
    with get_limiter().connector(current_limits(session), source.id):
        entries, truncated = _browse(source, path, refresh, all_members, user, session, limit)
    if path and not refresh and system_settings.get_bool(session, "audit_folder_listings"):
        # A folder below the root (or inside an archive) that was listed:
        # what someone looked through, not only what they opened. The
        # "Audit folder listings" System setting, on by default. Like the
        # root, the explorer's periodic refresh isn't audited again.
        metadata: dict = {"path": path}
        if all_members:
            metadata["all_members"] = True
        record_audit_event(
            session,
            user_id=user.id,
            action="source.browse",
            target_type="source",
            target_id=source.id,
            metadata=metadata,
        )
        session.commit()
    _set_listing_headers(response, truncated, limit)
    return entries


def _browse(
    source: Source,
    path: str,
    refresh: bool,
    all_members: bool,
    user: User,
    session: Session,
    limit: int,
) -> tuple[list[BrowseEntry], bool]:
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
        if all_members:
            if member_dir:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="all is for an archive itself, not a folder inside it",
                )
            return _capped(
                _archive_flat_entries(source, archive_path, members, rules), limit, flat=True
            )
        children = tree_children(members, member_dir)
        return _capped(
            [
                BrowseEntry(
                    name=child.name.rsplit("/", 1)[-1],
                    path=f"{archive_path}/{child.name}",
                    is_dir=child.is_dir,
                    size=child.size,
                    is_archive=False,
                    mtime=child.mtime,
                )
                for child in children
                # Same filter as regular listings: files through the rule
                # chain, folders only when something below them could be
                # visible. A rule scoped to show just the archive itself
                # doesn't leak its members.
                if _entry_visible(source, f"{archive_path}/{child.name}", child.is_dir, rules)
            ],
            limit,
        )

    if all_members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="all is only for an archive"
        )
    if path and not _dir_visible(source, path, rules):
        # A folder the rules hide is not browsable at all -- otherwise its
        # sub-folder names could still be enumerated by calling the API.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    entries = connector.list_directory(source, rules, path)
    listed, truncated = _capped(
        [
            BrowseEntry(
                name=entry.name,
                path=entry.path,
                is_dir=entry.is_dir,
                size=entry.size,
                is_archive=(not entry.is_dir) and is_archive(entry.name),
                mtime=entry.mtime,
            )
            for entry in entries
        ],
        limit,
    )
    # The source may have cut the listing itself (an agent, #347).
    return listed, truncated or is_truncated(entries)


def redaction_rules_for(session: Session, source: Source) -> CompiledRules | None:
    """The compiled redaction rules a source's content goes through, or None
    when redaction doesn't apply to it (off, or no effective rule: see
    app.redaction_rules.redaction_applies). The same for every user, super
    admins included. A stored rule the engine can't run fails the request
    rather than letting the content out unredacted."""
    if not system_settings.get_bool(session, "redaction_enabled"):
        return None
    rules = effective_redaction_rules(session, source.id)
    if not rules:
        return None
    try:
        return compile_rules(engine_rules(rules))
    except RedactionRuleError as exc:
        logger.error("redaction.rules_invalid", source_id=source.id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The redaction rules can't be applied. An administrator needs to check them.",
        ) from exc


@dataclass(frozen=True)
class RedactedRead:
    """What redacting a file's content for the viewer gave: the count per
    rule, how many bytes of the original the content covers (where the Tail
    goes on from), how many bytes at the end of the content stand for an
    unfinished last line, and the original's first HEAD_BYTES (to identify
    the file for the Tail)."""

    counts: dict[str, int]
    source_offset: int
    trailing_partial: int
    head: bytes

    @property
    def count(self) -> int:
        return sum(self.counts.values())


class _Resolved(NamedTuple):
    path: Path
    filename: str
    scratch_key: str | None
    redacted: RedactedRead | None = None


def _redact_file(original: Path, destination: Path, rules: CompiledRules, final: bool):
    """Redacts `original` into `destination`, in the encoding detect_encoding
    finds in its first bytes, for the viewer (UTF-8 out)."""
    with original.open("rb") as src:
        head = src.read(HEAD_BYTES)
        try:
            encoding = detect_encoding(head)
        except UnsupportedEncodingError as exc:
            raise RedactionEncodingRefused() from exc
        src.seek(0)
        with destination.open("wb") as dst:
            result = redact_stream(src, dst, rules, final=final, source_encoding=encoding)
    return RedactedRead(result.counts, result.source_offset, result.trailing_partial, head)


def _resolve_redacted(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    redaction: CompiledRules,
) -> _Resolved:
    """_resolve_content when redaction applies: the content is fetched (or,
    for a local plain file, read in place), redacted into scratch, and the
    fetched original deleted before the response goes out, so scratch only
    ever keeps the redacted copy. A whole archive (or an archive inside
    one) would carry its members unredacted, so it is refused (403, the
    same answer as an archive hiding files). A .gz file or an archive
    member is complete content, so its last line is redacted as it is; a
    plain file may still be written to, so an unfinished last line becomes
    `[redacted: incomplete line]` and the Tail reads it again."""
    filename = path.rsplit("/", 1)[-1]
    if member is not None:
        member = normalize_member_name(member)
        if not member or not _visible(source, f"{path}/{member}", rules):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        whole_archive = is_archive(member.rsplit("/", 1)[-1])
    else:
        whole_archive = is_archive(filename)
    if whole_archive:
        logger.warning("archive.redaction_whole_archive_refused", source_id=source.id, path=path)
        raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON)

    complete = member is not None or is_transparent_gzip(filename)
    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        key = _scratch_key(source.id, path, member)
        destination = store.acquire(key)
        fetched = destination.with_name(f"{destination.name}.source")
        try:
            if source.protocol == Protocol.local and not complete:
                original = _local_plain_path(source, path)
                served_filename = filename
            else:
                served_filename = _materialize(source, path, member, rules, fetched)
                original = fetched
            redacted = _redact_file(original, destination, redaction, final=complete)
        except FetchTooLargeError as exc:
            store.release(key)
            raise _fetch_too_large(exc) from exc
        except BaseException:
            store.release(key)
            raise
        finally:
            fetched.unlink(missing_ok=True)
    return _Resolved(destination, served_filename, key, redacted)


def _local_plain_path(source: Source, path: str) -> Path:
    _require_local_source_allowed(source)
    if source_roots.is_multi_root(source):
        rooted, inner = source_roots.split(source, path)
        return local_collector.resolve_path(rooted, inner)
    return local_collector.resolve_path(source, path)


def _resolve_content(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    redaction: CompiledRules | None = None,
) -> _Resolved:
    """Returns (path_to_serve, filename, scratch_key, redacted). With
    `redaction`, the content is redacted (_resolve_redacted). scratch_key is None
    when nothing was fetched/copied — currently only a local, plain file
    (not an archive member, not transparent-gzip), served straight from its
    real path with no ephemeral scratch involved at all (CLAUDE.md's Built-
    in log viewer note: "a file already on the same machine ... is
    inherently zero-latency and always fresh"). Everything else (remote
    protocols always; local .gz/archive-member, since those produce derived
    bytes that must live somewhere) goes through the scratch store.

    A fetch takes a connector slot while it runs (#344, 503 when none is
    free) and is refused up front (507) when scratch or its disk is full
    (#345)."""
    if not path:
        # Browsing the root is "", but opening or downloading needs a file.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    if not _visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if redaction is not None:
        return _resolve_redacted(source, path, member, rules, limits, redaction)

    filename = path.rsplit("/", 1)[-1]
    needs_scratch = (
        source.protocol != Protocol.local or member is not None or is_transparent_gzip(filename)
    )

    if not needs_scratch:
        resolved = _local_plain_path(source, path)
        if is_archive(filename):
            _require_whole_archive_allowed(source, path, resolved, rules)
        return _Resolved(resolved, filename, None)

    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        key = _scratch_key(source.id, path, member)
        destination = store.acquire(key)
        try:
            served_filename = _materialize(source, path, member, rules, destination)
        except FetchTooLargeError as exc:
            store.release(key)
            raise _fetch_too_large(exc) from exc
        except BaseException:
            store.release(key)
            raise
    return _Resolved(destination, served_filename, key)


def _audit_refusal(
    session: Session,
    user: User,
    source: Source,
    action: str,
    path: str,
    reason: str,
    **extra,
) -> None:
    record_audit_event(
        session,
        user_id=user.id,
        action=action,
        target_type="source",
        target_id=source.id,
        metadata={"path": path, "reason": reason, **extra},
    )
    session.commit()
    # Audited here, so the central access.denied handler (app/api/errors.py)
    # doesn't write it again.
    context = request_context.current()
    if context is not None:
        context.denial_handled = True


def _resolve_content_audited(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    user: User,
    session: Session,
    denied_action: str,
    redaction: CompiledRules | None = None,
) -> _Resolved:
    """_resolve_content, recording a refused whole archive (or content
    redaction can't read) in the audit log (`denied_action`) before the
    refusal goes out, so every attempt leaves a trace. For an archive
    inside an archive (`member=`), the path recorded is the refused
    archive's virtual path, `<archive>/<member>`."""
    try:
        return _resolve_content(source, path, member, rules, limits, redaction)
    except AuditedRefusal as exc:
        refused_path = path if member is None else f"{path}/{normalize_member_name(member)}"
        _audit_refusal(session, user, source, denied_action, refused_path, exc.reason)
        raise


def _redaction_metadata(redacted: RedactedRead | None) -> dict:
    """What the audit entry of a read says about redaction: nothing when it
    didn't apply."""
    if redacted is None:
        return {}
    return {"redaction": "applied", "redaction_at": "server"}


def _redaction_headers(redacted: RedactedRead) -> dict[str, str]:
    """X-Redacted, X-Redaction-Count (values replaced), X-Source-Offset (bytes
    of the original the content covers: where the Tail goes on from) and
    X-Follow-Partial (bytes at the end of the content that stand for an
    unfinished last line, which the next Tail answer sends again)."""
    return {
        "X-Redacted": "1",
        "X-Redaction-Count": str(redacted.count),
        "X-Source-Offset": str(redacted.source_offset),
        "X-Follow-Partial": str(redacted.trailing_partial),
    }


class _FinishingMixin:
    """Runs `on_finish` once the response is over, whichever way: sent in
    full, cut short by a client that went away, or cancelled. A background
    task isn't enough: it's skipped when the client disconnects, and a slot
    (#344) or a scratch file held by then would never be released."""

    _on_finish: Callable[[], None]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)  # type: ignore[misc]
        finally:
            self._on_finish()


class _SnapshotFileResponse(FileResponse):
    """A FileResponse that sends exactly the bytes the file had when it was
    stat'ed, which is what its Content-Length says. A local file is served
    in place (the system log source, a local source), and a log that grows
    while it's being sent would otherwise be read to its new end, more than
    the Content-Length, and the server aborts the response half way."""

    def __init__(self, path: Path, **kwargs):
        stat_result = os.stat(path)
        super().__init__(path, stat_result=stat_result, **kwargs)
        self._snapshot_size = stat_result.st_size

    async def _handle_simple(self, send: Send, send_header_only: bool, send_pathsend: bool) -> None:
        # pathsend would hand the server the whole path, growth included.
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        remaining = self._snapshot_size
        file = await anyio.open_file(self.path, mode="rb")
        try:
            more_body = remaining > 0
            if not more_body:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            while more_body:
                chunk = await file.read(min(self.chunk_size, remaining))
                remaining -= len(chunk)
                more_body = remaining > 0 and bool(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})
        finally:
            with anyio.CancelScope(shield=True):
                await file.aclose()


class _FinishingFileResponse(_FinishingMixin, _SnapshotFileResponse):
    def __init__(self, *args, on_finish: Callable[[], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_finish = on_finish


class _FinishingStreamingResponse(_FinishingMixin, StreamingResponse):
    def __init__(self, *args, on_finish: Callable[[], None], **kwargs):
        super().__init__(*args, **kwargs)
        self._on_finish = on_finish


@router.get("/open")
def open_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(readable_source(Capability.view)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """Fetches into scratch and holds it (refcounted) until a matching
    /close call — for a persistent viewer session, not a one-shot download.
    (Local plain files skip scratch entirely — see _resolve_content.)

    Viewing necessarily hands the client the file's bytes, so `view` can
    read a whole file; `download` gates bulk export (/download, the folder
    ZIP). Every open is audited so reads are as traceable as downloads.

    When redaction applies to the source (#382, #249), the content is
    redacted on the server before it is stored in scratch or sent, for
    every user, and the redaction headers (_redaction_headers) say so; the
    viewer then follows from X-Source-Offset, not from the length of what
    it received."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    redaction = redaction_rules_for(session, source)
    served_path, filename, key, redacted = _resolve_content_audited(
        source,
        path,
        member,
        rules,
        current_limits(session),
        user,
        session,
        "file.open_denied",
        redaction,
    )
    metadata: dict = {"path": path, "member": member} if member else {"path": path}
    record_audit_event(
        session,
        user_id=user.id,
        action="file.open",
        target_type="source",
        target_id=source.id,
        metadata={**metadata, **_redaction_metadata(redacted)},
    )
    session.commit()
    headers = {"X-Scratch-Key": key} if key else {}
    followable = member is None and not is_transparent_gzip(path.rsplit("/", 1)[-1])
    if redacted is not None:
        headers.update(_redaction_headers(redacted))
        if followable:
            # Over the original's start, up to where the Tail goes on from.
            headers["X-Follow-Head"] = _head_hash(source.id, redacted.head, redacted.source_offset)
    elif followable:
        # What the first Follow poll compares against (#254), so a file
        # rotated between this open and that poll is caught too.
        size = served_path.stat().st_size
        with served_path.open("rb") as served:
            headers["X-Follow-Head"] = _head_hash(source.id, served.read(FOLLOW_HEAD_BYTES), size)
    # Inline, not an attachment: this is what the viewer reads to display a
    # file, not a download endpoint (downloads go through /download, gated
    # by the `download` capability -- #174). nosniff (set app-wide) keeps
    # a browser from rendering it as HTML.
    return _SnapshotFileResponse(
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


def _head_hash(source_id: int, start: bytes, offset: int) -> str:
    """Identifies the file by the bytes the viewer already has from its
    start: the first min(FOLLOW_HEAD_BYTES, offset), as an HMAC with the
    source's key (app.redaction.head_digest), so a short, guessable start
    of a file can't be confirmed from the header."""
    return head_digest(follow_head_key(source_id), start, offset)


# Redacted Tail (#382, #249): how much is read before the offset, only to
# know whether it falls inside a private-key block. Never sent.
FOLLOW_CONTEXT_BYTES = 16 * 1024
# The most a redacted poll reads after the offset; a line with no end
# within it is replaced by a marker and skipped.
FOLLOW_WINDOW_BYTES = RANGE_MAX_BYTES - FOLLOW_CONTEXT_BYTES


@router.get("/follow")
def follow_file(
    path: str,
    # Below 2**62: an agent reads it as an int64.
    offset: int = Query(ge=0, lt=2**62),
    head: str | None = None,
    start: bool = False,
    source: Source = Depends(readable_source(Capability.view)),
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
    without a record; not on every poll.

    When redaction applies to the source (#382, #249), see _follow_redacted:
    only redacted whole lines come back, offsets stay in bytes of the
    original, and X-Follow-Aligned / X-Follow-Partial tell the viewer how
    the answer starts and ends."""
    _require_safe_path(source, path)
    if not path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    rules = _rules_for(session, source.id)
    if not _visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    filename = path.rsplit("/", 1)[-1]
    if is_transparent_gzip(filename) or is_archive(filename):
        raise _follow_unsupported("Compressed files and archives can only be reloaded whole")

    redaction = redaction_rules_for(session, source)
    connector = _connector(source)
    # Redacted, the read starts a little before the offset (context only).
    read_from = max(0, offset - FOLLOW_CONTEXT_BYTES) if redaction is not None else offset
    try:
        # One round trip: the size, the start of the file and the new bytes.
        size, first, data = connector.read_range(
            source, path, read_from, RANGE_MAX_BYTES, FOLLOW_HEAD_BYTES
        )
    except RangeNotSupportedError as exc:
        raise _follow_unsupported(
            "This source's agent is older than 0.6 and can't send only the new lines"
        ) from exc

    changed = head is not None and offset > 0 and head != _head_hash(source.id, first, offset)
    if offset > size or changed:
        return Response(
            content=b"",
            media_type="application/octet-stream",
            headers={"X-Follow-Reset": "1", "X-File-Size": str(size)},
        )

    if redaction is not None:
        try:
            encoding = detect_encoding(first)
        except UnsupportedEncodingError as exc:
            _audit_refusal(
                session,
                user,
                source,
                "file.open_denied",
                path,
                _AUDIT_REDACTION_ENCODING_REASON,
                follow=True,
            )
            raise RedactionEncodingRefused() from exc
        body, headers = _follow_redacted(
            source, redaction, encoding, offset, read_from, size, first, data
        )
    else:
        next_offset = offset + len(data)
        # What the viewer holds from the start once it appends `data`: the
        # head as read, extended by the new bytes while the file is still
        # short.
        start_bytes = first[:offset] + data if offset < FOLLOW_HEAD_BYTES else first
        body = data
        headers = {
            "X-File-Size": str(size),
            "X-Follow-Offset": str(next_offset),
            "X-Follow-Head": _head_hash(source.id, start_bytes, next_offset),
        }
        if next_offset < size:
            headers["X-Follow-More"] = "1"

    if start or offset == 0:
        record_audit_event(
            session,
            user_id=user.id,
            action="file.follow",
            target_type="source",
            target_id=source.id,
            metadata={
                "path": path,
                **({"redaction": "applied", "redaction_at": "server"} if redaction else {}),
            },
        )
        session.commit()
    return Response(content=body, media_type="application/octet-stream", headers=headers)


def _follow_redacted(
    source: Source,
    redaction: CompiledRules,
    encoding: str | None,
    offset: int,
    read_from: int,
    size: int,
    first: bytes,
    data: bytes,
) -> tuple[bytes, dict[str, str]]:
    """A redacted Tail answer. `data` was read from `read_from`, up to
    FOLLOW_CONTEXT_BYTES before `offset`; that context is never sent, only
    read to know whether `offset` falls inside a private-key block.

    - Only whole lines are redacted and sent. When `offset` isn't the start
      of a line (opening only the end of a file), the answer starts at the
      next line and says X-Follow-Aligned: 1, so a value the offset cuts is
      never shown in part.
    - An unfinished last line is sent as `[redacted: incomplete line]`;
      X-Follow-Partial is its length in the answer and X-Follow-Offset its
      start, so the next poll sends it again, whole once it is.
    - A line with no end within FOLLOW_WINDOW_BYTES can't be redacted whole:
      it becomes a marker and the Tail goes on after it.
    - Offsets, X-File-Size and X-Follow-Head are about the original."""
    window = redact_window(
        data,
        min(offset - read_from, len(data)),
        redaction,
        base_offset=read_from,
        final=False,
        source_encoding=encoding,
    )
    body = window.data
    counts = window.counts
    partial = window.trailing_partial
    read_end = read_from + len(data)
    next_offset = read_from + window.end
    if next_offset <= offset and read_end < size:
        # No line ends within what one poll can read: skip it, fail closed.
        body = marker(long_line_label(FOLLOW_WINDOW_BYTES)) + b"\n"
        counts = {}
        partial = 0
        next_offset = read_end
    headers = {
        "X-File-Size": str(size),
        "X-Follow-Offset": str(next_offset),
        "X-Follow-Head": _head_hash(source.id, first, next_offset),
        "X-Redacted": "1",
        "X-Redaction-Count": str(sum(counts.values())),
        "X-Follow-Partial": str(partial),
    }
    if window.aligned:
        headers["X-Follow-Aligned"] = "1"
    # More is waiting only when something wasn't read yet: an unfinished
    # last line is read again on the next poll, not right away.
    if read_end < size:
        headers["X-Follow-More"] = "1"
    return body, headers


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
    source: Source = Depends(readable_source(Capability.download)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> FileResponse:
    """One-shot: fetches, streams, and releases within the same request —
    no persistent session, so no /close call is needed afterward.

    Counts as one of the user's transfers (#344) until the client has the
    whole file or goes away: 503 with Retry-After when the user already has
    as many downloads and folder ZIPs in progress as allowed."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    limits = current_limits(session)
    transfer = get_limiter().transfer(limits, user.id)
    try:
        served_path, filename, key, _ = _resolve_content_audited(
            source, path, member, rules, limits, user, session, "file.download_denied"
        )
    except BaseException:
        transfer.release()
        raise
    store = get_scratch_store()

    def finish() -> None:
        transfer.release()
        if key:
            store.release(key)

    try:
        # CLAUDE.md's Application logging section names file download as
        # one of the minimum AuditLog-worthy actions.
        record_audit_event(
            session,
            user_id=user.id,
            action="file.download",
            target_type="source",
            target_id=source.id,
            metadata={"path": path, "member": member} if member else {"path": path},
        )
        session.commit()
    except BaseException:
        finish()
        raise
    return _FinishingFileResponse(served_path, filename=filename, on_finish=finish)


ZIP_LISTING_TRUNCATED_DETAIL = (
    "This folder has more entries than a ZIP can list; narrow it to a subfolder"
)
ZIP_TOO_DEEP_DETAIL = "This folder is nested too deeply for a ZIP; narrow it to a subfolder"


class _ZipListingIncomplete(Exception):
    """The walk can't list every file under the folder, so the ZIP would
    miss some without saying so: it's refused before it starts."""

    def __init__(self, path: str, reason: str, detail: str):
        super().__init__(path)
        self.path = path
        self.reason = reason
        self.detail = detail


def _zip_entries(
    connector,
    source: Source,
    rules: list[Rule],
    current_dir: str,
    depth: int = 0,
    visited: set[str] | None = None,
) -> list[DirEntry]:
    """Every rule-visible file under `current_dir`, from listings alone --
    nothing is fetched yet, so the folder's listed total can be checked
    against MAX_FETCH_MB before the first download (#191). Nested
    .zip/.tar.gz files are included as raw files rather than expanded --
    expanding archives-within-a-bulk-zip isn't worth the complexity for
    what CLAUDE.md asks for here (download a folder). An archive holding a
    file the rules hide is left out once fetched (see _check_archives).

    Like the file-name index, the walk stops MAX_DEPTH folders down and
    lists a folder only once, by its identity on the source when the
    connector knows it (#347): a symlink loop over SSH ends instead of
    logging in again for every level. A folder it would have to leave out
    (below MAX_DEPTH, or a listing the source cut) raises
    _ZipListingIncomplete, so a ZIP is never missing files unannounced."""
    visited = set() if visited is None else visited
    out: list[DirEntry] = []
    entries = connector.list_directory(source, rules, current_dir)
    if is_truncated(entries):
        # The source cut the listing (an agent's limit).
        logger.warning("archive.zip_listing_truncated", source_id=source.id, path=current_dir)
        raise _ZipListingIncomplete(current_dir, "listing_truncated", ZIP_LISTING_TRUNCATED_DETAIL)
    for entry in entries:
        if entry.is_dir:
            if entry.real_key is not None:
                if entry.real_key in visited:
                    logger.info("archive.zip_folder_seen", source_id=source.id, path=entry.path)
                    continue
                visited.add(entry.real_key)
            if depth + 1 >= MAX_DEPTH:
                logger.warning("archive.zip_too_deep", source_id=source.id, path=entry.path)
                raise _ZipListingIncomplete(entry.path, "too_deep", ZIP_TOO_DEEP_DETAIL)
            out.extend(_zip_entries(connector, source, rules, entry.path, depth + 1, visited))
        elif _visible(source, entry.path, rules):
            out.append(entry)
    return out


# How much compressed output a streamed folder ZIP gathers before handing
# it to the client, and how much of a member it reads at a time.
_ZIP_FLUSH_BYTES = 1024 * 1024
_ZIP_READ_BYTES = 1024 * 1024


class _ZipSink(io.RawIOBase):
    """Where zipfile writes a streamed ZIP: an unseekable buffer that the
    response drains as it fills, so zipfile uses data descriptors and the
    ZIP is never held whole, on disk or in memory (#345)."""

    def __init__(self):
        super().__init__()
        self._chunks: list[bytes] = []
        self.pending = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        chunk = bytes(data)
        self._chunks.append(chunk)
        self.pending += len(chunk)
        return len(chunk)

    def take(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        self.pending = 0
        return data


def _arcname(path: str, request_root: str) -> str:
    return path[len(request_root) + 1 :] if request_root else path


def _check_archives(
    connector,
    source: Source,
    rules: list[Rule],
    entries: list[DirEntry],
    request_root: str,
    tmp_dir: Path,
) -> tuple[list[tuple[DirEntry, Path | None]], list[str]]:
    """Before a folder's ZIP starts: fetches each archive among `entries`
    into `tmp_dir` and leaves out any that holds a file the rules hide
    (_archive_has_hidden_members), so the audit entry can name them. The
    copy checked is the copy zipped -- each archive is read from the source
    once. Returns the entries to zip, each with its already fetched copy
    (None for a file fetched while streaming), and the arcnames of the
    archives left out."""
    planned: list[tuple[DirEntry, Path | None]] = []
    skipped: list[str] = []
    for index, entry in enumerate(entries):
        if not is_archive(entry.name):
            planned.append((entry, None))
            continue
        destination = tmp_dir / f"archive-{index}"
        connector.fetch_file(source, entry.path, destination)
        if _archive_has_hidden_members(source, entry.path, destination, rules):
            destination.unlink()
            skipped.append(_arcname(entry.path, request_root))
            continue
        planned.append((entry, destination))
    return planned, skipped


def _skipped_list_name(entries: list[DirEntry], request_root: str) -> str:
    """SKIPPED.txt, unless the folder already has a file by that name at its
    root (compared ignoring case, so extracting on Windows can't collide)."""
    root_names = {_arcname(entry.path, request_root).lower() for entry in entries}
    return "SKIPPED (PerchTail).txt" if "skipped.txt" in root_names else "SKIPPED.txt"


def _stream_zip(
    connector,
    source: Source,
    planned: list[tuple[DirEntry, Path | None]],
    request_root: str,
    tmp_dir: Path,
    limit: int,
    skipped: list[str],
    skipped_name: str,
) -> Iterator[bytes]:
    """Fetches each of `planned` fresh, one at a time, into scratch, and
    streams it into the ZIP the client is receiving -- same always-fresh
    rule as every other read in this project, just batched into one
    archive. An archive was already fetched and checked (_check_archives):
    its copy is the one zipped. Arcnames are relative to `request_root`.
    The fetched size is what counts against the MAX_FETCH_MB budget: a file
    may have grown since it was listed, and one that takes the ZIP over it
    ends the download. The archives left out (`skipped`) are listed last,
    in `skipped_name`.

    Once the response has started, a failure can only cut the download
    short (the client sees an incomplete download); it's logged."""
    sink = _ZipSink()
    remaining = limit
    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, (entry, fetched) in enumerate(planned):
                arcname = _arcname(entry.path, request_root)
                destination = fetched if fetched is not None else tmp_dir / f"member-{index}"
                try:
                    if fetched is None:
                        connector.fetch_file(source, entry.path, destination)
                    remaining -= destination.stat().st_size
                    if remaining < 0:
                        raise FetchTooLargeError("folder is over the MAX_FETCH_MB limit")
                    info = zipfile.ZipInfo.from_file(destination, arcname=arcname)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    with destination.open("rb") as src, zf.open(info, "w") as dst:
                        while chunk := src.read(_ZIP_READ_BYTES):
                            dst.write(chunk)
                            if sink.pending >= _ZIP_FLUSH_BYTES:
                                yield sink.take()
                finally:
                    destination.unlink(missing_ok=True)
                if sink.pending:
                    yield sink.take()
            if skipped:
                zf.writestr(
                    skipped_name, "".join(f"{arcname}: {_SKIPPED_REASON}\n" for arcname in skipped)
                )
        # Closing the ZipFile wrote the central directory.
        yield sink.take()
    except Exception as exc:
        logger.warning(
            "archive.zip_stream_failed",
            source_id=source.id,
            path=request_root,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _attachment(filename: str) -> str:
    """A Content-Disposition for `filename`, written the way FileResponse
    writes it."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


@router.get("/download-zip")
def download_folder_zip(
    path: str = "",
    source: Source = Depends(readable_source(Capability.download)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """Zips an entire folder for download (CLAUDE.md's Viewer section:
    download "single file or a zipped folder"), fetching each contained
    file fresh — no different from any other read here, just batched.

    The ZIP is streamed as it's built (#345): each file is fetched into
    scratch, written to the response and deleted before the next; the
    archives among them are fetched and checked first (_check_archives),
    and one that holds a file the rules hide is left out and listed in
    SKIPPED.txt. The folder's listed total is checked against MAX_FETCH_MB before anything
    is fetched (413). Holds one of the user's transfers and a connector
    slot until the client has it all or goes away (#344, 503 with
    Retry-After when none is free)."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    if path and not _dir_visible(source, path, rules):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    connector = _connector(source)

    limits = current_limits(session)
    slots: list[Slot] = [get_limiter().transfer(limits, user.id)]

    def finish() -> None:
        for slot in slots:
            slot.release()

    tmp_dir: Path | None = None
    try:
        slots.append(get_limiter().connector(limits, source.id))
        limit = max_fetch_bytes()
        try:
            entries = _zip_entries(connector, source, rules, path)
        except _ZipListingIncomplete as exc:
            folder_key = "truncated_folder" if exc.reason == "listing_truncated" else "folder"
            record_audit_event(
                session,
                user_id=user.id,
                action="file.download_denied",
                target_type="source",
                target_id=source.id,
                metadata={"path": path, "reason": exc.reason, folder_key: exc.path},
            )
            session.commit()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
        listed = sum(entry.size for entry in entries)
        if listed > limit:
            raise _fetch_too_large(
                FetchTooLargeError(
                    "folder is over the MAX_FETCH_MB limit", size=listed, limit=limit
                ),
                folder=True,
            )
        # The archives are fetched and checked before the ZIP starts and wait
        # in scratch for their turn; the other files sit there one at a time.
        archives = sum(entry.size for entry in entries if is_archive(entry.name))
        largest_other = max(
            (entry.size for entry in entries if not is_archive(entry.name)), default=0
        )
        get_scratch_store().ensure_room(archives + largest_other)
        tmp_dir = make_scratch_dir("perchtail-zip-")
        try:
            planned, skipped = _check_archives(connector, source, rules, entries, path, tmp_dir)
        except FetchTooLargeError as exc:
            raise _fetch_too_large(exc, folder=True) from exc
        fetched = sum(copy.stat().st_size for _, copy in planned if copy is not None)
        if fetched > limit:
            raise _fetch_too_large(
                FetchTooLargeError(
                    "folder is over the MAX_FETCH_MB limit", size=fetched, limit=limit
                ),
                folder=True,
            )
        metadata: dict = {"path": path, "file_count": len(planned)}
        if skipped:
            metadata["skipped_archives"] = skipped
        record_audit_event(
            session,
            user_id=user.id,
            action="folder.download",
            target_type="source",
            target_id=source.id,
            metadata=metadata,
        )
        session.commit()
    except BaseException:
        finish()
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    stream = _stream_zip(
        connector,
        source,
        planned,
        path,
        tmp_dir,
        limit,
        skipped,
        _skipped_list_name(entries, path),
    )

    def finish_stream() -> None:
        finish()
        # Runs the generator's own cleanup now if the download was cut
        # short; a generator still running in its thread cleans up itself.
        try:
            stream.close()
        except ValueError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    folder_name = path.rsplit("/", 1)[-1] if path else source.name
    return _FinishingStreamingResponse(
        stream,
        media_type="application/zip",
        headers={"Content-Disposition": _attachment(f"{folder_name}.zip")},
        on_finish=finish_stream,
    )
