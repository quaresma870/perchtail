from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import get_settings

_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class DirEntry:
    name: str
    path: str
    is_dir: bool
    size: int


class SourceError(Exception):
    """A predictable failure on the source side, with a status and a message
    that are safe to show anyone who can view the source. The underlying
    error text (UNC paths, library internals, host keys) stays in `detail`,
    which only goes to the server log and to source managers."""

    status_code = 502
    public_message = "The source returned an error"

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


class SourceLockedError(SourceError):
    status_code = 409
    public_message = "The file is locked by another process on the source"


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
