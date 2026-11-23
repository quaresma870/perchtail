import gzip
import io
import shutil
import tarfile
import zipfile
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

_TAR_SUFFIXES = (".tar.gz", ".tgz")
_CHUNK = 1024 * 1024

# A tar.gz is read front to back once, entry by entry, and never whole into
# memory: at most MAX_TAR_ENTRIES entries (members of any type) and
# MAX_TAR_NAME_BYTES of their names are read, at most MAX_TAR_HEADER_BYTES
# of headers for any one entry (its own header block and the long names,
# pax headers and sparse blocks before or after it), and at most
# MAX_TAR_GLOBAL_HEADER_BYTES of global pax headers in the whole pass
# (tarfile keeps those until the end). Past the first two, the rest of the
# archive is left unread (a listing says it was cut short); past the other
# two, the archive can't be read.
MAX_TAR_ENTRIES = 50_000
MAX_TAR_NAME_BYTES = 8 * 1024 * 1024
MAX_TAR_HEADER_BYTES = 1024 * 1024
MAX_TAR_GLOBAL_HEADER_BYTES = 1024 * 1024
# What a corrupt gzip stream raises while it is read, and what tarfile
# raises on headers chained deeper than Python's recursion limit.
_GZIP_ERRORS = (gzip.BadGzipFile, EOFError, zlib.error)
_TAR_READ_ERRORS = (*_GZIP_ERRORS, RecursionError)


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


