import gzip
import hashlib
import io
import mimetypes
import os
import shutil
import tarfile
import threading
import time
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NamedTuple
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlmodel import Session, select
from starlette.types import Receive, Scope, Send

from app import request_context, source_roots, system_settings
from app.agent_registry import get_agent_registry
from app.api.auth import get_current_active_user
from app.api.errors import PathNotFound
from app.archives import (
    SKIP_ENCRYPTED,
    SKIP_LINK,
    SKIP_NESTED_ARCHIVE,
    SKIP_UNSUPPORTED_COMPRESSION,
    ArchiveMember,
    DecompressionLimitError,
    MemberUnreadableError,
    decompress_gzip,
    extract_member,
    has_folder,
    is_archive,
    is_transparent_gzip,
    is_zip,
    normalize_member_name,
    rebuild_archive,
    scan_members,
    tree_children,
    write_archive,
)
from app.audit import record_audit_event
from app.auth.models import Capability, User
from app.auth.rbac import require_capability
from app.collectors import local as local_collector
from app.collectors.agent import (
    AGENT_UPDATE_REQUIRED,
    AgentRedactionError,
    AgentUpdateRequiredError,
    redaction_block,
    require_links,
)
from app.collectors.base import (
    LINK_TARGET_HIDDEN,
    NOT_A_FILE,
    RANGE_MAX_BYTES,
    DirEntry,
    FetchTooLargeError,
    PathOutsideBaseError,
    SourceArchiveUnreadableError,
    SourceError,
    SourceMemberUnreadableError,
    SourceNotAFileError,
    SourceNotFoundError,
    SourcePermissionError,
    SourceUnreachableError,
    is_truncated,
    max_fetch_bytes,
)
from app.collectors.errors import target_check_for
from app.collectors.ranged import RemoteFileChangedError, ranged_file
from app.collectors.registry import CONNECTORS
from app.config import get_settings
from app.crypto import follow_head_key
from app.db import get_session
from app.limits import Limits, Slot, current_limits, get_limiter
from app.logging_config import get_logger
from app.models import Protocol, Rule, RuleType, Source
from app.name_index import MAX_DEPTH
from app.redaction import (
    CHUNK_BYTES,
    HEAD_BYTES,
    CompiledRules,
    RedactionRuleError,
    Redactor,
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

# The routes that send a file's content: whatever they answer goes out with
# Cache-Control: no-store, so neither the browser nor a proxy in front keeps
# a copy after the viewer closes it. Set here, on the route, so every
# response those routes build (plain, redacted, streamed) carries it.
_FILE_CONTENT_ROUTES = {"open", "follow", "download", "download-zip"}


class _FileContentRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[object, object, Response]]:
        handler = super().get_route_handler()
        if self.path.rsplit("/", 1)[-1] not in _FILE_CONTENT_ROUTES:
            return handler

        async def no_store(request: Request) -> Response:
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store


router = APIRouter(prefix="/sources/{source_id}", tags=["archive"], route_class=_FileContentRoute)


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


# With a code of its own: the viewer offers no Try again and stops Tail, as
# for the other refusals that repeating doesn't resolve.
SOURCE_DISABLED_ERROR_CODE = "source_disabled"
SOURCE_DISABLED = {
    "message": "This source is disabled. An admin can turn it back on in its settings.",
    "error_code": SOURCE_DISABLED_ERROR_CODE,
}
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


def _scratch_owner(user: User) -> str:
    """Whose scratch entries a request reaches: the user's, in the session
    the request came with (app/request_context.py), so a viewer only ever
    closes what its own session opened. A request without a session (none
    reaches these endpoints through the app) is scoped to the user."""
    context = request_context.current()
    session_ref = context.session_id if context is not None else None
    return f"{user.id}/{session_ref or ''}"


def _scratch_key(owner: str, source_id: int, path: str, member: str | None) -> str:
    raw = f"{owner}\0{source_id}\0{path}\0{member or ''}"
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


# Redaction is on and the content is compressed in a format PerchTail doesn't
# decompress (bzip2, xz, zstd, 7z, rar, or gzip or zip in a file not named
# as one), so the values inside can't be redacted.
_AUDIT_REDACTION_FORMAT_REASON = "redaction_unsupported_format"
REDACTION_FORMAT_MESSAGE = (
    "This file is in a compressed format redaction doesn't support, so it can't be shown."
)
REDACTION_FORMAT_ERROR_CODE = "redaction_unsupported_format"


class RedactionFormatRefused(AuditedRefusal):
    """409 for content compressed in a format redaction can't read
    (unsupported_compressed_format): refused rather than served with the
    values inside it unredacted."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": REDACTION_FORMAT_MESSAGE,
                "error_code": REDACTION_FORMAT_ERROR_CODE,
            },
        )
        self.reason = _AUDIT_REDACTION_FORMAT_REASON


MEMBER_UNREADABLE_MESSAGE = (
    "This archive member is encrypted or compressed with a method PerchTail can't read, "
    "so it can't be opened."
)
MEMBER_UNREADABLE_ERROR_CODE = "archive_member_unreadable"
# Why an archive member couldn't be extracted, in the audit log.
_AUDIT_MEMBER_REASONS = {
    SKIP_ENCRYPTED: "member_encrypted",
    SKIP_UNSUPPORTED_COMPRESSION: "member_unsupported_compression",
}


class MemberUnreadableRefused(AuditedRefusal):
    """409 for an archive member that can't be extracted (encrypted, or
    compressed with a method zipfile can't read), with or without
    redaction."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": MEMBER_UNREADABLE_MESSAGE,
                "error_code": MEMBER_UNREADABLE_ERROR_CODE,
            },
        )
        self.reason = _AUDIT_MEMBER_REASONS.get(reason, "member_unreadable")


ARCHIVE_UNREADABLE_MESSAGE = (
    "This archive can't be read: it is damaged, or it isn't really a .zip or .tar.gz file."
)
GZIP_UNREADABLE_MESSAGE = (
    "This compressed file can't be read: it is damaged, or it isn't really a .gz file."
)
ARCHIVE_UNREADABLE_ERROR_CODE = "archive_unreadable"
_AUDIT_ARCHIVE_UNREADABLE_REASON = "archive_unreadable"


class ArchiveUnreadableRefused(AuditedRefusal):
    """409 for a .zip or .tar.gz that can't be read as one (corrupt, or
    really another kind of file), whether it is browsed or one of its
    members is opened, or for a .gz file that can't be decompressed
    (`gzip`), rather than a 500 (or, from an agent, a 502)."""

    def __init__(self, *, gzip: bool = False) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": GZIP_UNREADABLE_MESSAGE if gzip else ARCHIVE_UNREADABLE_MESSAGE,
                "error_code": ARCHIVE_UNREADABLE_ERROR_CODE,
            },
        )
        self.reason = _AUDIT_ARCHIVE_UNREADABLE_REASON


def _archive_unreadable(
    source: Source, path: str, exc: BaseException, *, gzip: bool = False
) -> ArchiveUnreadableRefused:
    """The refusal of an archive or .gz file that can't be read, logged as
    archive.unreadable with the error's type (never its text or content)."""
    logger.warning(
        "archive.unreadable", source_id=source.id, path=path, error_type=type(exc).__name__
    )
    return ArchiveUnreadableRefused(gzip=gzip)


@contextmanager
def _agent_unreadable(source: Source, path: str, member: str | None) -> Iterator[None]:
    """An agent that can't read the archive or .gz file it was asked to
    list or read from (SourceArchiveUnreadableError: the agent's
    archive_unreadable or gzip_unreadable code) gets the refusals the
    server gives when it reads them itself: ArchiveUnreadableRefused, or,
    for a .gz member it can't decompress to redact,
    RedactionUnreadableRefused (_decompress_gzip_member). A member the
    agent finds encrypted (SourceMemberUnreadableError, its member_encrypted
    code) gets MemberUnreadableRefused, as in _materialize. An older agent
    answers without a code: that stays the source's own error."""
    try:
        yield
    except SourceArchiveUnreadableError as exc:
        if exc.gzip and member is not None:
            raise RedactionUnreadableRefused() from exc
        raise _archive_unreadable(source, path, exc, gzip=exc.gzip) from exc
    except SourceMemberUnreadableError as exc:
        logger.warning(
            "archive.member_unreadable", source_id=source.id, path=path, reason=exc.reason
        )
        raise MemberUnreadableRefused(exc.reason) from exc


# The first bytes of the compressed formats PerchTail doesn't decompress.
# bzip2 is "BZh", the block size ('1' to '9'), then the magic of its first
# block or of the end of an empty stream. gzip and zip are decompressed only
# when the file name says so (.gz, .zip); by the time content reaches this
# check, that has already happened, so gzip or zip content here is in a
# file whose name doesn't say so (app.log.1 holding gzip) and is refused too.
_BZIP2_BLOCK_MAGICS = (b"\x31\x41\x59\x26\x53\x59", b"\x17\x72\x45\x38\x50\x90")
_UNSUPPORTED_COMPRESSED_MAGICS = (
    b"\xfd7zXZ\x00",  # xz
    b"\x28\xb5\x2f\xfd",  # zstd
    b"7z\xbc\xaf\x27\x1c",  # 7z
    b"Rar!\x1a\x07",  # rar (1.5 and 5)
    b"\x1f\x8b",  # gzip
    b"PK\x03\x04",  # zip
    b"PK\x05\x06",  # zip with no members
    b"PK\x07\x08",  # zip split or spanned
)


def unsupported_compressed_format(head: bytes) -> bool:
    """Whether content starting with `head` is compressed in a format
    PerchTail doesn't decompress there (bzip2, xz, zstd, 7z, rar, and gzip
    or zip in a file not named as one), told by its first bytes rather than
    by its file name."""
    if (
        head[:3] == b"BZh"
        and len(head) >= 10
        and head[3:4] in (b"1", b"2", b"3", b"4", b"5", b"6", b"7", b"8", b"9")
        and head[4:10] in _BZIP2_BLOCK_MAGICS
    ):
        return True
    return head.startswith(_UNSUPPORTED_COMPRESSED_MAGICS)


# Redaction is on and a compressed file inside an archive can't be read, so
# it can't be redacted.
_AUDIT_REDACTION_UNREADABLE_REASON = "redaction_unreadable"


class RedactionUnreadableRefused(AuditedRefusal):
    """409 for a compressed file a redacted download can't read: refused
    rather than sent unredacted."""

    def __init__(self) -> None:
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "This compressed file can't be read, so it can't be redacted.",
                "error_code": _AUDIT_REDACTION_UNREADABLE_REASON,
            },
        )
        self.reason = _AUDIT_REDACTION_UNREADABLE_REASON


def _archive_has_hidden_members(
    source: Source, path: str, local_archive: Path, rules: list[Rule]
) -> bool:
    """Whether the archive at `path` (already copied to `local_archive`)
    holds a file the rules hide on purpose: the last rule matching its
    virtual path (`<archive>/<member>`, the same path the explorer and
    /open check) is an exclude. Members no rule matches don't count -- a
    rule that shows only the archive still lets it be downloaded whole. An
    archive that can't be read as one, whose listing was cut short
    (app.archives.scan_members), or that holds another archive (whose
    own members the rules are never checked against), counts as hiding
    something whenever the source has an exclude rule. Every entry counts,
    a tar's links, devices and FIFOs included: they are never listed or
    opened, but the whole archive still carries their names."""
    has_exclude = any(rule.type == RuleType.exclude for rule in rules)
    filename = path.rsplit("/", 1)[-1]
    try:
        members, cut = scan_members(local_archive, filename, include_special=True)
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
    # Entries past a cut listing were never checked: as with an archive that
    # can't be read.
    return cut and has_exclude


def _require_whole_archive_allowed(
    source: Source, path: str, local_archive: Path, rules: list[Rule]
) -> None:
    """A whole archive carries every member's bytes, so it is only served
    when none of them is hidden by the rules (_archive_has_hidden_members);
    its visible files stay reachable one by one through `member=`."""
    if _archive_has_hidden_members(source, path, local_archive, rules):
        logger.warning("archive.whole_archive_refused", source_id=source.id, path=path)
        raise WholeArchiveRefused()


def _reads_in_place(connector, filename: str) -> bool:
    """Whether the archive `filename` is read in place, with ranged reads
    (collectors/ranged.py), instead of being copied whole to scratch: a
    .zip on a connector that can (RANGED_READS: SSH, SMB and WinRM). Its
    listing reads the archive's end, a member its own header and data."""
    return is_zip(filename) and bool(getattr(connector, "RANGED_READS", False))


