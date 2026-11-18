from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, NamedTuple

from app.config import get_settings

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class DirEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    # Last modification, as seconds since the epoch (#273); None when the
    # source doesn't say (an agent older than 0.6).
    mtime: float | None = None
    # A directory's identity on the source (its resolved real path, or
    # device:inode), when the connector knows it: recursive walks keep the
    # ones they've listed, so a symlink loop ends (#347). Not compared.
    real_key: str | None = field(default=None, compare=False)
    # Where the entry really is, relative to the base path, when a link
    # (the entry itself or a folder above it) leads somewhere else; None
    # when it is where its path says. Rules are checked on both.
    target: str | None = field(default=None, compare=False)


class DirListing(list):
    """What list_directory returns when the source cut the listing short
    (an agent caps a huge folder, #347): a list of DirEntry with
    `truncated` set. `real_dir` is where the listed folder really is,
    relative to the base path, when a link leads there (None otherwise)."""

    truncated: bool = False
    real_dir: str | None = None


def is_truncated(entries) -> bool:
    return bool(getattr(entries, "truncated", False))


# The most entries a folder or archive listing returns (#347); the response
# says when there were more.
MAX_LISTING_ENTRIES = 5000


class SourceError(Exception):
    """A predictable failure on the source side, with a status and a message
    that are safe to show anyone who can view the source. The underlying
    error text (UNC paths, library internals, host keys) stays in `detail`,
    which only goes to the server log and to source managers."""

    status_code = 502
    public_message = "The source returned an error"
    # Optional, machine-readable, sent next to `detail`.
    error_code: str | None = None

    def __init__(self, detail: str = "", public_message: str | None = None):
        super().__init__(detail or public_message or self.public_message)
        self.detail = detail
        if public_message:
            self.public_message = public_message


class SourcePermissionError(SourceError):
    status_code = 403
    public_message = "The source account has no read permission for this path"


class SourceNotFoundError(SourceError):
    status_code = 404
    public_message = "Not found on the source"
    # Lets the viewer say "no longer exists" and drop it from the tree
    # (#272). Only reached once the rules allow the path: a hidden one
    # answers a plain "not found".
    error_code = "not_found_on_source"


class SourceLockedError(SourceError):
    status_code = 409
    public_message = "The file is locked by another process on the source"


class SourceArchiveUnreadableError(SourceError):
    """An agent couldn't read the archive or the .gz file it was asked to
    list or read from (its codes archive_unreadable and gzip_unreadable,
    `gzip` for the second). The archive routes answer it as they answer one
    the server can't read (409 archive_unreadable, audited)."""

    status_code = 409
    public_message = "This archive or compressed file can't be read"
    error_code = "archive_unreadable"

    def __init__(self, detail: str = "", *, gzip: bool = False):
        super().__init__(detail)
        self.gzip = gzip


class SourceMemberUnreadableError(SourceError):
    """An agent found the archive member it was asked for stored encrypted
    (its code member_encrypted). The archive routes answer it as they answer
    one the server finds itself (409 archive_member_unreadable, audited
    with the reason member_encrypted)."""

    status_code = 409
    public_message = "This archive member can't be opened"
    error_code = "archive_member_unreadable"

    def __init__(self, detail: str = "", *, reason: str):
        super().__init__(detail)
        # app.archives' SKIP_ENCRYPTED.
        self.reason = reason


class SourceUnreachableError(SourceError):
    status_code = 502
    public_message = "Can't reach the source"


class SourceAuthError(SourceError):
    status_code = 502
    public_message = "Authentication to the source failed"


class SourceHostKeyError(SourceError):
    status_code = 502
    public_message = (
        "The server's SSH host key doesn't match the pinned one -- refusing to connect "
        "(possible man-in-the-middle). An administrator must verify and re-pin it."
    )


class SourceNoCredentialError(SourceAuthError):
    """The source has no saved credential yet (e.g. imported without one).
    Raised before connecting, instead of a raw error from decrypting None
    (#206)."""

    public_message = "This source has no credential yet: set one in its settings"


