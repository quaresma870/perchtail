import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import smbclient

from app.collectors.base import DirEntry
from app.crypto import decrypt_credential
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]


def _register_session(source: Source) -> None:
    """smbclient keeps a process-wide session cache keyed by server —
    registering again with the same credentials is a cheap no-op if already
    connected, so this can be called before every operation without
    reconnecting each time."""
    creds = decrypt_credential(source.credential_ref)
    smbclient.register_session(
        source.host,
        username=creds["username"],
        password=creds["password"],
        port=source.port or 445,
    )


def _unc_path(source: Source, relative_path: str = "") -> str:
    """source.base_path is the share name (optionally with a sub-path), e.g.
    "AppLogs" or "AppLogs\\nested" — see docs/source-setup.md for how the
    share itself is set up on the Windows side."""
    base = source.base_path.strip("\\/")
    tail_parts = [p for p in (base, relative_path.replace("/", "\\")) if p]
    tail = "\\".join(tail_parts)
    return f"\\\\{source.host}\\{tail}" if tail else f"\\\\{source.host}"


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Live directory listing filtered through the rule engine. Directories
    are always listed (never filtered) so the tree stays navigable toward
    deeper matches like `**/*.log` — only files are subject to the rule
    chain (see collectors/ssh.py's list_directory for the same rationale)."""
    _register_session(source)
    directory = _unc_path(source, relative_path)
    entries = []
    for info in smbclient.scandir(directory):
        child_path = f"{relative_path}/{info.name}" if relative_path else info.name
        is_dir = info.is_dir()
        if not is_dir and not is_visible(child_path, rules):
            continue
        size = 0 if is_dir else info.stat().st_size
        entries.append(DirEntry(name=info.name, path=child_path, is_dir=is_dir, size=size))
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — always a fresh transfer, never
    reused."""
    _register_session(source)
    remote_path = _unc_path(source, relative_path)
    with smbclient.open_file(remote_path, mode="rb") as src, open(destination, "wb") as dst:
        shutil.copyfileobj(src, dst)


@contextmanager
def local_copy(source: Source, relative_path: str):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