@contextmanager
def _zip_in_place(
    connector, source: Source, path: str, rules: list[Rule], operation: str
) -> Iterator[BinaryIO]:
    """The .zip at `path` as a seekable file read in place, for one
    operation ("list" or "member"): opened and checked as a fetch is, and
    allowed to transfer at most MAX_FETCH_MB."""
    with (
        connector.open_ranged(source, path, rules=rules) as remote,
        ranged_file(
            remote, limit=max_fetch_bytes(), source_id=source.id, operation=operation
        ) as archive_file,
    ):
        yield archive_file


def _materialize(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    destination: Path,
    *,
    check_whole_archive: bool = True,
) -> str:
    """Fetches `path` (or `member` inside it, if `path` is an archive) into
    `destination`, applying transparent .gz decompression. Returns the
    filename to present to the client. Every call is a fresh fetch — see
    CLAUDE.md's "Live browsing & ephemeral fetch behavior".

    A whole archive (or an archive member that is one) is only fetched when
    none of its members is hidden (_require_whole_archive_allowed), unless
    `check_whole_archive` is False: a redacted download rebuilds it with
    only the members the rules don't hide (_rebuild_redacted)."""
    if not _visible(source, path, rules):
        # Rules gate which files are reachable independent of RBAC — a user
        # may have view/download on the source overall but not on this
        # specific path.
        raise PathNotFound()

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
            raise PathNotFound()
        try:
            if _lists_on_agent(source):
                # Extracted on the agent's host, as it is stored: only the
                # member travels, never the whole archive.
                with _agent_unreadable(source, path, None):
                    connector.call("fetch_member", source, path, member, destination, rules=rules)
            elif _reads_in_place(connector, filename):
                with _zip_in_place(connector, source, path, rules, "member") as archive_file:
                    extract_member(
                        archive_file, filename, member, destination, _max_decompressed_bytes()
                    )
            else:
                with connector.local_copy(source, path, rules=rules) as archive_local:
                    extract_member(
                        archive_local, filename, member, destination, _max_decompressed_bytes()
                    )
            if check_whole_archive and is_archive(member.rsplit("/", 1)[-1]):
                # A member that is itself an archive is served whole, so it
                # gets the same check as any whole archive, at its virtual
                # path.
                _require_whole_archive_allowed(source, f"{path}/{member}", destination, rules)
        except DecompressionLimitError as exc:
            raise _too_large(exc) from exc
        except MemberUnreadableError as exc:
            logger.warning(
                "archive.member_unreadable", source_id=source.id, path=path, reason=exc.reason
            )
            raise MemberUnreadableRefused(exc.reason) from exc
        except _UNREADABLE_ERRORS as exc:
            raise _archive_unreadable(source, path, exc) from exc
        except KeyError as exc:
            # Not in the archive, or not a regular file (a link, a device):
            # either way there's nothing to open under that name.
            raise PathNotFound() from exc
        return member.rsplit("/", 1)[-1]

    if is_transparent_gzip(filename):
        try:
            with connector.local_copy(source, path, rules=rules) as raw_local:
                decompress_gzip(raw_local, destination, _max_decompressed_bytes())
        except DecompressionLimitError as exc:
            raise _too_large(exc) from exc
        except _UNREADABLE_ERRORS as exc:
            # Damaged, or not gzip at all: refused as an archive that can't
            # be read, rather than a 500.
            raise _archive_unreadable(source, path, exc, gzip=True) from exc
        return filename[: -len(".gz")]

    if is_archive(filename):
        # Read from the source once: the same copy is checked and served.
        with connector.local_copy(source, path, rules=rules) as archive_local:
            if check_whole_archive:
                _require_whole_archive_allowed(source, path, Path(archive_local), rules)
            shutil.copyfile(archive_local, destination)
        return filename

    connector.fetch_file(source, path, destination, rules=rules)
    return filename


# Listings are capped (#347): at most `listing_max_entries` (a System
# setting) entries come back, folders first, then by name; these headers
# say when there were more.
TRUNCATED_HEADER = "X-Listing-Truncated"
LIMIT_HEADER = "X-Listing-Limit"
# Who cut a truncated listing when the cap didn't: "archive" (the archive's
# reader stops at 50,000 entries, and so does an agent listing one) or
# "source" (the source cut the folder's listing: an agent stops at 50,000
# entries). Absent when the cap cut it, so the explorer can say which.
CUT_BY_HEADER = "X-Listing-Cut-By"


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


def _set_listing_headers(
    response: Response, truncated: bool, limit: int, cut_by: str | None
) -> None:
    response.headers[LIMIT_HEADER] = str(limit)
    if truncated:
        response.headers[TRUNCATED_HEADER] = "1"
    if cut_by is not None:
        response.headers[CUT_BY_HEADER] = cut_by


def _cut(capped: bool, cut_before: bool, by: str) -> tuple[bool, str | None]:
    """(truncated, cut_by) of a listing the cap may have cut (`capped`)
    after the archive's reader or the source (`by`) may have cut it
    (`cut_before`). When the cap cut it, the cap is what the explorer
    shows."""
    return capped or cut_before, (by if cut_before and not capped else None)


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
        entries, truncated, cut_by = _browse(
            source, path, refresh, all_members, user, session, limit
        )
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
    _set_listing_headers(response, truncated, limit, cut_by)
    return entries


def _browse(
    source: Source,
    path: str,
    refresh: bool,
    all_members: bool,
    user: User,
    session: Session,
    limit: int,
) -> tuple[list[BrowseEntry], bool, str | None]:
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
            raise PathNotFound()
        if member_dir and not _dir_visible(source, path, rules):
            raise PathNotFound()
        listing_cut = False
        try:
            if _lists_on_agent(source):
                # The agent lists the archive itself, so the archive never
                # leaves the host only to be listed (#382, #249).
                with _agent_unreadable(source, archive_path, None):
                    members, listing_cut = connector.call(
                        "list_archive", source, archive_path, rules=rules
                    )
            else:
                try:
                    if _reads_in_place(connector, archive_name):
                        with _zip_in_place(
                            connector, source, archive_path, rules, "list"
                        ) as archive_file:
                            members, listing_cut = scan_members(archive_file, archive_name)
                    else:
                        with connector.local_copy(
                            source, archive_path, rules=rules
                        ) as archive_local:
                            members, listing_cut = scan_members(archive_local, archive_name)
                except _UNREADABLE_ERRORS as exc:
                    # As when one of its members is opened (_materialize).
                    raise _archive_unreadable(source, archive_path, exc) from exc
        except FetchTooLargeError as exc:
            # An archive over MAX_FETCH_MB (a .zip read in place: a listing
            # that would transfer more than that), or over
            # MAX_DECOMPRESSED_MB once read: the 413 opening or downloading
            # it gives, not a 500.
            raise _fetch_too_large(exc) from exc
        except DecompressionLimitError as exc:
            raise _too_large(exc) from exc
        if all_members:
            if member_dir:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="all is for an archive itself, not a folder inside it",
                )
            flat, capped = _capped(
                _archive_flat_entries(source, archive_path, members, rules), limit, flat=True
            )
            return flat, *_cut(capped, listing_cut, "archive")
        if member_dir and not listing_cut and not has_folder(members, member_dir):
            # Not a folder of the archive: answered as a folder that isn't
            # on the source, and as one the rules hide. A listing cut short
            # can't tell, so it lists nothing, as before.
            raise PathNotFound()
        children = tree_children(members, member_dir)
        listed, capped = _capped(
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
        return listed, *_cut(capped, listing_cut, "archive")

    if all_members:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="all is only for an archive"
        )
    if path and not _dir_visible(source, path, rules):
        # A folder the rules hide is not browsable at all -- otherwise its
        # sub-folder names could still be enumerated by calling the API.
        raise PathNotFound()

    entries = connector.list_directory(source, rules, path)
    listed, capped = _capped(
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
    return listed, *_cut(capped, is_truncated(entries), "source")


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


# Where redaction ran, as the audit log says it (metadata.redaction_at).
REDACTED_ON_SERVER = "server"
REDACTED_ON_AGENT = "agent"


def agent_redaction_rules(session: Session, source: Source) -> list[dict] | None:
    """For content redaction applies to: the rules to send the source's
    agent, in its `redact` block, when the agent redacts on its host (it
    announced redaction/1 on this connection), so the original never leaves
    the host (#382, #249). None when the content is redacted on the server:
    a source that isn't an agent, or an agent that can't redact on the host
    (an older one), which falls back to the server, logged once per
    connection. An agent whose files can't be read at all (require_links)
    gets None without that log: the connector refuses the request
    (agent_update_required, audited where it reads), so nothing falls back."""
    if source.protocol != Protocol.agent:
        return None
    try:
        require_links(source)
    except AgentUpdateRequiredError:
        return None
    registry = get_agent_registry()
    if registry.redacts_on_host(source.id):
        return engine_rules(effective_redaction_rules(session, source.id))
    if registry.first_redaction_fallback(source.id):
        logger.warning(
            "agent.redaction_fallback",
            source_id=source.id,
            message="the agent can't redact on its host; content is redacted on the server",
        )
    return None


def _lists_on_agent(source: Source) -> bool:
    """Whether an archive of `source` is listed (list_archive), and its
    members read (fetch with `member`), on its agent rather than from the
    whole archive fetched here: the agent's connection announced
    redaction/1, with which it lists and extracts archives on its host,
    whether redaction applies or not. The rules are still checked here,
    member by member."""
    return source.protocol == Protocol.agent and get_agent_registry().redacts_on_host(source.id)


def _agent_refusal(exc: AgentRedactionError, agent_rules: list[dict]) -> Exception:
    """What the agent refusing to redact means for the request: the same
    refusals as when the server redacts (UTF-32, an unsupported compressed
    format, an archive that can't be redacted whole), or 502, never the
    content. A rule the agent can't run is named, without falling back to
    the server, which would take the original off a host that redacts."""
    if exc.code == "unsupported_encoding":
        return RedactionEncodingRefused()
    if exc.code == "unsupported_format":
        return RedactionFormatRefused()
    if exc.code == "not_redactable":
        return WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON)
    if exc.code == "rule_unsupported":
        name = next((rule["name"] for rule in agent_rules if rule.get("id") == exc.rule), None)
        logger.error("agent.redaction_rule_unsupported", rule_id=exc.rule, error=str(exc))
        exc.public_message = (
            f"The redaction rule \"{name}\" can't run on this source's agent: "
            "upgrade the agent or change the rule."
            if name is not None
            else "A redaction rule can't run on this source's agent: "
            "upgrade the agent or change the rule."
        )
    return exc


@contextmanager
def _agent_refusals(agent_rules: list[dict]) -> Iterator[None]:
    try:
        yield
    except AgentRedactionError as exc:
        raise _agent_refusal(exc, agent_rules) from exc


@dataclass(frozen=True)
class RedactedRead:
    """What redacting a file's content for the viewer gave: the count per
    rule, how many bytes of the original the content covers (where the Tail
    goes on from), how many bytes at the end of the content stand for an
    unfinished last line, and the original's first HEAD_BYTES (to identify
    the file for the Tail). Redacted on the agent, the server never has
    those bytes: `head_hash` is the agent's digest of them, and `at` says
    where it was redacted. `refused_at` is None, or the offset in the
    original from which the content turned to an encoding redaction can't
    read (UTF-32) and the rest was refused."""

    counts: dict[str, int]
    source_offset: int
    trailing_partial: int
    head: bytes
    head_hash: str | None = None
    at: str = REDACTED_ON_SERVER
    refused_at: int | None = None

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
        if unsupported_compressed_format(head):
            raise RedactionFormatRefused()
        try:
            encoding = detect_encoding(head)
        except UnsupportedEncodingError as exc:
            raise RedactionEncodingRefused() from exc
        src.seek(0)
        with destination.open("wb") as dst:
            result = redact_stream(src, dst, rules, final=final, source_encoding=encoding)
    return RedactedRead(
        result.counts,
        result.source_offset,
        result.trailing_partial,
        head,
        refused_at=result.refused_at,
    )