class MemberUnreadableError(Exception):
    """An archive member that can't be extracted: encrypted, or compressed
    with a method zipfile can't read. `reason` is SKIP_ENCRYPTED or
    SKIP_UNSUPPORTED_COMPRESSION."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


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


def is_zip(filename: str) -> bool:
    """A `.zip`, matched whatever the case: one that can be listed and read
    in place from a seekable file (api/archive.py's ranged reads)."""
    return filename.lower().endswith(".zip")


def _is_tar_gz(filename: str) -> bool:
    return filename.lower().endswith(_TAR_SUFFIXES)


def is_archive(filename: str) -> bool:
    """Zip/tar.gz are virtual folders in the tree (CLAUDE.md's "Live
    browsing & ephemeral fetch behavior") — expanding one lists its members
    rather than opening it directly. The extension is matched whatever its
    case (`OLD.ZIP`)."""
    return is_zip(filename) or _is_tar_gz(filename)


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


def _zip_member_stretch(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[int, int]:
    """Where a member's local header and data lie in the archive: from its
    header to the next member's header, or to the central directory after
    the last one. Only bounds what is read ahead; zipfile reads what it
    needs either way."""
    start = info.header_offset
    end = zf.start_dir
    for other in zf.infolist():
        if start < other.header_offset < end:
            end = other.header_offset
    return start, max(end, start)


def _zip_member_name(zf: zipfile.ZipFile, requested: str) -> str:
    """Maps a (normalized) member name from the tree back to the zip's own
    member name, so members with odd raw names can still be opened."""
    infos = zf.infolist()
    for info in infos:
        if info.filename == requested:
            return info.filename
    for info in infos:
        if not info.is_dir() and normalize_member_name(info.filename) == requested:
            return info.filename
    return requested


class _ForwardReader:
    """The decompressed content of a tar.gz for tarfile, which can only move
    forward through it: a seek back raises instead of decompressing the
    stream again from its start. While `headers` is set (tarfile reading the
    next entry's headers), reads that would take the entry's headers past
    MAX_TAR_HEADER_BYTES raise instead of taking that much memory;
    `start_entry` starts counting again. `global_header_bytes` counts the
    global pax headers of the whole pass (_BoundedTarInfo)."""

    def __init__(self, gz: gzip.GzipFile):
        self._gz = gz
        self.headers = True
        self._entry_header_bytes = 0
        self.global_header_bytes = 0

    def start_entry(self) -> None:
        self.headers = True
        self._entry_header_bytes = 0

    def read(self, size: int = -1) -> bytes:
        if self.headers:
            if size is None or size < 0:
                raise tarfile.ReadError("tar header too large")
            self._entry_header_bytes += size
            if self._entry_header_bytes > MAX_TAR_HEADER_BYTES:
                raise tarfile.ReadError("tar header too large")
        return self._gz.read(size)

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence != io.SEEK_SET or offset < self._gz.tell():
            raise tarfile.ReadError("a tar.gz is only read forward")
        return self._gz.seek(offset)

    def tell(self) -> int:
        return self._gz.tell()


class _BoundedTarInfo(tarfile.TarInfo):
    """A TarInfo that counts each global pax header against
    MAX_TAR_GLOBAL_HEADER_BYTES before reading it: tarfile adds every global
    header to `TarFile.pax_headers` and keeps them for the whole pass, so a
    limit on one entry's headers doesn't bound them."""

    def _proc_pax(self, tarfile_: tarfile.TarFile) -> tarfile.TarInfo:  # type: ignore[override]
        if self.type == tarfile.XGLTYPE:
            reader = tarfile_.fileobj
            if isinstance(reader, _ForwardReader):
                reader.global_header_bytes += self.size
                if reader.global_header_bytes > MAX_TAR_GLOBAL_HEADER_BYTES:
                    raise tarfile.ReadError("tar global headers too large")
        return super()._proc_pax(tarfile_)  # type: ignore[misc]


class _TarScan:
    """One pass through a tar.gz: iterating gives its entries in order,
    without keeping them (tarfile would otherwise hold every entry it has
    read), and `extractfile` reads the content of the entry just given,
    before the next one is asked for. Stops after MAX_TAR_ENTRIES entries or
    MAX_TAR_NAME_BYTES of names, and then sets `truncated`."""

    def __init__(self, local_path: Path):
        self._local_path = local_path
        self.truncated = False

    def __enter__(self) -> "_TarScan":
        self._gz = gzip.GzipFile(self._local_path, "rb")
        self._reader = _ForwardReader(self._gz)
        try:
            # Reads the first entry's headers.
            self._tf = tarfile.TarFile(
                fileobj=self._reader, mode="r", tarinfo=_BoundedTarInfo  # type: ignore[arg-type]
            )
        except _TAR_READ_ERRORS as exc:
            self._gz.close()
            raise tarfile.ReadError(f"not a readable tar.gz: {exc}") from exc
        except BaseException:
            self._gz.close()
            raise
        return self

    def __exit__(self, *exc_info) -> None:
        self._tf.close()
        self._gz.close()

    def __iter__(self) -> Iterator[tarfile.TarInfo]:
        entries = 0
        name_bytes = 0
        while True:
            self._reader.start_entry()
            try:
                member = self._tf.next()
            except _TAR_READ_ERRORS as exc:
                raise tarfile.ReadError(f"not a readable tar.gz: {exc}") from exc
            finally:
                self._reader.headers = False
            self._tf.members.clear()
            if member is None:
                return
            entries += 1
            name_bytes += len(member.name)
            if entries > MAX_TAR_ENTRIES or name_bytes > MAX_TAR_NAME_BYTES:
                self.truncated = True
                return
            yield member

    def extractfile(self, member: tarfile.TarInfo) -> BinaryIO | None:
        return self._tf.extractfile(member)  # type: ignore[return-value]


def scan_members(
    local_path: Path | BinaryIO, filename: str, *, include_special: bool = False
) -> tuple[list[ArchiveMember], bool]:
    """The archive's members, and whether the list was cut short (a tar.gz
    past MAX_TAR_ENTRIES entries or MAX_TAR_NAME_BYTES of names, read in one
    pass, see _TarScan). A tar's links, devices and FIFOs are left out (see
    _is_plain_tar_member) unless `include_special` is set, for checks that
    must see every entry's name; they then list as files. A zip can be
    given as a seekable file instead of a path (a ranged read of a remote
    one, collectors/ranged.py); a tar.gz only as a path."""
    if is_zip(filename):
        with zipfile.ZipFile(local_path) as zf:
            members = [
                ArchiveMember(
                    name=info.filename,
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    mtime=_zip_mtime(info.date_time),
                )
                for info in zf.infolist()
            ]
        return members, False
    if _is_tar_gz(filename):
        with _TarScan(local_path) as scan:
            members = [
                ArchiveMember(
                    name=member.name,
                    size=member.size,
                    is_dir=member.isdir(),
                    mtime=float(member.mtime) if member.mtime else None,
                )
                for member in scan
                if include_special or _is_plain_tar_member(member)
            ]
        return members, scan.truncated
    raise ValueError(f"Not a supported archive: {filename}")


def list_members(
    local_path: Path, filename: str, *, include_special: bool = False
) -> list[ArchiveMember]:
    """scan_members' members, without saying whether the list was cut."""
    return scan_members(local_path, filename, include_special=include_special)[0]


def _is_plain_tar_member(member: tarfile.TarInfo) -> bool:
    """Only regular files and directories are members. A symbolic or hard
    link would open whatever member it points at -- possibly one the rules
    hide -- under its own, visible name; devices and FIFOs have no content
    to show. So they are neither listed nor opened."""
    return member.isfile() or member.isdir()


def extract_member(
    local_path: Path | BinaryIO,
    filename: str,
    member_name: str,
    destination: Path,
    max_bytes: int | None = None,
) -> None:
    """Decompresses and fetches just the requested member — same
    always-fresh, purge-on-close rules apply to it as to anything else.
    `member_name` is the member's own name or, when no member has that name,
    its normalized one (normalize_member_name), as the tree shows it.
    The cap counts bytes actually produced, not the size the archive
    header claims, since a crafted header can lie. A member that doesn't
    exist, or a tar member that isn't a regular file (a link, a device),
    raises KeyError: as far as the caller knows, it isn't there. An
    encrypted zip member, or one compressed with a method zipfile can't
    read, raises MemberUnreadableError. Zip members are always read as
    stored -- zipfile never follows a link. A tar.gz is read in one pass
    (_extract_tar_member). A zip can be given as a seekable file (see
    scan_members); one that can read ahead (collectors/ranged.py) is told
    the member's stretch first."""
    if is_zip(filename):
        with zipfile.ZipFile(local_path) as zf:
            info = zf.getinfo(_zip_member_name(zf, member_name))
            if info.flag_bits & _ZIP_ENCRYPTED:
                raise MemberUnreadableError(SKIP_ENCRYPTED)
            read_sequentially = getattr(local_path, "read_sequentially", None)
            if read_sequentially is not None:
                read_sequentially(*_zip_member_stretch(zf, info))
            try:
                src = zf.open(info)
            except NotImplementedError as exc:
                raise MemberUnreadableError(SKIP_UNSUPPORTED_COMPRESSION) from exc
            with src:
                _bounded_copy(src, destination, max_bytes)
        return
    if _is_tar_gz(filename):
        try:
            _extract_tar_member(local_path, member_name, destination, max_bytes)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        return
    raise ValueError(f"Not a supported archive: {filename}")


def _extract_tar_member(
    local_path: Path, requested: str, destination: Path, max_bytes: int | None
) -> None:
    """extract_member for a tar.gz, in one pass through it. The member is
    the one named `requested` when a file or folder has that name, else the
    first file whose normalized name is `requested`; when several entries
    carry that name, the last one counts, and it must be a regular file.
    Each entry that may be the answer is copied to `destination` as it goes
    by, replacing what an earlier one left there. Entries past the scan's
    caps (_TarScan) are not looked at, as they are not listed."""
    target: str | None = None
    exact = False
    found = False
    with _TarScan(local_path) as scan:
        for member in scan:
            plain = _is_plain_tar_member(member)
            if not exact and plain and member.name == requested:
                exact, target = True, requested
            elif (
                target is None
                and plain
                and not member.isdir()
                and normalize_member_name(member.name) == requested
            ):
                target = member.name
            if target is None or member.name != target:
                continue
            found = False
            destination.unlink(missing_ok=True)
            if member.isfile():
                src = scan.extractfile(member)
                if src is None:
                    continue
                with src:
                    _bounded_copy(src, destination, max_bytes)
                found = True
    if not found:
        destination.unlink(missing_ok=True)
        raise KeyError(requested)


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
    at a time. Returns the (name, reason) of each member listed. A tar.gz is
    read in one pass (_TarScan); one past its caps raises tarfile.ReadError."""
    if is_zip(filename):
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
        if is_zip(filename):
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
        _TarScan(local_path) as source,
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
        if source.truncated:
            # Past the scan's caps the rest is unread, and a rebuilt archive
            # can't say what it holds: it is refused whole.
            raise tarfile.ReadError("too many entries in the tar.gz")
        if skipped:
            list_name, content = skipped_list(skipped, written)
            out = tarfile.TarInfo(list_name)
            out.size = len(content)
            out.mtime = int(datetime.now(UTC).timestamp())
            out.mode = 0o644
            rebuilt.addfile(out, io.BytesIO(content))
    return skipped
