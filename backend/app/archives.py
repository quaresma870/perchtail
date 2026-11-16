import gzip
import io
import shutil
import tarfile
import zipfile
from collections.abc import Callable
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


def list_members(
    local_path: Path, filename: str, *, include_special: bool = False
) -> list[ArchiveMember]:
    """The archive's members. A tar's links, devices and FIFOs are left out
    (see _is_plain_tar_member) unless `include_special` is set, for checks
    that must see every entry's name; they then list as files."""
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
                if include_special or _is_plain_tar_member(member)
            ]
    raise ValueError(f"Not a supported archive: {filename}")


def _is_plain_tar_member(member: tarfile.TarInfo) -> bool:
    """Only regular files and directories are members. A symbolic or hard
    link would open whatever member it points at -- possibly one the rules
    hide -- under its own, visible name; devices and FIFOs have no content
    to show. So they are neither listed nor opened."""
    return member.isfile() or member.isdir()


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
    header claims, since a crafted header can lie. A member that doesn't
    exist, or a tar member that isn't a regular file (a link, a device),
    raises KeyError: as far as the caller knows, it isn't there. Zip
    members are always read as stored -- zipfile never follows a link."""
    if _is_zip(filename):
        with zipfile.ZipFile(local_path) as zf, zf.open(member_name) as src:
            _bounded_copy(src, destination, max_bytes)
        return
    if _is_tar_gz(filename):
        with tarfile.open(local_path, mode="r:gz") as tf:
            member = tf.getmember(member_name)
            if not member.isfile():
                raise KeyError(member_name)
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


# Why rebuild_archive left a member out (besides the ones `keep` refuses,
# which are left out without a trace).
SKIP_NESTED_ARCHIVE = "nested_archive"
SKIP_ENCRYPTED = "encrypted"
SKIP_LINK = "link"
SKIP_UNSUPPORTED_COMPRESSION = "unsupported_compression"

# Rewrites one member: reads its content from the stream, writes the new
# content to the path, and returns None, or why the member is left out
# instead (nothing is then written).
MemberTransform = Callable[[str, BinaryIO, Path], str | None]
# The list of what was left out, as (file name, content), written into the
# rebuilt archive; called with the left-out (name, reason) pairs and the
# names already in the archive, only when something was left out.
SkippedList = Callable[[list[tuple[str, str]], set[str]], tuple[str, bytes]]

# Zip's "encrypted" general-purpose flag.
_ZIP_ENCRYPTED = 0x1
# The Unix file type bits of a zip member's external attributes, and the
# type of a symbolic link.
_S_IFMT = 0o170000
_S_IFLNK = 0o120000


def _is_zip_link(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & _S_IFMT == _S_IFLNK


def rebuild_archive(
    local_path: Path,
    filename: str,
    destination: Path,
    work_dir: Path,
    *,
    keep: Callable[[str], bool],
    transform: MemberTransform,
    skipped_list: SkippedList,
) -> list[tuple[str, str]]:
    """Writes to `destination` a new archive in the same format as the one at
    `local_path` (zip, or tar.gz): its regular files that `keep` accepts,
    each rewritten by `transform`, under their normalized names
    (normalize_member_name) and with their modification times. Folders, a
    tar's links, devices and FIFOs, and members `keep` refuses are left out
    without a trace; a zip's symbolic links, encrypted zip members, zip
    members compressed with a method zipfile can't read, archives inside the
    archive and members `transform` turns down are left out and listed in
    the file `skipped_list` gives. Each member passes through `work_dir` one
    at a time. Returns the (name, reason) of each member listed."""
    if _is_zip(filename):
        rebuild = _rebuild_zip
    elif _is_tar_gz(filename):
        rebuild = _rebuild_tar_gz
    else:
        raise ValueError(f"Not a supported archive: {filename}")
    try:
        return rebuild(local_path, destination, work_dir, keep, transform, skipped_list)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


# The earliest and latest dates a zip member can carry (MS-DOS time).
_ZIP_EARLIEST = (1980, 1, 1, 0, 0, 0)
_ZIP_LATEST = (2107, 12, 31, 23, 59, 58)


def _zip_date_time(mtime: float | None) -> tuple[int, int, int, int, int, int]:
    """A modification time (seconds since the epoch, as list_members gives
    it, a zip's taken as UTC) as a zip member's date and time."""
    if mtime is None:
        return _ZIP_EARLIEST
    try:
        moment = datetime.fromtimestamp(mtime, UTC)
    except (OverflowError, OSError, ValueError):
        return _ZIP_EARLIEST
    stamp = (moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)
    return min(max(stamp, _ZIP_EARLIEST), _ZIP_LATEST)


def write_archive(
    filename: str,
    destination: Path,
    members: list[tuple[str, Path, float | None]],
    skipped: list[tuple[str, str]],
    skipped_list: SkippedList,
) -> None:
    """Writes to `destination` a new archive in the format `filename` names
    (zip, or tar.gz) holding `members`, each (name, file holding its
    content, modification time in seconds or None), in order, and, when
    `skipped` isn't empty, the list `skipped_list` gives for it. For an
    archive whose members were read and redacted elsewhere (an agent's
    fetch_archive), as rebuild_archive writes one read here."""
    try:
        if _is_zip(filename):
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as rebuilt:
                written: set[str] = set()
                for name, path, mtime in members:
                    out = zipfile.ZipInfo(name, date_time=_zip_date_time(mtime))
                    out.compress_type = zipfile.ZIP_DEFLATED
                    out.external_attr = 0o644 << 16
                    out.file_size = path.stat().st_size
                    with path.open("rb") as src, rebuilt.open(out, "w") as dst:
                        shutil.copyfileobj(src, dst, _CHUNK)
                    written.add(name)
                if skipped:
                    list_name, content = skipped_list(skipped, written)
                    rebuilt.writestr(list_name, content)
            return
        if _is_tar_gz(filename):
            with tarfile.open(destination, mode="w:gz") as rebuilt:
                written = set()
                for name, path, mtime in members:
                    out = tarfile.TarInfo(name)
                    out.size = path.stat().st_size
                    out.mtime = int(mtime) if mtime else 0
                    out.mode = 0o644
                    with path.open("rb") as src:
                        rebuilt.addfile(out, src)
                    written.add(name)
                if skipped:
                    list_name, content = skipped_list(skipped, written)
                    out = tarfile.TarInfo(list_name)
                    out.size = len(content)
                    out.mtime = int(datetime.now(UTC).timestamp())
                    out.mode = 0o644
                    rebuilt.addfile(out, io.BytesIO(content))
            return
        raise ValueError(f"Not a supported archive: {filename}")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def _skip_reason(name: str) -> str | None:
    return SKIP_NESTED_ARCHIVE if is_archive(name.rsplit("/", 1)[-1]) else None


def _rebuild_zip(
    local_path: Path,
    destination: Path,
    work_dir: Path,
    keep: Callable[[str], bool],
    transform: MemberTransform,
    skipped_list: SkippedList,
) -> list[tuple[str, str]]:
    skipped: list[tuple[str, str]] = []
    written: set[str] = set()
    member_copy = work_dir / "rebuild-member"
    with (
        zipfile.ZipFile(local_path) as source,
        zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as rebuilt,
    ):
        for info in source.infolist():
            name = normalize_member_name(info.filename)
            if info.is_dir() or not name or not keep(name):
                continue
            reason = SKIP_LINK if _is_zip_link(info) else _skip_reason(name)
            if reason is None and info.flag_bits & _ZIP_ENCRYPTED:
                reason = SKIP_ENCRYPTED
            if reason is None:
                try:
                    try:
                        src = source.open(info)
                    except NotImplementedError:
                        # A compression method zipfile doesn't support
                        # (Deflate64, PPMd...): the member can't be read.
                        src = None
                        reason = SKIP_UNSUPPORTED_COMPRESSION
                    if src is not None:
                        with src:
                            reason = transform(name, src, member_copy)
                    if reason is None:
                        out = zipfile.ZipInfo(name, date_time=info.date_time)
                        out.compress_type = zipfile.ZIP_DEFLATED
                        out.external_attr = info.external_attr
                        out.file_size = member_copy.stat().st_size
                        with member_copy.open("rb") as src, rebuilt.open(out, "w") as dst:
                            shutil.copyfileobj(src, dst, _CHUNK)
                        written.add(name)
                finally:
                    member_copy.unlink(missing_ok=True)
            if reason is not None:
                skipped.append((name, reason))
        if skipped:
            list_name, content = skipped_list(skipped, written)
            rebuilt.writestr(list_name, content)
    return skipped


def _rebuild_tar_gz(
    local_path: Path,
    destination: Path,
    work_dir: Path,
    keep: Callable[[str], bool],
    transform: MemberTransform,
    skipped_list: SkippedList,
) -> list[tuple[str, str]]:
    skipped: list[tuple[str, str]] = []
    written: set[str] = set()
    member_copy = work_dir / "rebuild-member"
    with (
        tarfile.open(local_path, mode="r:gz") as source,
        tarfile.open(destination, mode="w:gz") as rebuilt,
    ):
        for member in source:
            name = normalize_member_name(member.name)
            if not member.isfile() or not name or not keep(name):
                continue
            reason = _skip_reason(name)
            if reason is None:
                try:
                    src = source.extractfile(member)
                    if src is None:
                        continue
                    with src:
                        reason = transform(name, src, member_copy)
                    if reason is None:
                        out = tarfile.TarInfo(name)
                        out.size = member_copy.stat().st_size
                        out.mtime = member.mtime
                        out.mode = member.mode & 0o777
                        with member_copy.open("rb") as src:
                            rebuilt.addfile(out, src)
                        written.add(name)
                finally:
                    member_copy.unlink(missing_ok=True)
            if reason is not None:
                skipped.append((name, reason))
        if skipped:
            list_name, content = skipped_list(skipped, written)
            out = tarfile.TarInfo(list_name)
            out.size = len(content)
            out.mtime = int(datetime.now(UTC).timestamp())
            out.mode = 0o644
            rebuilt.addfile(out, io.BytesIO(content))
    return skipped