def _resolve_redacted(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    redaction: CompiledRules,
    owner: str,
    agent_rules: list[dict] | None = None,
) -> _Resolved:
    """_resolve_content when redaction applies: with `agent_rules`, the
    agent redacts on its host (_resolve_on_agent); otherwise the content is fetched (or,
    for a local plain file, read in place), redacted into scratch, and the
    fetched original deleted before the response goes out, so scratch only
    ever keeps the redacted copy. A whole archive (or an archive inside
    one) would carry its members unredacted, so it is refused (403, the
    same answer as an archive hiding files). A .gz member is decompressed
    first, like a .gz file. Content compressed in a format redaction
    doesn't support (unsupported_compressed_format) is refused (409). A .gz
    file or an archive member is complete content, so its last line is redacted
    as it is; a
    plain file may still be written to, so an unfinished last line becomes
    `[redacted: incomplete line]` and the Tail reads it again."""
    filename = path.rsplit("/", 1)[-1]
    if member is not None:
        member = normalize_member_name(member)
        if not member or not _visible(source, f"{path}/{member}", rules):
            raise PathNotFound()
        whole_archive = is_archive(member.rsplit("/", 1)[-1])
    else:
        whole_archive = is_archive(filename)
    if whole_archive:
        logger.warning("archive.redaction_whole_archive_refused", source_id=source.id, path=path)
        raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON)
    if agent_rules is not None:
        return _resolve_on_agent(source, path, member, limits, owner, agent_rules, rules)

    complete = member is not None or is_transparent_gzip(filename)
    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        key = _scratch_key(owner, source.id, path, member)
        destination = store.acquire(key, busy=True)
        fetched = destination.with_name(f"{destination.name}.source")
        plain = destination.with_name(f"{destination.name}.plain")
        in_place = source.protocol == Protocol.local and not complete
        try:
            if in_place:
                original = _local_plain_path(source, path, rules)
                served_filename = filename
            else:
                served_filename = _materialize(source, path, member, rules, fetched)
                original = fetched
                if member is not None and is_transparent_gzip(served_filename):
                    # A .gz member opens like a plain .gz file: decompressed,
                    # then redacted; one that can't be read is refused.
                    _decompress_gzip_member(fetched, plain)
                    fetched.unlink()
                    original = plain
                    served_filename = served_filename[: -len(".gz")]
            with _local_file_gone(path, in_place):
                redacted = _redact_file(original, destination, redaction, final=complete)
        except FetchTooLargeError as exc:
            _drop_scratch(key)
            raise _fetch_too_large(exc) from exc
        except BaseException:
            _drop_scratch(key)
            raise
        finally:
            fetched.unlink(missing_ok=True)
            plain.unlink(missing_ok=True)
    return _Resolved(destination, served_filename, key, redacted)


def _resolve_on_agent(
    source: Source,
    path: str,
    member: str | None,
    limits: Limits,
    owner: str,
    agent_rules: list[dict],
    rules: list[Rule],
) -> _Resolved:
    """_resolve_redacted when the agent redacts on its host (#382, #249):
    it reads the file, the archive member or the .gz file (decompressed)
    and sends only what it redacted, straight into scratch, so the original
    never reaches the server. A plain file's unfinished last line comes as
    the incomplete-line marker, as when the server redacts; the head digest
    the Tail starts from is the agent's."""
    filename = path.rsplit("/", 1)[-1]
    if member is not None and not is_archive(filename):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not an archive")
    target = member.rsplit("/", 1)[-1] if member is not None else filename
    decompress = is_transparent_gzip(target)
    followable = member is None and not decompress
    served_filename = target[: -len(".gz")] if decompress else target
    block = redaction_block(source.id, agent_rules, "utf-8")
    connector = _connector(source)
    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        key = _scratch_key(owner, source.id, path, member)
        destination = store.acquire(key, busy=True)
        try:
            with _agent_refusals(agent_rules), _agent_unreadable(source, path, member):
                fetched = connector.call(
                    "fetch_redacted",
                    source,
                    path,
                    destination,
                    block,
                    member=member,
                    decompress=decompress,
                    rules=rules,
                )
                if followable and fetched.head_digest is None:
                    raise AgentRedactionError("the agent sent no head digest", code="unverified")
        except FetchTooLargeError as exc:
            _drop_scratch(key)
            raise _fetch_too_large(exc) from exc
        except DecompressionLimitError as exc:
            _drop_scratch(key)
            raise _too_large(exc) from exc
        except BaseException:
            _drop_scratch(key)
            raise
    redacted = RedactedRead(
        fetched.counts,
        fetched.source_offset,
        fetched.trailing_partial,
        b"",
        head_hash=fetched.head_digest,
        at=REDACTED_ON_AGENT,
        refused_at=fetched.refused_encoding_at,
    )
    return _Resolved(destination, served_filename, key, redacted)


def _decompress_gzip_member(compressed: Path, destination: Path) -> None:
    """A .gz archive member decompressed into `destination` for redaction,
    up to MAX_DECOMPRESSED_MB (413 past it); 409 (RedactionUnreadableRefused)
    when it can't be read as gzip, rather than redacting compressed bytes."""
    try:
        decompress_gzip(compressed, destination, _max_decompressed_bytes())
    except DecompressionLimitError as exc:
        raise _too_large(exc) from exc
    except _UNREADABLE_ERRORS as exc:
        raise RedactionUnreadableRefused() from exc


def _local_plain_path(source: Source, path: str, rules: list[Rule]) -> Path:
    """A local file served in place, followed through a link only when the
    rules show where it leads (collectors.base.require_target). A folder, or
    a link to one, is refused (collectors.base.refuse_directory). A file
    that isn't there answers as on every other connector
    (SourceNotFoundError, 404), once the rules allowed the path and where
    it leads; one gone before it is read, through _local_file_gone."""
    _require_local_source_allowed(source)
    if source_roots.is_multi_root(source):
        rooted, inner = source_roots.split(source, path)
        label = path.partition("/")[0]
        resolved = local_collector.resolve_file(
            rooted, inner, target_check_for(source, rules, label)
        )
    else:
        resolved = local_collector.resolve_file(source, path, target_check_for(source, rules))
    if not resolved.exists():
        raise SourceNotFoundError(path)
    return resolved


@contextmanager
def _local_file_gone(path: str, in_place: bool = True) -> Iterator[None]:
    """A local file served in place (_local_plain_path) that is gone by the
    time it is read answers as one that isn't there (SourceNotFoundError,
    404), never 500. With `in_place` false (the content is in scratch), the
    error goes out as it is."""
    try:
        yield
    except FileNotFoundError as exc:
        if not in_place:
            raise
        raise SourceNotFoundError(path) from exc


def _resolve_content(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    owner: str,
    redaction: CompiledRules | None = None,
    agent_rules: list[dict] | None = None,
) -> _Resolved:
    """Returns (path_to_serve, filename, scratch_key, redacted). With
    `redaction`, the content is redacted (_resolve_redacted), on the agent
    with `agent_rules`. scratch_key is None
    when nothing was fetched/copied — currently only a local, plain file
    (not an archive member, not transparent-gzip), served straight from its
    real path with no ephemeral scratch involved at all (CLAUDE.md's Built-
    in log viewer note: "a file already on the same machine ... is
    inherently zero-latency and always fresh"). Everything else (remote
    protocols always; local .gz/archive-member, since those produce derived
    bytes that must live somewhere) goes through the scratch store.

    A fetch takes a connector slot while it runs (#344, 503 when none is
    free) and is refused up front (507) when scratch or its disk is full
    (#345). A returned scratch entry is busy (ScratchStore.acquire), so
    neither the idle sweep nor the size guard deletes it while it is being
    fetched and served: the caller calls end_busy once the response is over
    or fails."""
    if not path:
        # Browsing the root is "", but opening or downloading needs a file.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    if not _visible(source, path, rules):
        raise PathNotFound()
    if redaction is not None:
        return _resolve_redacted(source, path, member, rules, limits, redaction, owner, agent_rules)

    filename = path.rsplit("/", 1)[-1]
    needs_scratch = (
        source.protocol != Protocol.local or member is not None or is_transparent_gzip(filename)
    )

    if not needs_scratch:
        resolved = _local_plain_path(source, path, rules)
        if is_archive(filename):
            with _local_file_gone(path):
                _require_whole_archive_allowed(source, path, resolved, rules)
        return _Resolved(resolved, filename, None)

    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        key = _scratch_key(owner, source.id, path, member)
        destination = store.acquire(key, busy=True)
        try:
            served_filename = _materialize(source, path, member, rules, destination)
        except FetchTooLargeError as exc:
            _drop_scratch(key)
            raise _fetch_too_large(exc) from exc
        except BaseException:
            _drop_scratch(key)
            raise
    return _Resolved(destination, served_filename, key)


def _drop_scratch(key: str) -> None:
    """Gives up a scratch entry _resolve_content (or _resolve_redacted,
    _resolve_on_agent) acquired busy, when the fetch fails: its reference
    and the operation, so its file is deleted."""
    store = get_scratch_store()
    store.release(key)
    store.end_busy(key)


# Reads refused before any content was served, audited by the endpoints that
# serve content with the reason _refused_read_reason gives.
_REFUSED_READS = (PathOutsideBaseError, AgentUpdateRequiredError, SourceNotAFileError)


def _refused_read_reason(exc: BaseException) -> str:
    if isinstance(exc, AgentUpdateRequiredError):
        return AGENT_UPDATE_REQUIRED
    if isinstance(exc, SourceNotAFileError):
        return NOT_A_FILE
    return LINK_TARGET_HIDDEN


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
    agent_rules: list[dict] | None = None,
) -> _Resolved:
    """_resolve_content, recording a refused whole archive (or content
    redaction can't read) in the audit log (`denied_action`) before the
    refusal goes out, so every attempt leaves a trace. For an archive
    inside an archive (`member=`), the path recorded is the refused
    archive's virtual path, `<archive>/<member>`. Its scratch entry, if
    any, belongs to `user`'s session (_scratch_owner)."""
    try:
        return _resolve_content(
            source, path, member, rules, limits, _scratch_owner(user), redaction, agent_rules
        )
    except AuditedRefusal as exc:
        refused_path = path if member is None else f"{path}/{normalize_member_name(member)}"
        _audit_refusal(session, user, source, denied_action, refused_path, exc.reason)
        raise
    except _REFUSED_READS as exc:
        # A link to outside the base path or to something the rules hide
        # (answered as 404, app/api/errors.py), or an agent to update:
        # recorded here with the path.
        _audit_refusal(session, user, source, denied_action, path, _refused_read_reason(exc))
        raise


def _redaction_metadata(redacted: RedactedRead | None) -> dict:
    """What the audit entry of a read says about redaction: nothing when it
    didn't apply."""
    if redacted is None:
        return {}
    return {**_redaction_applied(redacted.at), **_redaction_refused(redacted.refused_at)}


def _redaction_applied(at: str) -> dict:
    """What the audit entry of a read redacted on the server or the agent
    (`at`) says."""
    return {"redaction": "applied", "redaction_at": at}


