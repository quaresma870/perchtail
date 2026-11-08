import base64
from contextlib import contextmanager
from pathlib import Path

from app.agent_registry import AgentCommandError, get_agent_registry
from app.collectors import base as collector_base
from app.collectors.base import DirEntry
from app.models import Rule, Source
from app.rules import is_entry_visible, rules_case_insensitive
from app.scratch import scratch_temp_dir

__all__ = ["DirEntry", "fetch_file", "list_directory", "local_copy", "read_range"]

# Size of each fetch_chunk the agent sends (#205): about 1.4 MB once
# base64-encoded, well under uvicorn's 16 MiB WebSocket message limit.
FETCH_CHUNK_BYTES = 1024 * 1024


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Sends a `list` command down the source's connected agent (see
    app/agent_registry.py) and waits for its response — same live,
    fetch-on-browse semantics as every other connector, just relayed
    through a connection the agent dialed out to establish rather than one
    this app dials directly."""
    result = get_agent_registry().send_command_sync(source.id, "list", path=relative_path)
    case_insensitive = rules_case_insensitive(source)
    entries = []
    for raw in result.get("entries", []):
        child_path = f"{relative_path}/{raw['name']}" if relative_path else raw["name"]
        if not is_entry_visible(
            child_path, raw["is_dir"], rules, case_insensitive=case_insensitive
        ):
            continue
        # mtime_ms (#273): agents from 0.6 on; None from older ones.
        mtime_ms = raw.get("mtime_ms")
        entries.append(
            DirEntry(
                name=raw["name"],
                path=child_path,
                is_dir=raw["is_dir"],
                size=raw["size"],
                mtime=mtime_ms / 1000 if isinstance(mtime_ms, int) else None,
            )
        )
    return entries


def fetch_file(source: Source, relative_path: str, destination: Path) -> None:
    """Fetch-on-open into `destination` — the agent reads the file off its
    own local disk and sends it back over the same connection,
    base64-encoded (same trade-off as collectors/winrm.py: correctness and
    a simple wire format over squeezing the last bit of transfer
    efficiency out of what's still just a log-file viewer).

    The file comes in chunks of FETCH_CHUNK_BYTES, each written to
    `destination` as it arrives (#205), so neither side holds the whole
    file in memory, and a large file stays under the WebSocket message
    limit. An agent older than 0.5.3 ignores `chunk_size` and sends the
    whole file in one message, handled as before."""
    limit = collector_base.max_fetch_bytes()
    try:
        with destination.open("wb") as sink:
            result = get_agent_registry().send_command_sync(
                source.id,
                "fetch",
                path=relative_path,
                chunk_size=FETCH_CHUNK_BYTES,
                max_bytes=limit,
                sink=sink,
                limit=limit,
            )
    except BaseException as exc:
        destination.unlink(missing_ok=True)  # no partial file left behind
        if isinstance(exc, AgentCommandError) and exc.code == "too_large":
            raise collector_base.too_large(exc.size or limit + 1, limit) from exc
        raise
    if result.get("streamed"):
        return

    content = base64.b64decode(result["content_b64"])
    if len(content) > limit:
        destination.unlink(missing_ok=True)
        raise collector_base.too_large(len(content), limit)
    destination.write_bytes(content)


@contextmanager
def local_copy(source: Source, relative_path: str):
    """Yields a local filesystem path with `relative_path`'s content, for
    callers that need to run zipfile/tarfile/gzip against it — same pattern
    as the other remote connectors (ssh.py, smb.py, winrm.py)."""
    with scratch_temp_dir() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path)
        yield path


def read_range(
    source: Source, relative_path: str, offset: int, length: int, head: int = 0
) -> collector_base.RangeRead:
    """Incremental follow (#254): the agent reads the file's first `head`
    bytes and up to `length` bytes from `offset` off its own disk, and
    answers with the file's size. An agent older than 0.6 doesn't know the
    command: RangeNotSupportedError, and the viewer reloads the whole file
    instead."""
    try:
        result = get_agent_registry().send_command_sync(
            source.id,
            "read_range",
            path=relative_path,
            offset=max(int(offset), 0),
            length=max(min(int(length), collector_base.RANGE_MAX_BYTES), 0),
            head_length=max(min(int(head), collector_base.RANGE_MAX_HEAD_BYTES), 0),
        )
    except AgentCommandError as exc:
        if "unknown command type" in str(exc).lower():
            raise collector_base.RangeNotSupportedError(str(exc)) from exc
        raise
    head_bytes = base64.b64decode(result.get("head_b64") or "")
    data = base64.b64decode(result.get("data_b64") or "")
    if (
        len(data) > collector_base.RANGE_MAX_BYTES
        or len(head_bytes) > collector_base.RANGE_MAX_HEAD_BYTES
    ):
        raise collector_base.SourceError("the agent sent more than it was asked for")
    return collector_base.RangeRead(int(result.get("size", 0)), head_bytes, data)
