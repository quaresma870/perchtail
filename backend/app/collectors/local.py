import shutil
from contextlib import contextmanager
from pathlib import Path

from app.collectors.base import DirEntry
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy", "resolve_path"]


def resolve_path(source: Source, relative_path: str = "") -> Path:
    """No ephemeral scratch needed for local sources — a file already on the
    same machine as the app is inherently zero-latency and always fresh
    (CLAUDE.md's "Built-in log viewer" section). api/archive.py uses this to
    serve plain files directly, skipping the scratch store entirely."""
    base = Path(source.base_path)
    return base / relative_path if relative_path else base


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    directory = resolve_path(source, relative_path)
    entries = []
    for child in sorted(directory.iterdir(), key=lambda p: p.name):
        child_path = f"{relative_path}/{child.name}" if relative_path else child.name
        is_dir = child.is_dir()
        if not is_dir and not is_visible(child_path, rules):
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