def _redaction_refused(refused_at: int | None) -> dict:
    """What the audit entry of a read says when the content turned to an
    encoding redaction can't read part way (UTF-32): the rest was refused,
    from `redaction_refused_offset` in the original (apart from the
    entry's own `offset`, where a Tail starts). Nothing otherwise."""
    if refused_at is None:
        return {}
    return {
        "redaction_refused": _AUDIT_REDACTION_ENCODING_REASON,
        "redaction_refused_offset": refused_at,
    }


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
    the Content-Length, and the server aborts the response half way.

    The file is opened when the response is made and sent from that open
    file: one deleted afterwards (a log rotated away) is still sent whole,
    and one already gone raises FileNotFoundError there, where the endpoint
    can still answer 404 (_local_file_gone) instead of failing half way."""

    def __init__(self, path: Path, **kwargs):
        file = open(path, "rb")  # noqa: SIM115 - closed once the response is over
        try:
            stat_result = os.fstat(file.fileno())
            super().__init__(path, stat_result=stat_result, **kwargs)
        except BaseException:
            file.close()
            raise
        self._file = file
        self._snapshot_size = stat_result.st_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._file.close()

    @asynccontextmanager
    async def _open_file(self) -> AsyncIterator[anyio.AsyncFile[bytes]]:
        # The file opened when the response was made (closed by __call__),
        # for every way FileResponse sends it, ranges included.
        yield anyio.wrap_file(self._file)

    async def _handle_simple(self, send: Send, send_header_only: bool, send_pathsend: bool) -> None:
        # pathsend would hand the server the whole path, growth included.
        await send(
            {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        remaining = self._snapshot_size
        async with self._open_file() as file:
            more_body = remaining > 0
            if not more_body:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            while more_body:
                chunk = await file.read(min(self.chunk_size, remaining))
                remaining -= len(chunk)
                more_body = remaining > 0 and bool(chunk)
                await send({"type": "http.response.body", "body": chunk, "more_body": more_body})


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
    agent_rules = agent_redaction_rules(session, source) if redaction is not None else None
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
        agent_rules,
    )
    try:
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
                # Over the original's start, up to where the Tail goes on from;
                # redacted on the agent, the agent's digest of it.
                headers["X-Follow-Head"] = (
                    redacted.head_hash
                    if redacted.head_hash is not None
                    else _head_hash(source.id, redacted.head, redacted.source_offset)
                )
        elif followable:
            # What the first Follow poll compares against (#254), so a file
            # rotated between this open and that poll is caught too.
            with _local_file_gone(path, key is None):
                size = served_path.stat().st_size
                with served_path.open("rb") as served:
                    headers["X-Follow-Head"] = _head_hash(
                        source.id, served.read(FOLLOW_HEAD_BYTES), size
                    )
        # Inline, not an attachment: this is what the viewer reads to display a
        # file, not a download endpoint (downloads go through /download, gated
        # by the `download` capability -- #174). nosniff (set app-wide) keeps
        # a browser from rendering it as HTML.
        options: dict = {
            "filename": filename,
            "headers": headers,
            "media_type": "text/plain; charset=utf-8",
            "content_disposition_type": "inline",
        }
        if key is None:
            with _local_file_gone(path):
                return _SnapshotFileResponse(served_path, **options)
        # A scratch file stays busy until it has been sent (or the client
        # went away), so the idle sweep can't delete it half way through.
        store = get_scratch_store()
        return _FinishingFileResponse(served_path, on_finish=lambda: store.end_busy(key), **options)
    except BaseException:
        if key is not None:
            get_scratch_store().end_busy(key)
        raise


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
    end: int | None = Query(default=None, gt=0, lt=2**62),
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

    409 follow_unsupported for .gz files and archives: the viewer reloads
    the whole file instead.
    Audited once per Tail of a file in a session (_FollowAudits), whatever
    offset it starts from, with that offset, and again when a poll reads
    from before the offset audited; not on every poll. A redacted poll
    whose content turned to an encoding redaction can't read (UTF-32) is
    audited too, with where the refused rest started.

    Takes a connector slot while it reads (#344): 503 with Retry-After when
    none is free, as /open.

    When redaction applies to the source (#382, #249), see _follow_redacted:
    only redacted whole lines come back, offsets stay in bytes of the
    original, and X-Follow-Aligned / X-Follow-Partial tell the viewer how
    the answer starts and ends; X-Follow-Start is where its first line
    starts in the original.

    `end` reads what comes before bytes the viewer already holds, for
    "Load earlier" on a file opened at its end: only `[offset, end)`, at
    most RANGE_MAX_BYTES (422 otherwise), or, redacted, the whole lines
    that end by `end` (a line that runs past it isn't sent: `end` is the
    start of a line the viewer shows). X-Follow-Offset is where the answer
    stopped and X-Follow-More is never sent. `head` is then compared with
    the file's first FOLLOW_HEAD_BYTES, which is what the viewer's head
    covers once it holds that much; a file that shrank below `end` or
    starts differently gets X-Follow-Reset."""
    if end is not None and not (offset < end and end - offset <= RANGE_MAX_BYTES):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"end must be after offset and at most {RANGE_MAX_BYTES} bytes after it",
        )
    _require_safe_path(source, path)
    if not path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    rules = _rules_for(session, source.id)
    if not _visible(source, path, rules):
        raise PathNotFound()
    filename = path.rsplit("/", 1)[-1]
    if is_transparent_gzip(filename) or is_archive(filename):
        raise _follow_unsupported("Compressed files and archives can only be reloaded whole")

    redaction = redaction_rules_for(session, source)
    connector = _connector(source)
    agent_rules = agent_redaction_rules(session, source) if redaction is not None else None
    limits = current_limits(session)
    if agent_rules is not None:
        with get_limiter().connector(limits, source.id):
            answer = _follow_on_agent(
                connector, source, path, offset, head, agent_rules, rules, user, session, start, end
            )
        if isinstance(answer, Response):
            return answer
        body, headers, refused_at = answer
        _audit_follow(session, user, source, path, offset, start, REDACTED_ON_AGENT, refused_at)
        return Response(content=body, media_type="application/octet-stream", headers=headers)
    # Redacted, the read starts a little before the offset (context only).
    read_from = max(0, offset - FOLLOW_CONTEXT_BYTES) if redaction is not None else offset
    window_from = offset
    if end is not None and end - read_from > RANGE_MAX_BYTES:
        # Redacted, the context and the window don't fit in one read: the
        # window starts later, keeping its context, and X-Follow-Start says
        # where the answer starts.
        read_from = end - RANGE_MAX_BYTES
        window_from = read_from + FOLLOW_CONTEXT_BYTES
    length = RANGE_MAX_BYTES if end is None else end - read_from
    try:
        # One round trip: the size, the start of the file and the new bytes.
        with get_limiter().connector(limits, source.id):
            size, first, data = connector.read_range(
                source, path, read_from, length, FOLLOW_HEAD_BYTES, rules=rules
            )
    except _REFUSED_READS as exc:
        _audit_refusal(
            session, user, source, "file.open_denied", path, _refused_read_reason(exc), follow=True
        )
        raise

    if end is None:
        changed = head is not None and offset > 0 and head != _head_hash(source.id, first, offset)
        shrank = offset > size
    else:
        changed = head is not None and head != _head_hash(source.id, first, FOLLOW_HEAD_BYTES)
        shrank = end > size
    if shrank or changed:
        return Response(
            content=b"",
            media_type="application/octet-stream",
            headers={"X-Follow-Reset": "1", "X-File-Size": str(size)},
        )

    if redaction is not None:
        if unsupported_compressed_format(first):
            _audit_refusal(
                session,
                user,
                source,
                "file.open_denied",
                path,
                _AUDIT_REDACTION_FORMAT_REASON,
                follow=True,
            )
            raise RedactionFormatRefused()
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
        body, headers, refused_at = _follow_redacted(
            source, redaction, encoding, window_from, read_from, size, first, data, end is not None
        )
    else:
        refused_at = None
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
        if next_offset < size and end is None:
            headers["X-Follow-More"] = "1"

    _audit_follow(
        session,
        user,
        source,
        path,
        offset,
        start,
        REDACTED_ON_SERVER if redaction else None,
        refused_at,
    )
    return Response(content=body, media_type="application/octet-stream", headers=headers)


# How long a Tail may go without a poll before the next one counts as a new
# Tail, audited again; and how many Tails are remembered at once (the
# least recently polled are forgotten first, and audited again if they come
# back).
FOLLOW_AUDIT_IDLE_SECONDS = 600
FOLLOW_AUDIT_MAX_TAILS = 10_000


class _FollowAudits:
    """Which Tails have been audited: one entry per session, source and
    file, with the time of its last poll and the lowest offset audited for
    it. In memory only, like the scratch bookkeeping; after a restart every
    Tail is audited again."""

    def __init__(self) -> None:
        self._polled: OrderedDict[tuple[str, int, str], tuple[float, int]] = OrderedDict()
        self._lock = threading.Lock()

    def starts(self, key: tuple[str, int, str], offset: int, *, start: bool) -> bool:
        """Records a poll of the Tail `key` from `offset`. True when it
        starts a Tail: the viewer says so (`start`), no poll of it was seen
        recently, or it reads from before the lowest offset audited for it
        (the viewer never goes back within a Tail, so a read from earlier
        is on record as a Tail of its own)."""
        now = time.monotonic()
        with self._lock:
            last = self._polled.pop(key, None)
            starts = (
                start
                or last is None
                or now - last[0] > FOLLOW_AUDIT_IDLE_SECONDS
                or offset < last[1]
            )
            lowest = offset if starts else last[1]
            self._polled[key] = (now, lowest)
            while len(self._polled) > FOLLOW_AUDIT_MAX_TAILS:
                self._polled.popitem(last=False)
        return starts

    def clear(self) -> None:
        with self._lock:
            self._polled.clear()


_follow_audits = _FollowAudits()


def _audit_follow(
    session: Session,
    user: User,
    source: Source,
    path: str,
    offset: int,
    start: bool,
    redacted_at: str | None,
    refused_at: int | None = None,
) -> None:
    """Writes file.follow, with the offset the Tail starts from, when this
    poll starts a Tail (_FollowAudits): whatever offset a client asks from,
    what it reads through here is on record, from the lowest offset read.
    A poll whose redacted content turned to an encoding redaction can't
    read (`refused_at`, in bytes of the original) is written too, with the
    offset it asked from and where the refused rest started."""
    starts = _follow_audits.starts((_scratch_owner(user), source.id, path), offset, start=start)
    if not starts and refused_at is None:
        return
    metadata: dict = {"path": path, "offset": offset}
    if redacted_at is not None:
        metadata.update(_redaction_applied(redacted_at))
    metadata.update(_redaction_refused(refused_at))
    record_audit_event(
        session,
        user_id=user.id,
        action="file.follow",
        target_type="source",
        target_id=source.id,
        metadata=metadata,
    )
    session.commit()


def _follow_on_agent(
    connector,
    source: Source,
    path: str,
    offset: int,
    head: str | None,
    agent_rules: list[dict],
    rules: list[Rule],
    user: User,
    session: Session,
    start: bool = False,
    end: int | None = None,
) -> tuple[bytes, dict[str, str], int | None] | Response:
    """The redacted Tail when the agent redacts on its host (#382, #249):
    the agent reads, from a little before `offset`, only whole lines and
    redacts them (the same answer as _follow_redacted), and never sends the
    start of the file, only its digests. A refusal (UTF-32, a compressed
    format, a link the rules hide, an agent to update) is audited as when
    the server redacts; the last item is where the agent refused the rest
    of what it read (UTF-32 part way), or None.

    The first poll of a Tail and a read up to `end` also ask where each
    line starts, for X-Follow-Start; up to `end`, an unfinished last line
    isn't sent (see follow_file)."""
    block = redaction_block(source.id, agent_rules, "utf-8")
    # The agent reads its context within RANGE_MAX_BYTES too: up to `end`,
    # the window starts later when both don't fit, as on the server.
    read_offset = offset if end is None else max(offset, end - FOLLOW_WINDOW_BYTES)
    length = RANGE_MAX_BYTES if end is None else end - read_offset
    try:
        with _agent_refusals(agent_rules):
            read = connector.call(
                "read_range_redacted",
                source,
                path,
                read_offset,
                length,
                block,
                line_offsets=start or end is not None,
                rules=rules,
            )
    except AuditedRefusal as exc:
        _audit_refusal(session, user, source, "file.open_denied", path, exc.reason, follow=True)
        raise
    except _REFUSED_READS as exc:
        _audit_refusal(
            session, user, source, "file.open_denied", path, _refused_read_reason(exc), follow=True
        )
        raise
    if end is None:
        changed = head is not None and offset > 0 and head != read.head_digest
        shrank = offset > read.size
    else:
        # The digest made where the head covers FOLLOW_HEAD_BYTES (see
        # follow_file): at the offset, or else where the answer stopped.
        if read_offset >= FOLLOW_HEAD_BYTES:
            covering = read.head_digest
        elif read.next_offset >= FOLLOW_HEAD_BYTES:
            covering = read.next_head_digest
        else:
            covering = None
        changed = head is not None and covering is not None and head != covering
        shrank = end > read.size
    if shrank or changed:
        return Response(
            content=b"",
            media_type="application/octet-stream",
            headers={"X-Follow-Reset": "1", "X-File-Size": str(read.size)},
        )
    data, partial = read.data, read.trailing_partial
    counts = read.counts
    first_line = None
    if end is not None and partial:
        # Only whole lines up to `end`.
        data, partial = data[: len(data) - partial], 0
    if (
        end is not None
        and not data
        and not read.lines
        and read.refused_encoding_at is None
        and read_offset > 0
    ):
        # No line starts in the window: the line that fills it is longer
        # than a window. Fail closed, and go on before it, as
        # _follow_redacted does when bounded.
        data = marker(long_line_label(FOLLOW_WINDOW_BYTES)) + b"\n"
        counts = {}
        first_line = read_offset
    headers = {
        "X-File-Size": str(read.size),
        "X-Follow-Offset": str(read.next_offset),
        "X-Follow-Head": read.next_head_digest,
        "X-Redacted": "1",
        "X-Redaction-Count": str(sum(counts.values())),
        "X-Follow-Partial": str(partial),
    }
    if first_line is not None:
        headers["X-Follow-Start"] = str(first_line)
    elif read.lines is not None:
        headers["X-Follow-Start"] = str(read.lines[0][0] if read.lines else read.next_offset)
    if read.aligned:
        headers["X-Follow-Aligned"] = "1"
    if read.read_end < read.size and end is None:
        headers["X-Follow-More"] = "1"
    return data, headers, read.refused_encoding_at


