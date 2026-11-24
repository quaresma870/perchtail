import errno
import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from app.collectors import base as collector_base
from app.collectors.base import (
    DirEntry,
    DirListing,
    LinkTargetHiddenError,
    TargetCheck,
    refuse_directory,
    require_target,
)
from app.config import get_settings
from app.models import Rule, Source
from app.rules import is_dir_visible, is_linked_entry_visible

# Follows links inside the base path, checking the rules on their target
# (collectors/errors.py passes it a target_check). SMB and WinRM refuse
# every link instead.
FOLLOWS_LINKS = True

__all__ = [
    "DirEntry",
    "FOLLOWS_LINKS",
    "LocalPathNotAllowedError",
    "base_dir",
    "base_path_allowed",
    "fetch_file",
    "list_directory",
    "local_copy",
    "resolve_file",
    "resolve_path",
]


class LocalPathNotAllowedError(PermissionError):
    pass


def _allowed_roots() -> list[str]:
    raw = get_settings().local_source_roots
    return [os.path.realpath(part.strip()) for part in raw.split(",") if part.strip()]


def base_path_allowed(base_path: str) -> bool:
    """Whether a non-system local source may use `base_path` -- it must
    resolve (symlinks included) to one of Settings.local_source_roots or
    somewhere beneath one."""
    base = os.path.realpath(base_path)
    return any(base == root or base.startswith(os.path.join(root, "")) for root in _allowed_roots())


def base_dir(source: Source) -> Path:
    """The source's base directory, resolved. Non-system sources must sit
    under an operator-allowed root (see Settings.local_source_roots) --
    re-checked here on every access, not just when the source is saved, so
    a source created before that setting existed (or after it was narrowed)
    can't keep reading."""
    if not source.is_system and not base_path_allowed(source.base_path):
        raise LocalPathNotAllowedError(
            "local sources must sit under a directory listed in LOCAL_SOURCE_ROOTS"
        )
    return Path(os.path.realpath(source.base_path))


def _real_relative(base: Path, real: Path) -> str:
    """Where `real` (resolved, inside `base`) is, relative to `base`, with
    `/` separators; "" for the base itself."""
    return "" if real == base else real.relative_to(base).as_posix()


def _resolve(source: Source, relative_path: str) -> tuple[Path, str]:
    """The resolved path inside the base path (LinkTargetHiddenError when it
    is outside it or is the base itself) and where it is relative to it."""
    base = str(base_dir(source))
    # realpath + a prefix check on the separator-terminated base (rather
    # than Path.is_relative_to) is the containment pattern static analysis
    # recognizes; behaviour is the same.
    target = os.path.realpath(os.path.join(base, relative_path))
    if not target.startswith(os.path.join(base, "")):
        raise LinkTargetHiddenError(relative_path)
    return Path(target), _real_relative(Path(base), Path(target))


def resolve_file(source: Source, relative_path: str, target_check: TargetCheck = None) -> Path:
    """resolve_path for a read: a folder, or a link to one, is refused
    (collectors.base.refuse_directory), with the rules checked on a link's
    target as a folder."""
    if not relative_path:
        # The base itself (a root of a source with several): a folder, once
        # the base path is known to be allowed.
        base_dir(source)
        refuse_directory(relative_path, relative_path, target_check)
    target, real = _resolve(source, relative_path)
    if target.is_dir():
        refuse_directory(relative_path, real, target_check)
    require_target(relative_path, real, target_check)
    return target


def resolve_path(source: Source, relative_path: str, target_check: TargetCheck = None) -> Path:
    """No ephemeral scratch needed for local sources — a file already on the
    same machine as the app is inherently zero-latency and always fresh
    (CLAUDE.md's "Built-in log viewer" section). api/archive.py uses this to
    serve plain files directly, skipping the scratch store entirely.

    The result is resolved and must stay strictly inside the source's base
    path, so a symlink under it can't point a read anywhere else on this
    host, and an empty path can't name the base directory itself. When a
    link leads somewhere else inside it, `target_check` must allow where it
    leads (collectors.base.require_target): the rules see the target, not
    only the link's name. Either refusal is a LinkTargetHiddenError."""
    target, real = _resolve(source, relative_path)
    require_target(relative_path, real, target_check)
    return target


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """The folder's children the rules show. A link (a child, or the folder
    itself) is listed only when it leads inside the base path to something
    the rules show too, checked on where it leads (DirEntry.target)."""
    base = base_dir(source)
    entries = DirListing()
    directory = base
    if relative_path:
        directory = resolve_path(
            source,
            relative_path,
            lambda real: is_dir_visible(real, rules, case_insensitive=False),
        )
        real_dir = _real_relative(base, directory)
        entries.real_dir = real_dir if real_dir != relative_path else None
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        child_path = f"{relative_path}/{child.name}" if relative_path else child.name
        real_child = directory / child.name
        if child.is_symlink():
            real_child = child.resolve()
            if not real_child.is_relative_to(base):
                continue
        real_path = _real_relative(base, real_child)
        target = real_path if real_path != child_path else None
        try:
            is_dir = child.is_dir()
            info = child.stat()
        except OSError:
            continue  # a dangling link, or gone since the folder was read
        if not is_linked_entry_visible(child_path, is_dir, target, rules, case_insensitive=False):
            continue
        size = 0 if is_dir else info.st_size
        entries.append(
            DirEntry(
                name=child.name,
                path=child_path,
                is_dir=is_dir,
                size=size,
                mtime=info.st_mtime,
                # Where a link leads, so a recursive walk can tell a loop (#347).
                real_key=f"{info.st_dev}:{info.st_ino}" if is_dir else None,
                target=target,
            )
        )
    return entries


def fetch_file(
    source: Source, relative_path: str, destination: Path, *, target_check: TargetCheck = None
) -> None:
    """Only used where a real copy is unavoidable (none, currently —
    local_copy() below always yields resolve_file() directly instead).
    Kept for interface parity with the other connectors."""
    shutil.copyfile(resolve_file(source, relative_path, target_check), destination)


@contextmanager
def local_copy(source: Source, relative_path: str, *, target_check: TargetCheck = None):
    """No fetch needed — the file is already local, so this yields the real
    path directly rather than copying it anywhere first. A file that isn't
    there is refused here, as a fetch refuses it on the other connectors,
    not where it is read (an archive opened for a member)."""
    path = resolve_file(source, relative_path, target_check)
    if not path.exists():
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", relative_path)
    yield path


def read_range(
    source: Source,
    relative_path: str,
    offset: int,
    length: int,
    head: int = 0,
    *,
    target_check: TargetCheck = None,
) -> collector_base.RangeRead:
    """Incremental follow (#254): the file's size, its first `head` bytes and
    up to `length` bytes from `offset`."""
    with open(resolve_file(source, relative_path, target_check), "rb") as f:
        size = os.fstat(f.fileno()).st_size
        return collector_base.read_range_from(f, size, offset, length, head)
