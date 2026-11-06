import os
import shutil
from contextlib import contextmanager
from pathlib import Path

from app.collectors import base as collector_base
from app.collectors.base import DirEntry
from app.config import get_settings
from app.models import Rule, Source
from app.rules import is_entry_visible

__all__ = [
    "DirEntry",
    "LocalPathNotAllowedError",
    "base_dir",
    "base_path_allowed",
    "fetch_file",
    "list_directory",
    "local_copy",
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


def resolve_path(source: Source, relative_path: str) -> Path:
    """No ephemeral scratch needed for local sources — a file already on the
    same machine as the app is inherently zero-latency and always fresh
    (CLAUDE.md's "Built-in log viewer" section). api/archive.py uses this to
    serve plain files directly, skipping the scratch store entirely.

    The result is resolved and must stay strictly inside the source's base
    path, so a symlink under it can't point a read anywhere else on this
    host, and an empty path can't name the base directory itself."""
    base = str(base_dir(source))
    # realpath + a prefix check on the separator-terminated base (rather
    # than Path.is_relative_to) is the containment pattern static analysis
    # recognizes; behaviour is the same.
    target = os.path.realpath(os.path.join(base, relative_path))
    if not target.startswith(os.path.join(base, "")):
        raise LocalPathNotAllowedError("path resolves outside the source's base path")
    return Path(target)


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    base = base_dir(source)
    directory = resolve_path(source, relative_path) if relative_path else base
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        if child.is_symlink() and not child.resolve().is_relative_to(base):
            continue
        child_path = f"{relative_path}/{child.name}" if relative_path else child.name
        is_dir = child.is_dir()
        if not is_entry_visible(child_path, is_dir, rules, case_insensitive=False):
            continue
        size = 0 if is_dir else child.stat().st_size
        entries.append(DirEntry(name=child.name, path=child_path, is_dir=is_dir, size=size))
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Only used where a real copy is unavoidable (none, currently —
    local_copy() below always yields resolve_path() directly instead).
    Kept for interface parity with the other connectors."""
    shutil.copyfile(resolve_path(source, relative_path), destination)


@contextmanager
def local_copy(source: Source, relative_path: str):
    """No fetch needed — the file is already local, so this yields the real
    path directly rather than copying it anywhere first."""
    yield resolve_path(source, relative_path)


def read_range(
    source: Source, relative_path: str, offset: int, length: int, head: int = 0
) -> collector_base.RangeRead:
    """Incremental follow (#254): the file's size, its first `head` bytes and
    up to `length` bytes from `offset`."""
    with open(resolve_path(source, relative_path), "rb") as f:
        size = os.fstat(f.fileno()).st_size
        return collector_base.read_range_from(f, size, offset, length, head)