def require_credential(source) -> None:
    """Raises SourceNoCredentialError for an SSH/SMB/WinRM source with no
    saved credential, before anything tries to decrypt None."""
    if not source.credential_ref:
        raise SourceNoCredentialError("source has no credential")


class SourceTLSError(SourceError):
    status_code = 502
    public_message = "The source's TLS certificate isn't trusted"


class PathOutsideBaseError(PermissionError):
    """A requested path resolves (on the source host, symlinks included) to
    somewhere outside the source's base path. Answered as 404, the same as
    a path the rules hide, so it reveals nothing about what's out there."""


# The audit reason of a path refused as PathOutsideBaseError: a link that
# leads outside the base path or to something the rules hide.
LINK_TARGET_HIDDEN = "link_target_hidden"


class LinkTargetHiddenError(PathOutsideBaseError):
    """A requested path reaches its file or folder through a link (a
    symlink, itself or a folder above it) whose target is outside the base
    path or hidden by the rules. Answered as 404 like PathOutsideBaseError;
    the endpoints that serve content audit it (`link_target_hidden`)."""


# Called with where a path really is, relative to the base path, when a link
# leads there: whether the rules let it be read. None means no link may be
# followed at all (fail closed).
TargetCheck = Callable[[str], bool] | None


def require_target(requested: str, real: str, check: TargetCheck) -> None:
    """Refuses `requested` when it really is at `real` (a link leads there)
    and `check` doesn't allow that: rules are checked on the target, not only
    on the link's name. Nothing to check when the path is where it says."""
    if real == requested:
        return
    if check is None or not check(real):
        raise LinkTargetHiddenError(requested)


class FetchTooLargeError(ValueError):
    """A file (or a folder ZIP's total) is over MAX_FETCH_MB -- refused
    rather than filling scratch disk or memory with it. `size` and `limit`
    (bytes) are set when known, so the client can say by how much."""

    def __init__(self, message: str, *, size: int | None = None, limit: int | None = None):
        super().__init__(message)
        self.size = size
        self.limit = limit


def max_fetch_bytes() -> int:
    return int(get_settings().max_fetch_mb * 1024**2)


def too_large(size: int, limit: int) -> FetchTooLargeError:
    return FetchTooLargeError(
        f"{size} bytes is over the {limit}-byte MAX_FETCH_MB limit", size=size, limit=limit
    )


def copy_with_limit(src: BinaryIO, destination: Path, limit: int) -> int:
    """Streams src into destination in chunks, refusing to write more than
    `limit` bytes (the file may have grown since it was listed). Deletes the
    partial file on any failure. Returns the number of bytes written."""
    written = 0
    try:
        with open(destination, "wb") as dst:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if written > limit:
                    raise too_large(written, limit)
                dst.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return written


# Incremental follow (#254): each connector's read_range(source, path,
# offset, length, head) returns a RangeRead -- the file's current size, its
# first `head` bytes and up to `length` bytes from `offset` -- in one round
# trip, without fetching the rest of the file. A poll reads at
# most this much; the agent's own cap is the same (agent/internal/agent).
RANGE_MAX_BYTES = 1 << 20


class RangeRead(NamedTuple):
    """What read_range returns: the file's size now, its first `head` bytes
    (to tell a rotated file from the one the viewer has), and up to
    `length` bytes from `offset`."""

    size: int
    head: bytes
    data: bytes


# The start of the file read along with the range, to identify it.
RANGE_MAX_HEAD_BYTES = 4096


def read_range_from(f: BinaryIO, size: int, offset: int, length: int, head: int) -> RangeRead:
    """The shared tail of read_range for file objects that can seek."""
    head_bytes = b""
    if head > 0 and size > 0:
        f.seek(0)
        head_bytes = f.read(min(head, size, RANGE_MAX_HEAD_BYTES))
    data = b""
    if offset < size and length > 0:
        f.seek(offset)
        data = f.read(min(length, size - offset, RANGE_MAX_BYTES))
    return RangeRead(size, head_bytes, data)