def _follow_redacted(
    source: Source,
    redaction: CompiledRules,
    encoding: str | None,
    offset: int,
    read_from: int,
    size: int,
    first: bytes,
    data: bytes,
    bounded: bool = False,
) -> tuple[bytes, dict[str, str], int | None]:
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
    - From a line that looks UTF-32, the rest of what was read is refused:
      `[redacted: unsupported encoding]` ends the answer and the Tail goes
      on after what was read. The last item returned is where that started
      in the original, or None.
    - Offsets, X-File-Size and X-Follow-Head are about the original;
      X-Follow-Start is where the first line sent starts.
    - `bounded` (a read up to `end`, follow_file): `data` ends at `end`, an
      unfinished last line isn't sent, and when no line starts in the
      window, the line that fills it becomes the long-line marker and the
      answer starts at `offset`."""
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
    first_line = read_from + window.begin
    if bounded:
        if partial:
            body, partial = body[: len(body) - partial], 0
        if not body and window.begin >= len(data) and offset > 0:
            # No line starts after the offset: the line around it is longer
            # than a window. Fail closed, and go on before it.
            body = marker(long_line_label(FOLLOW_WINDOW_BYTES)) + b"\n"
            counts = {}
            first_line = offset
    elif next_offset <= offset and read_end < size:
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
        "X-Follow-Start": str(first_line),
    }
    if window.aligned:
        headers["X-Follow-Aligned"] = "1"
    # More is waiting only when something wasn't read yet: an unfinished
    # last line is read again on the next poll, not right away.
    if read_end < size and not bounded:
        headers["X-Follow-More"] = "1"
    refused_at = None if window.refused_at is None else read_from + window.refused_at
    return body, headers, refused_at


class CloseRequest(BaseModel):
    path: str
    member: str | None = None


@router.post("/close", status_code=status.HTTP_204_NO_CONTENT)
def close_file(
    payload: CloseRequest,
    source: Source = Depends(require_capability(Capability.view, get_current_active_user)),
    user: User = Depends(get_current_active_user),
) -> None:
    """Releases what this session opened at `path` (and `member`); the
    scratch key names the session (_scratch_owner), so another viewer's
    copy of the same file is never touched."""
    # The scratch key is a hash, not a filesystem path, so an unsafe path
    # here can't actually escape anywhere — checked anyway for consistency
    # with every other endpoint that takes a client-supplied path.
    _require_safe_path(source, payload.path)
    store = get_scratch_store()
    key = _scratch_key(_scratch_owner(user), source.id, payload.path, payload.member)
    store.release(key)


# Redacted downloads (#382, #249): every redacted download says so in this
# header, and in its file name when the redaction_download_suffix setting
# is on.
REDACTED_DOWNLOAD_HEADER = "X-PerchTail-Redacted"
# What a redacted archive or folder ZIP leaves out is listed in this file
# inside it.
REDACTION_SKIPPED_NAME = "REDACTION-SKIPPED.txt"
_REDACTION_SKIPPED_FALLBACK_NAME = "REDACTION-SKIPPED (PerchTail).txt"
# Why a redacted download left a file out, besides archives.SKIP_*.
_SKIP_ENCODING = "unsupported_encoding"
_SKIP_TOO_LARGE = "too_large"
_SKIP_UNREADABLE = "unreadable"
_SKIP_FORMAT = "unsupported_format"
_REDACTION_SKIP_TEXT = {
    SKIP_NESTED_ARCHIVE: "an archive inside an archive, which redaction doesn't open",
    SKIP_ENCRYPTED: "encrypted, so it can't be redacted",
    SKIP_LINK: "a symbolic link, which redaction doesn't follow",
    SKIP_UNSUPPORTED_COMPRESSION: "compressed with a method that can't be read",
    _SKIP_ENCODING: "in an encoding redaction doesn't support (UTF-32)",
    _SKIP_FORMAT: "in a compressed format redaction doesn't support",
    _SKIP_TOO_LARGE: "too large once decompressed (MAX_DECOMPRESSED_MB)",
    _SKIP_UNREADABLE: "can't be read as an archive or a compressed file",
}
# What a corrupt archive or compressed file raises while it is read, and
# a .zip read in place that changed while it was read.
_UNREADABLE_ERRORS = (
    zipfile.BadZipFile,
    tarfile.TarError,
    gzip.BadGzipFile,
    EOFError,
    zlib.error,
    RemoteFileChangedError,
)


def download_redaction_rules_for(session: Session, source: Source) -> CompiledRules | None:
    """The rules a download from `source` is redacted with: those of the
    viewer (redaction_rules_for) while the redaction_downloads setting is
    on; None, and the original content, while it is off."""
    if not system_settings.get_bool(session, "redaction_downloads"):
        return None
    return redaction_rules_for(session, source)


def redacted_filename(name: str) -> str:
    """`app.log` -> `app.redacted.log`, `app.log.gz` -> `app.redacted.log.gz`,
    `logs.tar.gz` -> `logs.redacted.tar.gz`, `README` -> `README.redacted`."""
    lower = name.lower()
    for extension in (".tar.gz", ".tgz"):
        if lower.endswith(extension) and len(name) > len(extension):
            cut = len(name) - len(extension)
            return f"{name[:cut]}.redacted{name[cut:]}"
    if lower.endswith(".gz") and len(name) > len(".gz"):
        return f"{redacted_filename(name[:-3])}{name[-3:]}"
    stem, dot, extension = name.rpartition(".")
    if dot and stem:
        return f"{stem}.redacted.{extension}"
    return f"{name}.redacted"


def _download_name(session: Session, name: str) -> str:
    if system_settings.get_bool(session, "redaction_download_suffix"):
        return redacted_filename(name)
    return name


def _read_head(src: BinaryIO) -> bytes:
    """The first HEAD_BYTES of a stream (fewer only at its end), which
    decide its encoding the same way /open does."""
    head = b""
    while len(head) < HEAD_BYTES:
        block = src.read(HEAD_BYTES - len(head))
        if not block:
            break
        head += block
    return head


def _redact_into(
    src: BinaryIO, destination: Path, rules: CompiledRules, max_bytes: int | None
) -> str | None:
    """Redacts the whole of `src` into `destination`, for a download: the
    last line is redacted as it is, and UTF-16 content goes back to UTF-16.
    Returns None, or why it can't be redacted (nothing is then written):
    content that turns UTF-32 part way is left out whole, as one that
    starts so. Raises DecompressionLimitError past `max_bytes` of
    content."""
    head = _read_head(src)
    if unsupported_compressed_format(head):
        return _SKIP_FORMAT
    try:
        encoding = detect_encoding(head)
    except UnsupportedEncodingError:
        return _SKIP_ENCODING
    redactor = Redactor(rules, source_encoding=encoding, output_encoding="source")
    total = 0
    try:
        with destination.open("wb") as dst:
            block = head
            while block:
                total += len(block)
                if max_bytes is not None and total > max_bytes:
                    raise DecompressionLimitError(
                        f"decompressed size exceeds the {max_bytes}-byte limit"
                    )
                dst.write(redactor.feed(block))
                if redactor.refused_at is not None:
                    break
                block = src.read(CHUNK_BYTES)
            dst.write(redactor.finish(final=True))
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if redactor.refused_at is not None:
        destination.unlink(missing_ok=True)
        return _SKIP_ENCODING
    return None


def _redact_content(
    name: str,
    src: BinaryIO,
    destination: Path,
    rules: CompiledRules,
    work_dir: Path,
) -> str | None:
    """A file of a redacted archive or folder ZIP, redacted into
    `destination`: a .gz file is decompressed, redacted and compressed again
    under the same name. Returns None, or why it is left out: content
    redaction can't read, past MAX_DECOMPRESSED_MB once decompressed, or a
    corrupt .gz file."""
    max_bytes = _max_decompressed_bytes()
    try:
        if not is_transparent_gzip(name):
            return _redact_into(src, destination, rules, max_bytes)
        plain = work_dir / "redact-gz"
        try:
            with gzip.GzipFile(fileobj=src, mode="rb") as decompressed:
                reason = _redact_into(decompressed, plain, rules, max_bytes)
            if reason is None:
                with plain.open("rb") as redacted, gzip.open(destination, "wb") as dst:
                    shutil.copyfileobj(redacted, dst, CHUNK_BYTES)
            return reason
        finally:
            plain.unlink(missing_ok=True)
    except DecompressionLimitError:
        destination.unlink(missing_ok=True)
        return _SKIP_TOO_LARGE
    except _UNREADABLE_ERRORS:
        destination.unlink(missing_ok=True)
        return _SKIP_UNREADABLE


def _redaction_skipped_name(names: set[str]) -> str:
    """REDACTION-SKIPPED.txt, unless a file by that name is already at the
    top of the archive (compared ignoring case, so extracting on Windows
    can't collide)."""
    if REDACTION_SKIPPED_NAME.lower() in {name.lower() for name in names}:
        return _REDACTION_SKIPPED_FALLBACK_NAME
    return REDACTION_SKIPPED_NAME


def _redaction_skipped_text(skipped: list[tuple[str, str]]) -> bytes:
    lines = [
        "These files were left out of this redacted download:\n",
        *(f"{name}: {_REDACTION_SKIP_TEXT[reason]}\n" for name, reason in skipped),
    ]
    return "".join(lines).encode("utf-8")


def _redaction_skipped_list(skipped: list[tuple[str, str]], names: set[str]) -> tuple[str, bytes]:
    return _redaction_skipped_name(names), _redaction_skipped_text(skipped)


class _ArchiveUnreadable(Exception):
    """An archive a redacted download can't read, so it can't be rebuilt."""


def _log_rebuild_done(
    source: Source,
    virtual_path: str,
    destination: Path,
    redacted: int,
    skipped: list[tuple[str, str]],
    started: float,
) -> None:
    """INFO archive.rebuild_done at the end of a redacted rebuild: how large
    the result is, how many members went in redacted and how many were left
    out, and how long it took."""
    try:
        size: int | None = destination.stat().st_size
    except OSError:
        size = None
    logger.info(
        "archive.rebuild_done",
        source_id=source.id,
        path=virtual_path,
        result_bytes=size,
        redacted_members=redacted,
        skipped_members=len(skipped),
        seconds=round(time.monotonic() - started, 3),
    )


def _rebuild_redacted(
    source: Source,
    virtual_path: str,
    local_archive: Path,
    filename: str,
    destination: Path,
    rules: list[Rule],
    redaction: CompiledRules,
    work_dir: Path,
) -> list[tuple[str, str]]:
    """The archive at `virtual_path` (copied to `local_archive`), rebuilt in
    the same format into `destination` with its members redacted
    (app.archives.rebuild_archive, _redact_content). A member the rules
    hide on purpose (the last rule matching `<archive>/<member>` is an
    exclude, as for _archive_has_hidden_members) is left out without a
    trace; what can't be redacted is left out and listed in
    REDACTION-SKIPPED.txt. Returns the (member, reason) of each one listed.
    Raises _ArchiveUnreadable for an archive that can't be read."""
    case_insensitive = rules_case_insensitive(source)

    def keep(name: str) -> bool:
        return not is_explicitly_excluded(
            f"{virtual_path}/{name}", rules, case_insensitive=case_insensitive
        )

    redacted = 0
    started = time.monotonic()

    def transform(name: str, src: BinaryIO, out: Path) -> str | None:
        nonlocal redacted
        reason = _redact_content(name, src, out, redaction, work_dir)
        if reason is None:
            redacted += 1
        return reason

    try:
        skipped = rebuild_archive(
            local_archive,
            filename,
            destination,
            work_dir,
            keep=keep,
            transform=transform,
            skipped_list=_redaction_skipped_list,
        )
        _log_rebuild_done(source, virtual_path, destination, redacted, skipped, started)
        return skipped
    except (*_UNREADABLE_ERRORS, NotImplementedError) as exc:
        # NotImplementedError: a zip feature zipfile can't read, met
        # outside a single member (a member's own is listed instead).
        logger.warning(
            "archive.redaction_archive_unreadable",
            source_id=source.id,
            path=virtual_path,
            error_type=type(exc).__name__,
        )
        raise _ArchiveUnreadable() from exc


class _RedactedDownload(NamedTuple):
    path: Path
    filename: str
    # Content redacted while it is sent, in this encoding (detect_encoding);
    # unused for a rebuilt archive, which is sent as it is.
    encoding: str | None
    # Already redacted (a rebuilt archive, a .gz member): sent as it is.
    ready: bool
    # For a rebuilt archive, the (member, reason) of each member left out.
    skipped: list[tuple[str, str]] | None
    work_dir: Path
    at: str = REDACTED_ON_SERVER
    # Redacted on the agent: where it refused the rest of the content (UTF-32
    # part way), or None.
    refused_at: int | None = None
    # Content redacted while it is sent: the file, already open, it is read
    # from (_redacted_chunks), closed when the download is over.
    file: BinaryIO | None = None


# Why the agent left a member of an archive out (fetch_archive), as the
# reasons a server-side rebuild gives.
_AGENT_MEMBER_SKIPS = {
    "not_found": _SKIP_UNREADABLE,
    "encrypted": SKIP_ENCRYPTED,
    "nested_archive": SKIP_NESTED_ARCHIVE,
    "unsupported_encoding": _SKIP_ENCODING,
    "unsupported_format": _SKIP_FORMAT,
    "too_large": _SKIP_TOO_LARGE,
    "unreadable": _SKIP_UNREADABLE,
}
# A redaction refusal of the agent that leaves one file out of a folder ZIP
# rather than failing it.
_AGENT_FILE_SKIPS = {
    "unsupported_encoding": _SKIP_ENCODING,
    "unsupported_format": _SKIP_FORMAT,
}
# Errors of the agent on a whole archive that keep their own answer rather
# than refusing the archive as one it can't read.
_AGENT_ARCHIVE_ERRORS_KEPT = (
    AgentRedactionError,
    AgentUpdateRequiredError,
    SourceUnreachableError,
    SourcePermissionError,
    SourceNotFoundError,
    SourceNotAFileError,
)


def _gzip_file(plain: Path, destination: Path) -> None:
    with plain.open("rb") as src, gzip.open(destination, "wb") as dst:
        shutil.copyfileobj(src, dst, CHUNK_BYTES)


def _rebuild_on_agent(
    connector,
    source: Source,
    virtual_path: str,
    filename: str,
    destination: Path,
    rules: list[Rule],
    agent_rules: list[dict],
    work_dir: Path,
) -> list[tuple[str, str]]:
    """_rebuild_redacted when the agent redacts on its host (#382, #249):
    the agent lists the archive (list_archive), the members the rules don't
    hide on purpose are asked for (an archive inside it is left out and
    listed, as the agent can't redact it), and the agent sends them
    redacted, in one pass (fetch_archive). The archive is rebuilt here in
    the same format, a .gz member compressed again under its name; what the
    agent left out is listed in REDACTION-SKIPPED.txt. The original never
    reaches the server. Raises _ArchiveUnreadable when the listing was cut,
    so the archive would be missing members without saying so."""
    started = time.monotonic()
    members, cut = connector.call("list_archive", source, virtual_path, rules=rules)
    if cut:
        logger.warning(
            "archive.redaction_archive_listing_cut", source_id=source.id, path=virtual_path
        )
        raise _ArchiveUnreadable()
    case_insensitive = rules_case_insensitive(source)
    skipped: list[tuple[str, str]] = []
    wanted: list[str] = []
    for member in members:
        name = normalize_member_name(member.name)
        if member.is_dir or not name:
            continue
        hidden = is_explicitly_excluded(
            f"{virtual_path}/{name}", rules, case_insensitive=case_insensitive
        )
        if hidden:
            continue
        if is_archive(name.rsplit("/", 1)[-1]):
            skipped.append((name, SKIP_NESTED_ARCHIVE))
            continue
        if member.name not in wanted:
            wanted.append(member.name)
    member_dir = work_dir / "agent-members"
    member_dir.mkdir(exist_ok=True)
    try:
        received, _ = connector.call(
            "fetch_archive",
            source,
            virtual_path,
            wanted,
            member_dir,
            redaction_block(source.id, agent_rules, "source"),
            rules=rules,
        )
        entries: list[tuple[str, Path, float | None]] = []
        for index, item in enumerate(received):
            name = normalize_member_name(item.name)
            if item.skipped is not None or item.path is None:
                reason = _AGENT_MEMBER_SKIPS.get(item.skipped or "", _SKIP_UNREADABLE)
                skipped.append((name, reason))
                continue
            content = item.path
            if item.gunzipped:
                # Sent decompressed to be redacted: compressed again, under
                # the name it has in the archive.
                content = member_dir / f"gz-{index}"
                _gzip_file(item.path, content)
                item.path.unlink(missing_ok=True)
            entries.append((name, content, item.mtime))
        write_archive(filename, destination, entries, skipped, _redaction_skipped_list)
    finally:
        shutil.rmtree(member_dir, ignore_errors=True)
    _log_rebuild_done(source, virtual_path, destination, len(entries), skipped, started)
    return skipped


def _prepare_on_agent(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    agent_rules: list[dict],
) -> _RedactedDownload:
    """_prepare_redacted_download when the agent redacts on its host (#382,
    #249): everything arrives redacted, in the file's own encoding, and is
    sent as it is. A file or member as it is; a .gz file decompressed (as
    without redaction); a .gz member decompressed on the agent and
    compressed again here; a whole archive rebuilt here from the members
    the agent sent redacted (_rebuild_on_agent). An archive inside an
    archive can't be redacted on the agent, so it is refused (403), as is
    one that can't be read whole or that the agent can't read. Takes a
    connector slot while it fetches."""
    filename = path.rsplit("/", 1)[-1]
    if member is not None and not is_archive(filename):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="not an archive")
    target = member.rsplit("/", 1)[-1] if member is not None else filename
    if is_archive(target) and member is not None:
        logger.warning("archive.redaction_nested_archive_refused", source_id=source.id, path=path)
        raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON)
    connector = _connector(source)
    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        work_dir = make_scratch_dir("perchtail-download-")
        try:
            with _agent_refusals(agent_rules):
                if is_archive(target):
                    rebuilt = work_dir / "rebuilt"
                    try:
                        skipped = _rebuild_on_agent(
                            connector, source, path, target, rebuilt, rules, agent_rules, work_dir
                        )
                    except _ArchiveUnreadable as exc:
                        raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON) from exc
                    except _AGENT_ARCHIVE_ERRORS_KEPT:
                        raise
                    except SourceError as exc:
                        # The agent couldn't read the archive (a corrupt zip
                        # or tar.gz): refused as a server-side rebuild
                        # refuses one it can't read (_rebuild_redacted).
                        logger.warning(
                            "archive.redaction_archive_unreadable",
                            source_id=source.id,
                            path=path,
                            error_type=type(exc).__name__,
                        )
                        raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON) from exc
                    return _RedactedDownload(
                        rebuilt, target, None, True, skipped, work_dir, REDACTED_ON_AGENT
                    )
                decompress = is_transparent_gzip(target)
                content = work_dir / "redacted"
                with _agent_unreadable(source, path, member):
                    fetched = connector.call(
                        "fetch_redacted",
                        source,
                        path,
                        content,
                        redaction_block(source.id, agent_rules, "source"),
                        member=member,
                        decompress=decompress,
                        rules=rules,
                    )
                if member is not None and decompress:
                    if fetched.refused_encoding_at is not None:
                        # Compressed again whole, as the server refuses one
                        # it can't redact whole (_redact_content).
                        raise RedactionEncodingRefused()
                    # A .gz member is sent as it is in the archive: compressed.
                    rebuilt = work_dir / "rebuilt"
                    _gzip_file(content, rebuilt)
                    content.unlink()
                    return _RedactedDownload(
                        rebuilt, target, None, True, None, work_dir, REDACTED_ON_AGENT
                    )
                served = target[: -len(".gz")] if decompress else target
                return _RedactedDownload(
                    content,
                    served,
                    None,
                    True,
                    None,
                    work_dir,
                    REDACTED_ON_AGENT,
                    fetched.refused_encoding_at,
                )
        except FetchTooLargeError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise _fetch_too_large(exc) from exc
        except DecompressionLimitError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise _too_large(exc) from exc
        except BaseException:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise


