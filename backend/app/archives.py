import gzip
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
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
    # Seconds since the epoch, as the archive records it (#273).
    mtime: float | None = None


def _zip_mtime(date_time: tuple) -> float | None:
    """A zip member's DOS timestamp (no time zone: taken as UTC) in
    seconds since the epoch."""
    try:
        return datetime(*date_time, tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def _is_zip(filename: str) -> bool:
    return filename.lower().endswith(".zip")


def _is_tar_gz(filename: str) -> bool:
    return filename.lower().endswith(_TAR_SUFFIXES)


def is_archive(filename: str) -> bool:
    """Zip/tar.gz are virtual folders in the tree (CLAUDE.md's "Live
    browsing & ephemeral fetch behavior") — expanding one lists its members
    rather than opening it directly. The extension is matched whatever its
    case (`OLD.ZIP`)."""
    return _is_zip(filename) or _is_tar_gz(filename)


def is_transparent_gzip(filename: str) -> bool:
    """A plain single-file .gz decompresses transparently and opens like any
    other file — unlike .zip/.tar.gz, it is not a virtual folder. The
    extension is matched whatever its case (`app.log.GZ`)."""
    return filename.lower().endswith(".gz") and not _is_tar_gz(filename)


def normalize_member_name(name: str) -> str:
    """The name a member is shown and opened under: `/`-separated, without
    leading `/`, `./` or `..` segments. A crafted archive can name members
    `../../evil` or `/etc/x`; those are only ever *displayed* under their
    normalized name -- extraction streams the member's bytes and never
    writes to a path derived from its name."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "/".join(parts)


def tree_children(members: list[ArchiveMember], member_dir: str) -> list[ArchiveMember]:
    """The direct children of `member_dir` inside an archive ("" = the
    archive's root), as a folder tree: members deeper down are represented
    by a synthesised directory entry for their next path segment, whether
    or not the archive lists that directory explicitly. Names are
    normalized (normalize_member_name)."""
    prefix = f"{member_dir}/" if member_dir else ""
    children: dict[str, ArchiveMember] = {}
    for member in members:
        name = normalize_member_name(member.name)
        if not name or not name.startswith(prefix) or name == member_dir:
            continue
        rest = name[len(prefix) :]
        head, _, tail = rest.partition("/")
        child = f"{prefix}{head}"
        if tail or member.is_dir:
            children.setdefault(child, ArchiveMember(name=child, size=0, is_dir=True))
        else:
            children[child] = ArchiveMember(
                name=child, size=member.size, is_dir=False, mtime=member.mtime
            )
    return sorted(children.values(), key=lambda m: (not m.is_dir, m.name.lower()))


def resolve_member_name(local_path: Path, filename: str, requested: str) -> str:
    """Maps a (normalized) member name from the tree back to the archive's
    own member name, so members with odd raw names can still be opened."""
    for member in list_members(local_path, filename):
        if member.name == requested:
            return member.name
    for member in list_members(local_path, filename):
        if not member.is_dir and normalize_member_name(member.name) == requested:
            return member.name
    return requested


def list_members(local_path: Path, filename: str) -> list[ArchiveMember]:
    if _is_zip(filename):
        with zipfile.ZipFile(local_path) as zf:
            return [
                ArchiveMember(
                    name=info.filename,
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    mtime=_zip_mtime(info.date_time),
                )
                for info in zf.infolist()
            ]
    if _is_tar_gz(filename):
        with tarfile.open(local_path, mode="r:gz") as tf:
            return [
                ArchiveMember(
                    name=member.name,
                    size=member.size,
                    is_dir=member.isdir(),
                    mtime=float(member.mtime) if member.mtime else None,
                )
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
    if _is_zip(filename):
        with zipfile.ZipFile(local_path) as zf, zf.open(member_name) as src:
            _bounded_copy(src, destination, max_bytes)
        return
    if _is_tar_gz(filename):
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
