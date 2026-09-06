import base64
import tempfile
from contextlib import contextmanager
from pathlib import Path

from app.agent_registry import get_agent_registry
from app.collectors.base import DirEntry
from app.models import Rule, Source
from app.rules import is_visible

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy"]


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Sends a `list` command down the source's connected agent (see
    app/agent_registry.py) and waits for its response — same live,
    fetch-on-browse semantics as every other connector, just relayed
    through a connection the agent dialed out to establish rather than one
    this app dials directly."""
    result = get_agent_registry().send_command_sync(source.id, "list", path=relative_path)
    entries = []
    for raw in result.get("entries", []):
        child_path = f"{relative_path}/{raw['name']}" if relative_path else raw["name"]
        if not raw["is_dir"] and not is_visible(child_path, rules):
            continue
        entries.append(
            DirEntry(name=raw["name"], path=child_path, is_dir=raw["is_dir"], size=raw["size"])
        )
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — the agent reads the file off its
    own local disk and sends the bytes back over the same connection,
    base64-encoded (same trade-off as collectors/winrm.py: correctness and
    a single, simple wire format over squeezing the last bit of transfer
    efficiency out of what's still just a log-file viewer)."""
    result = get_agent_registry().send_command_sync(source.id, "fetch", path=relative_path)
    destination.write_bytes(base64.b64decode(result["content_b64"]))


@contextmanager
def local_copy(source: Source, relative_path: str):
    """Yields a local filesystem path with `relative_path`'s content, for
    callers that need to run zipfile/tarfile/gzip against it — same pattern
    as the other remote connectors (ssh.py, smb.py, winrm.py)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path