def _prepare_redacted_download(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    redaction: CompiledRules,
    agent_rules: list[dict] | None = None,
) -> _RedactedDownload:
    """A download when redaction applies to it. A file, a .gz file (sent
    decompressed, as without redaction) or an archive member is fetched
    into scratch (a local plain file is read in place) and redacted while
    it is sent; UTF-32 content, or content compressed in a format redaction
    doesn't support (unsupported_compressed_format), is refused (409). A whole archive, or a
    member that is one, is rebuilt in the same format with only the members
    the rules don't hide, each redacted (_rebuild_redacted), and sent once
    rebuilt; one that can't be read is refused (403). Takes a connector
    slot while it fetches (#344)."""
    if not path:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="path is required")
    if not _visible(source, path, rules):
        raise PathNotFound()
    filename = path.rsplit("/", 1)[-1]
    if member is not None:
        member = normalize_member_name(member)
        if not member or not _visible(source, f"{path}/{member}", rules):
            raise PathNotFound()
        target, virtual_path = member.rsplit("/", 1)[-1], f"{path}/{member}"
    else:
        target, virtual_path = filename, path
    if agent_rules is not None:
        return _prepare_on_agent(source, path, member, rules, limits, agent_rules)
    whole_archive = is_archive(target)
    in_place = (
        source.protocol == Protocol.local
        and member is None
        and not whole_archive
        and not is_transparent_gzip(filename)
    )
    store = get_scratch_store()
    with get_limiter().connector(limits, source.id):
        store.ensure_room()
        work_dir = make_scratch_dir("perchtail-download-")
        try:
            fetched = work_dir / "source"
            if in_place:
                original = _local_plain_path(source, path, rules)
                served_filename = filename
            else:
                served_filename = _materialize(
                    source, path, member, rules, fetched, check_whole_archive=False
                )
                original = fetched
            if whole_archive:
                rebuilt = work_dir / "rebuilt"
                try:
                    skipped = _rebuild_redacted(
                        source, virtual_path, fetched, target, rebuilt, rules, redaction, work_dir
                    )
                except _ArchiveUnreadable as exc:
                    raise WholeArchiveRefused(_AUDIT_REDACTION_ARCHIVE_REASON) from exc
                fetched.unlink(missing_ok=True)
                return _RedactedDownload(rebuilt, served_filename, None, True, skipped, work_dir)
            if member is not None and is_transparent_gzip(target):
                # Sent as it is in the archive, a .gz member: decompressed,
                # redacted and compressed again.
                rebuilt = work_dir / "rebuilt"
                with fetched.open("rb") as src:
                    reason = _redact_content(target, src, rebuilt, redaction, work_dir)
                fetched.unlink(missing_ok=True)
                if reason == _SKIP_ENCODING:
                    raise RedactionEncodingRefused()
                if reason == _SKIP_FORMAT:
                    raise RedactionFormatRefused()
                if reason == _SKIP_TOO_LARGE:
                    raise _too_large(DecompressionLimitError("decompressed size over the limit"))
                if reason is not None:
                    raise RedactionUnreadableRefused()
                return _RedactedDownload(rebuilt, served_filename, None, True, None, work_dir)
            # Opened here and sent from this open file (_redacted_chunks), so a
            # local file gone before the download starts answers 404
            # (_local_file_gone) and one deleted while it is sent still is.
            with _local_file_gone(path, in_place):
                src = original.open("rb")
            try:
                head = _read_head(src)
                src.seek(0)
                if unsupported_compressed_format(head):
                    raise RedactionFormatRefused()
                try:
                    encoding = detect_encoding(head)
                except UnsupportedEncodingError as exc:
                    raise RedactionEncodingRefused() from exc
            except BaseException:
                src.close()
                raise
            return _RedactedDownload(
                original, served_filename, encoding, False, None, work_dir, file=src
            )
        except FetchTooLargeError as exc:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise _fetch_too_large(exc) from exc
        except BaseException:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise


def _redacted_chunks(
    original: BinaryIO,
    rules: CompiledRules,
    encoding: str | None,
    record_refusal: Callable[[int], None],
) -> Iterator[bytes]:
    """`original` (an open file, closed here once started) redacted a block
    at a time as it is sent: the last line
    as it is, UTF-16 back to UTF-16. Content that turns UTF-32 part way
    ends with `[redacted: unsupported encoding]`, and `record_refusal` gets
    where the refused rest started, however the download ends."""
    redactor = Redactor(rules, source_encoding=encoding, output_encoding="source")
    try:
        with original as src:
            while block := src.read(CHUNK_BYTES):
                out = redactor.feed(block)
                if out:
                    yield out
                if redactor.refused_at is not None:
                    break
        out = redactor.finish(final=True)
        if out:
            yield out
    finally:
        if redactor.refused_at is not None:
            record_refusal(redactor.refused_at)


