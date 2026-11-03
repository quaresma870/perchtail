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


class FetchTooLargeError(ValueError):
    """A file (or a folder ZIP's total) is over MAX_FETCH_MB -- refused
    rather than filling scratch disk or memory with it."""


def max_fetch_bytes() -> int:
    return int(get_settings().max_fetch_mb * 1024**2)


def too_large(size: int, limit: int) -> FetchTooLargeError:
    return FetchTooLargeError(f"{size} bytes is over the {limit}-byte MAX_FETCH_MB limit")


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
