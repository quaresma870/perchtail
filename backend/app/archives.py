import gzip
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

_TAR_SUFFIXES = (".tar.gz", ".tgz")
_CHUNK = 1024 * 1024


class DecompressionLimitError(ValueError):
    """Decompressed output passed the configured cap -- a guard against
    zip/gzip bombs filling the scratch disk from a tiny compressed file."""


def _bounded_copy(src: BinaryIO, destination: Path, max_bytes: int | None) -> None:
    written = 0
    try:
        with open(destination, "wb") as dst:
            while chunk := src.read(_CHUNK):
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise DecompressionLimitError(
                        f"decompressed size exceeds the {max_bytes}-byte limit"
                    )
                dst.write(chunk)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


@dataclass(frozen=True)
class ArchiveMember:
    name: str
    size: int
    is_dir: bool


def is_archive(filename: str) -> bool:
    """Zip/tar.gz are virtual folders in the tree (CLAUDE.md's "Live
    browsing & ephemeral fetch behavior") — expanding one lists its members
    rather than opening it directly."""
    return filename.endswith(".zip") or filename.endswith(_TAR_SUFFIXES)


def is_transparent_gzip(filename: str) -> bool:
    """A plain single-file .gz decompresses transparently and opens like any
    other file — unlike .zip/.tar.gz, it is not a virtual folder."""
    return filename.endswith(".gz") and not filename.endswith(_TAR_SUFFIXES)


def list_members(local_path: Path, filename: str) -> list[ArchiveMember]:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(local_path) as zf:
            return [
                ArchiveMember(name=info.filename, size=info.file_size, is_dir=info.is_dir())
                for info in zf.infolist()
            ]
    if filename.endswith(_TAR_SUFFIXES):
        with tarfile.open(local_path, mode="r:gz") as tf:
            return [
                ArchiveMember(name=member.name, size=member.size, is_dir=member.isdir())
                for member in tf.getmembers()
            ]
    raise ValueError(f"Not a supported archive: {filename}")


def extract_member(
    local_path: Path,
    filename: str,
    member_name: str,
    destination: Path,
    max_bytes: int | None = None,
) -> None:
    """Decompresses and fetches just the requested member — same
    always-fresh, purge-on-close rules apply to it as to anything else.
    The cap counts bytes actually produced, not the size the archive
    header claims, since a crafted header can lie."""
    if filename.endswith(".zip"):
        with zipfile.ZipFile(local_path) as zf, zf.open(member_name) as src:
            _bounded_copy(src, destination, max_bytes)
        return
    if filename.endswith(_TAR_SUFFIXES):
        with tarfile.open(local_path, mode="r:gz") as tf:
            member = tf.getmember(member_name)
            src = tf.extractfile(member)
            if src is None:
                raise ValueError(f"{member_name} is not a regular file")
            with src:
                _bounded_copy(src, destination, max_bytes)
        return
    raise ValueError(f"Not a supported archive: {filename}")


def decompress_gzip(local_path: Path, destination: Path, max_bytes: int | None = None) -> None:
    with gzip.open(local_path, "rb") as src:
        _bounded_copy(src, destination, max_bytes)