def _download_redacted(
    source: Source,
    path: str,
    member: str | None,
    rules: list[Rule],
    limits: Limits,
    redaction: CompiledRules,
    user: User,
    session: Session,
    agent_rules: list[dict] | None = None,
) -> Response:
    """download_file when the download is redacted (#382, #249), on the
    agent with `agent_rules`."""
    transfer = get_limiter().transfer(limits, user.id)
    try:
        try:
            prepared = _prepare_redacted_download(
                source, path, member, rules, limits, redaction, agent_rules
            )
        except AuditedRefusal as exc:
            refused_path = path if member is None else f"{path}/{normalize_member_name(member)}"
            _audit_refusal(session, user, source, "file.download_denied", refused_path, exc.reason)
            raise
        except _REFUSED_READS as exc:
            _audit_refusal(
                session, user, source, "file.download_denied", path, _refused_read_reason(exc)
            )
            raise
    except BaseException:
        transfer.release()
        raise

    def finish() -> None:
        transfer.release()
        shutil.rmtree(prepared.work_dir, ignore_errors=True)

    def close_file() -> None:
        if prepared.file is not None:
            prepared.file.close()

    try:
        metadata: dict = {"path": path, "member": member} if member else {"path": path}
        metadata.update(_redaction_applied(prepared.at))
        metadata.update(_redaction_refused(prepared.refused_at))
        if prepared.skipped is not None:
            metadata["skipped_count"] = len(prepared.skipped)
        record_audit_event(
            session,
            user_id=user.id,
            action="file.download",
            target_type="source",
            target_id=source.id,
            metadata=metadata,
        )
        session.commit()
    except BaseException:
        close_file()
        finish()
        raise
    name = _download_name(session, prepared.filename)
    headers = {REDACTED_DOWNLOAD_HEADER: "1"}
    if prepared.ready:
        return _FinishingFileResponse(
            prepared.path, filename=name, headers=headers, on_finish=finish
        )
    # Set whenever the content isn't ready (_prepare_redacted_download).
    stream = _redacted_chunks(
        prepared.file,
        redaction,
        prepared.encoding,
        _late_refusal_recorder(session, user, source, metadata),
    )

    def finish_stream() -> None:
        # The generator's own cleanup (its open file) first, if the download
        # was cut short; a generator still running in its thread does it
        # itself.
        try:
            stream.close()
        except ValueError:
            pass
        else:
            # A generator that never started never closes its file itself.
            close_file()
        finish()

    headers["Content-Disposition"] = _attachment(name)
    return _FinishingStreamingResponse(
        stream,
        media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
        headers=headers,
        on_finish=finish_stream,
    )


@router.get("/download")
def download_file(
    path: str,
    member: str | None = None,
    source: Source = Depends(readable_source(Capability.download)),
    user: User = Depends(get_current_active_user),
    session: Session = Depends(get_session),
) -> Response:
    """One-shot: fetches, streams, and releases within the same request —
    no persistent session, so no /close call is needed afterward.

    Counts as one of the user's transfers (#344) until the client has the
    whole file or goes away: 503 with Retry-After when the user already has
    as many downloads and folder ZIPs in progress as allowed.

    When redaction applies and the redaction_downloads setting is on
    (#382, #249), the download is redacted on the server, for every user
    (_prepare_redacted_download), says so in X-PerchTail-Redacted and, with
    redaction_download_suffix on, in its name (`app.redacted.log`)."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    limits = current_limits(session)
    redaction = download_redaction_rules_for(session, source)
    if redaction is not None:
        return _download_redacted(
            source,
            path,
            member,
            rules,
            limits,
            redaction,
            user,
            session,
            agent_redaction_rules(session, source),
        )
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
        # The scratch file stays busy until the client has it (or went
        # away), then goes with its only reference.
        transfer.release()
        if key:
            store.release(key)
            store.end_busy(key)

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
        with _local_file_gone(path, key is None):
            return _FinishingFileResponse(served_path, filename=filename, on_finish=finish)
    except BaseException:
        finish()
        raise


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


def _audit_incomplete_listing(
    session: Session, user: User, source: Source, path: str, exc: _ZipListingIncomplete
) -> None:
    """Records a folder ZIP refused because its walk can't list every file
    (file.download_denied, with the folder that couldn't be listed)."""
    folder_key = "truncated_folder" if exc.reason == "listing_truncated" else "folder"
    _audit_refusal(
        session, user, source, "file.download_denied", path, exc.reason, **{folder_key: exc.path}
    )


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
    file the rules hide is left out once fetched (see _stream_zip).

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


def _skipped_list_name(entries: list[DirEntry], request_root: str) -> str:
    """SKIPPED.txt, unless the folder already has a file by that name at its
    root (compared ignoring case, so extracting on Windows can't collide)."""
    root_names = {_arcname(entry.path, request_root).lower() for entry in entries}
    return "SKIPPED (PerchTail).txt" if "skipped.txt" in root_names else "SKIPPED.txt"


class _FetchBudget:
    """What is left of a folder ZIP's MAX_FETCH_MB budget, counted in bytes
    fetched from the source as each file is fetched: a file may have grown
    since it was listed, and one that takes the ZIP over it ends it."""

    def __init__(self, limit: int):
        self.limit = limit
        self.fetched = 0

    def charge(self, size: int) -> None:
        self.fetched += size
        if self.fetched > self.limit:
            raise FetchTooLargeError(
                "folder is over the MAX_FETCH_MB limit", size=self.fetched, limit=self.limit
            )


class _LateSkip(NamedTuple):
    """Something a folder ZIP left out, found while it was sent: the file on
    the source (`path`), the member of it when only a member was left out,
    and why (a _SKIP_* or archives.SKIP_* reason, or _AUDIT_REFUSAL_REASON)."""

    path: str
    member: str | None
    reason: str


def _write_zip_member(
    zf: zipfile.ZipFile, sink: _ZipSink, content: Path, arcname: str, compress: int
) -> Iterator[bytes]:
    """Writes `content` into the streamed ZIP as `arcname`, handing the
    compressed output on as it fills."""
    info = zipfile.ZipInfo.from_file(content, arcname=arcname)
    info.compress_type = compress
    with content.open("rb") as src, zf.open(info, "w") as dst:
        while chunk := src.read(_ZIP_READ_BYTES):
            dst.write(chunk)
            if sink.pending >= _ZIP_FLUSH_BYTES:
                yield sink.take()
    if sink.pending:
        yield sink.take()


def _stream_zip(
    connector,
    source: Source,
    rules: list[Rule],
    entries: list[DirEntry],
    request_root: str,
    tmp_dir: Path,
    limit: int,
    skipped_name: str,
    record_skipped: Callable[[list[_LateSkip]], None],
) -> Iterator[bytes]:
    """Fetches each of `entries` fresh, one at a time and in order, into
    scratch, and streams it into the ZIP the client is receiving -- same
    always-fresh rule as every other read in this project, just batched into
    one archive. Nothing is fetched before its turn, so the first file goes
    out without waiting for the others. An archive is checked when its turn
    comes, on the copy that is zipped (_archive_has_hidden_members): one
    that holds a file the rules hide is left out, listed last in
    `skipped_name` and, once the ZIP ends however it ends, handed to
    `record_skipped`. Each file is deleted from scratch once it is in the
    ZIP. Arcnames are relative to `request_root`; the bytes fetched of what
    goes into the ZIP count against `limit` (MAX_FETCH_MB, _FetchBudget).

    Once the response has started, a failure can only cut the download
    short (the client sees an incomplete download); it's logged."""
    sink = _ZipSink()
    budget = _FetchBudget(limit)
    skipped: list[str] = []
    late: list[_LateSkip] = []
    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, entry in enumerate(entries):
                arcname = _arcname(entry.path, request_root)
                destination = tmp_dir / f"member-{index}"
                try:
                    connector.fetch_file(source, entry.path, destination, rules=rules)
                    if is_archive(entry.name) and _archive_has_hidden_members(
                        source, entry.path, destination, rules
                    ):
                        logger.warning(
                            "archive.whole_archive_refused", source_id=source.id, path=entry.path
                        )
                        skipped.append(arcname)
                        late.append(_LateSkip(entry.path, None, _AUDIT_REFUSAL_REASON))
                        continue
                    # What goes into the ZIP counts; an archive left out
                    # doesn't use the budget up.
                    budget.charge(destination.stat().st_size)
                    yield from _write_zip_member(
                        zf, sink, destination, arcname, zipfile.ZIP_DEFLATED
                    )
                finally:
                    destination.unlink(missing_ok=True)
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
        if late:
            record_skipped(late)


def _prepare_redacted_entry(
    connector,
    source: Source,
    rules: list[Rule],
    entry: DirEntry,
    original: Path,
    prepared: Path,
    tmp_dir: Path,
    redaction: CompiledRules,
    budget: _FetchBudget,
    agent_rules: list[dict] | None = None,
) -> list[tuple[str, str]] | str:
    """An archive or .gz file of a redacted folder ZIP, when its turn comes:
    fetched into `original` and redacted into `prepared`. An archive is
    rebuilt with only the members the rules don't hide, each redacted
    (_rebuild_redacted); a .gz file is decompressed, redacted and compressed
    again under the same name. Returns the (member, reason) of each member
    an archive left out, or why the whole file is left out. The bytes
    fetched count against `budget` as fetched from the source, not as
    redacted. With `agent_rules`, the agent redacts on its host
    (_prepare_folder_entry_on_agent)."""
    if agent_rules is not None:
        # As stored on the source (the agent refuses more than the limit),
        # as the server counts what it fetched.
        budget.charge(entry.size)
        return _prepare_folder_entry_on_agent(
            connector, source, entry, prepared, rules, agent_rules, tmp_dir
        )
    try:
        connector.fetch_file(source, entry.path, original, rules=rules)
        budget.charge(original.stat().st_size)
        if is_archive(entry.name):
            try:
                return _rebuild_redacted(
                    source, entry.path, original, entry.name, prepared, rules, redaction, tmp_dir
                )
            except _ArchiveUnreadable:
                return _SKIP_UNREADABLE
        with original.open("rb") as src:
            reason = _redact_content(entry.name, src, prepared, redaction, tmp_dir)
        return [] if reason is None else reason
    finally:
        original.unlink(missing_ok=True)


def _prepare_folder_entry_on_agent(
    connector,
    source: Source,
    entry: DirEntry,
    prepared: Path,
    rules: list[Rule],
    agent_rules: list[dict],
    tmp_dir: Path,
) -> list[tuple[str, str]] | str:
    """An archive or .gz file of a redacted folder ZIP when the agent
    redacts on its host, into `prepared`: an archive rebuilt from the
    members the agent sent redacted (_rebuild_on_agent), a .gz file
    decompressed and redacted on the agent and compressed again here.
    Returns the (member, reason) of what an archive left out, or why the
    whole file is left out: as when the server redacts, content redaction
    can't read, past MAX_DECOMPRESSED_MB, or unreadable. Anything else (the
    agent gone, a rule it can't run) fails the ZIP."""
    try:
        if is_archive(entry.name):
            return _rebuild_on_agent(
                connector, source, entry.path, entry.name, prepared, rules, agent_rules, tmp_dir
            )
        plain = tmp_dir / "agent-gz"
        try:
            fetched = connector.call(
                "fetch_redacted",
                source,
                entry.path,
                plain,
                redaction_block(source.id, agent_rules, "source"),
                decompress=True,
                rules=rules,
            )
            if fetched.refused_encoding_at is not None:
                # UTF-32 part way: left out whole, as when the server
                # redacts it (_redact_content).
                return _SKIP_ENCODING
            _gzip_file(plain, prepared)
        finally:
            plain.unlink(missing_ok=True)
        return []
    except _ArchiveUnreadable:
        return _SKIP_UNREADABLE
    except DecompressionLimitError:
        prepared.unlink(missing_ok=True)
        return _SKIP_TOO_LARGE
    except AgentRedactionError as exc:
        prepared.unlink(missing_ok=True)
        if exc.code in _AGENT_FILE_SKIPS:
            return _AGENT_FILE_SKIPS[exc.code]
        raise _agent_refusal(exc, agent_rules) from exc
    except (SourceUnreachableError, AgentUpdateRequiredError):
        raise
    except SourceError as exc:
        # The agent couldn't read it (a corrupt archive or .gz file), as a
        # server-side rebuild leaves out what it can't read.
        prepared.unlink(missing_ok=True)
        logger.warning(
            "archive.redaction_agent_unreadable",
            source_id=source.id,
            path=entry.path,
            error_type=type(exc).__name__,
        )
        return _SKIP_UNREADABLE


def _stream_redacted_zip(
    connector,
    source: Source,
    rules: list[Rule],
    entries: list[DirEntry],
    request_root: str,
    tmp_dir: Path,
    limit: int,
    redaction: CompiledRules,
    skipped_name: str,
    record_skipped: Callable[[list[_LateSkip]], None],
    agent_rules: list[dict] | None = None,
) -> Iterator[bytes]:
    """_stream_zip for a redacted folder ZIP, one file at a time and in
    order, each prepared only when its turn comes. A plain file is fetched
    fresh, redacted into scratch (with `agent_rules`, on the agent, which
    sends it redacted, counted by the bytes of the original it read) and
    written into the ZIP; one redaction can't read (UTF-32, or compressed
    in a format it doesn't support) is left out. An archive or .gz file is
    fetched and redacted or rebuilt (_prepare_redacted_entry) and stored as
    it is, without compressing it again; what it leaves out, or the whole
    file when it can't be redacted, is left out. Each file is deleted from
    scratch once it is in the ZIP. The bytes fetched from the source count
    against `limit` (MAX_FETCH_MB, _FetchBudget). Everything left out is
    listed last, in `skipped_name`, and goes to `record_skipped` (the file
    on the source, the member, and why) once the ZIP ends, however it
    ends."""
    sink = _ZipSink()
    budget = _FetchBudget(limit)
    skipped: list[tuple[str, str]] = []
    late: list[_LateSkip] = []

    def leave_out(arcname: str, path: str, reason: str) -> None:
        logger.warning("archive.redaction_skipped", source_id=source.id, path=path, reason=reason)
        skipped.append((arcname, reason))
        late.append(_LateSkip(path, None, reason))

    try:
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
            for index, entry in enumerate(entries):
                arcname = _arcname(entry.path, request_root)
                original = tmp_dir / f"member-{index}"
                redacted = tmp_dir / f"redacted-{index}"
                try:
                    if is_archive(entry.name) or is_transparent_gzip(entry.name):
                        outcome = _prepare_redacted_entry(
                            connector,
                            source,
                            rules,
                            entry,
                            original,
                            redacted,
                            tmp_dir,
                            redaction,
                            budget,
                            agent_rules,
                        )
                        if isinstance(outcome, str):
                            leave_out(arcname, entry.path, outcome)
                            continue
                        for name, reason in outcome:
                            skipped.append((f"{arcname}/{name}", reason))
                            late.append(_LateSkip(entry.path, name, reason))
                        compress = zipfile.ZIP_STORED
                    else:
                        if agent_rules is not None:
                            fetched, reason = _fetch_redacted_file_on_agent(
                                connector, source, entry.path, redacted, agent_rules, rules
                            )
                            budget.charge(fetched)
                        else:
                            connector.fetch_file(source, entry.path, original, rules=rules)
                            budget.charge(original.stat().st_size)
                            with original.open("rb") as src:
                                reason = _redact_into(src, redacted, redaction, None)
                            original.unlink()
                        if reason is not None:
                            leave_out(arcname, entry.path, reason)
                            continue
                        compress = zipfile.ZIP_DEFLATED
                    yield from _write_zip_member(zf, sink, redacted, arcname, compress)
                finally:
                    original.unlink(missing_ok=True)
                    redacted.unlink(missing_ok=True)
            if skipped:
                zf.writestr(skipped_name, _redaction_skipped_text(skipped))
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
        if late:
            record_skipped(late)


def _fetch_redacted_file_on_agent(
    connector,
    source: Source,
    path: str,
    destination: Path,
    agent_rules: list[dict],
    rules: list[Rule],
) -> tuple[int, str | None]:
    """A plain file of a redacted folder ZIP, redacted on the agent into
    `destination`. Returns the bytes of the original the agent read, and
    None, or why the file is left out (content redaction can't read)."""
    try:
        fetched = connector.call(
            "fetch_redacted",
            source,
            path,
            destination,
            redaction_block(source.id, agent_rules, "source"),
            rules=rules,
        )
    except AgentRedactionError as exc:
        if exc.code in _AGENT_FILE_SKIPS:
            return 0, _AGENT_FILE_SKIPS[exc.code]
        raise _agent_refusal(exc, agent_rules) from exc
    if fetched.refused_encoding_at is not None:
        # UTF-32 part way: left out whole, as when the server redacts it.
        destination.unlink(missing_ok=True)
        return fetched.source_size, _SKIP_ENCODING
    return fetched.source_size, None


# The audit reason of a file, or a member of an archive, a folder ZIP left
# out, by why it was left out; any other reason is recorded as it is
# (archive_hidden_members, and an archive member's encrypted, link and
# unsupported_compression).
_LATE_SKIP_AUDIT_REASONS = {
    _SKIP_ENCODING: _AUDIT_REDACTION_ENCODING_REASON,
    _SKIP_FORMAT: _AUDIT_REDACTION_FORMAT_REASON,
    _SKIP_UNREADABLE: _AUDIT_ARCHIVE_UNREADABLE_REASON,
    _SKIP_TOO_LARGE: "too_large",
    SKIP_NESTED_ARCHIVE: "nested_archive",
}


def _record_late_entries(
    bind, user_id: int, source_id: int, path: str, entries: list[dict], action: str
) -> None:
    """Writes `action` entries after the response started, in their own
    session, since the request's may be gone by then; a failure is logged,
    never raised into the response."""
    try:
        with Session(bind) as late_session:
            for metadata in entries:
                record_audit_event(
                    late_session,
                    user_id=user_id,
                    action=action,
                    target_type="source",
                    target_id=source_id,
                    metadata=metadata,
                )
            late_session.commit()
    except Exception as exc:
        logger.error(
            "archive.late_audit_failed",
            source_id=source_id,
            path=path,
            action=action,
            error_type=type(exc).__name__,
        )


def _late_skips_recorder(
    session: Session, user: User, source: Source, folder: str, action: str
) -> Callable[[list[_LateSkip]], None]:
    """What records what a folder ZIP left out, once it ends, after its
    folder.download entry was written: one `action` entry
    (file.download_denied without redaction,
    file.download_redaction_skipped with it) per file or archive member,
    with its path on the source, the member, the folder of the ZIP and the
    reason."""
    bind = session.get_bind()
    user_id = user.id
    source_id = source.id

    def record_skipped(late: list[_LateSkip]) -> None:
        entries = []
        for item in late:
            metadata: dict = {"path": item.path}
            if item.member is not None:
                metadata["member"] = item.member
            metadata["folder"] = folder
            metadata["reason"] = _LATE_SKIP_AUDIT_REASONS.get(item.reason, item.reason)
            entries.append(metadata)
        _record_late_entries(bind, user_id, source_id, folder, entries, action)

    return record_skipped


def _late_refusal_recorder(
    session: Session, user: User, source: Source, download: dict
) -> Callable[[int], None]:
    """What records a redacted download sent while it is redacted whose
    content turned UTF-32 part way, after its file.download entry was
    written (`download` is that entry's path and member): a second audit
    entry, file.download_redaction_skipped, with the reason and where the
    refused rest started in the original (`offset`)."""
    bind = session.get_bind()
    user_id = user.id
    source_id = source.id
    target = {key: download[key] for key in ("path", "member") if key in download}

    def record(offset: int) -> None:
        _record_late_entries(
            bind,
            user_id,
            source_id,
            target["path"],
            [{**target, "reason": _AUDIT_REDACTION_ENCODING_REASON, "offset": offset}],
            "file.download_redaction_skipped",
        )

    return record


def _download_folder_zip_redacted(
    connector,
    source: Source,
    rules: list[Rule],
    path: str,
    redaction: CompiledRules,
    limits: Limits,
    user: User,
    session: Session,
    agent_rules: list[dict] | None = None,
) -> StreamingResponse:
    """download_folder_zip when the download is redacted (#382, #249): every
    file in the ZIP is redacted when its turn comes (_stream_redacted_zip),
    on the agent with `agent_rules`, what can't be is listed in
    REDACTION-SKIPPED.txt and audited once the ZIP ends
    (file.download_redaction_skipped), and the ZIP says so in
    X-PerchTail-Redacted and, with redaction_download_suffix on, in its
    name."""
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
            _audit_incomplete_listing(session, user, source, path, exc)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=exc.detail) from exc
        listed = sum(entry.size for entry in entries)
        if listed > limit:
            raise _fetch_too_large(
                FetchTooLargeError(
                    "folder is over the MAX_FETCH_MB limit", size=listed, limit=limit
                ),
                folder=True,
            )
        # One file sits in scratch at a time, fetched and redacted (or
        # rebuilt) when its turn comes: the original and what it becomes.
        get_scratch_store().ensure_room(2 * max((entry.size for entry in entries), default=0))
        tmp_dir = make_scratch_dir("perchtail-zip-")
        # Written before the ZIP starts: what it leaves out is only known as
        # it is sent, and is audited once it ends (_late_skips_recorder).
        metadata: dict = {
            "path": path,
            "file_count": len(entries),
            **_redaction_applied(
                REDACTED_ON_AGENT if agent_rules is not None else REDACTED_ON_SERVER
            ),
        }
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

    root_names = {_arcname(entry.path, path) for entry in entries}
    stream = _stream_redacted_zip(
        connector,
        source,
        rules,
        entries,
        path,
        tmp_dir,
        limit,
        redaction,
        _redaction_skipped_name(root_names),
        _late_skips_recorder(session, user, source, path, "file.download_redaction_skipped"),
        agent_rules,
    )

    def finish_stream() -> None:
        finish()
        try:
            stream.close()
        except ValueError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    folder_name = path.rsplit("/", 1)[-1] if path else source.name
    return _FinishingStreamingResponse(
        stream,
        media_type="application/zip",
        headers={
            "Content-Disposition": _attachment(_download_name(session, f"{folder_name}.zip")),
            REDACTED_DOWNLOAD_HEADER: "1",
        },
        on_finish=finish_stream,
    )


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

    The ZIP is streamed as it's built (#345), in order: each file is fetched
    into scratch when its turn comes, written to the response and deleted
    before the next. An archive is checked on the copy that is zipped
    (_stream_zip), and one that holds a file the rules hide is left out,
    listed in SKIPPED.txt and audited once the ZIP ends
    (file.download_denied). The folder's listed total is checked against
    MAX_FETCH_MB before anything is fetched (413). Holds one of the user's
    transfers and a connector
    slot until the client has it all or goes away (#344, 503 with
    Retry-After when none is free).

    When redaction applies and the redaction_downloads setting is on
    (#382, #249), see _download_folder_zip_redacted."""
    _require_safe_path(source, path)
    rules = _rules_for(session, source.id)
    if path and not _dir_visible(source, path, rules):
        raise PathNotFound()
    if source.protocol == Protocol.agent:
        # Refused before anything is listed or sent (audited as
        # access.denied), not cut short once the ZIP has started.
        require_links(source)
    connector = _connector(source)

    limits = current_limits(session)
    redaction = download_redaction_rules_for(session, source)
    if redaction is not None:
        return _download_folder_zip_redacted(
            connector,
            source,
            rules,
            path,
            redaction,
            limits,
            user,
            session,
            agent_redaction_rules(session, source),
        )
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
            _audit_incomplete_listing(session, user, source, path, exc)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=exc.detail) from exc
        listed = sum(entry.size for entry in entries)
        if listed > limit:
            raise _fetch_too_large(
                FetchTooLargeError(
                    "folder is over the MAX_FETCH_MB limit", size=listed, limit=limit
                ),
                folder=True,
            )
        # One file sits in scratch at a time, fetched (and, for an archive,
        # checked) when its turn comes.
        get_scratch_store().ensure_room(max((entry.size for entry in entries), default=0))
        tmp_dir = make_scratch_dir("perchtail-zip-")
        # Written before the ZIP starts: the archives it leaves out are only
        # known as it is sent, and are audited once it ends
        # (_late_skips_recorder).
        metadata: dict = {"path": path, "file_count": len(entries)}
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
        rules,
        entries,
        path,
        tmp_dir,
        limit,
        _skipped_list_name(entries, path),
        _late_skips_recorder(session, user, source, path, "file.download_denied"),
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
