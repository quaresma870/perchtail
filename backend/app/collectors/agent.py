import base64
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, NoReturn

from app.agent_registry import (
    FEATURE_LINKS,
    FEATURE_REDACTION,
    AgentCommandError,
    get_agent_registry,
)
from app.archives import ArchiveMember, DecompressionLimitError
from app.collectors import base as collector_base
from app.collectors.base import (
    DirEntry,
    DirListing,
    LinkTargetHiddenError,
    SourceError,
    TargetCheck,
)
from app.config import get_settings
from app.crypto import follow_head_key
from app.json_values import as_int
from app.models import Rule, Source
from app.redaction import ENGINE_VERSION, MAX_LINE_BYTES, PEM_MAX_LINES, rules_digest
from app.rules import (
    is_dir_visible,
    is_entry_visible,
    is_linked_entry_visible,
    is_safe_relative_path,
    rules_case_insensitive,
)
from app.scratch import scratch_temp_dir

__all__ = [
    "AGENT_UPDATE_REQUIRED",
    "AgentRedactionError",
    "AgentUpdateRequiredError",
    "DirEntry",
    "FOLLOWS_LINKS",
    "agent_update_required",
    "fetch_archive",
    "fetch_file",
    "fetch_redacted",
    "list_archive",
    "list_directory",
    "local_copy",
    "read_range",
    "read_range_redacted",
    "redaction_block",
    "require_links",
]

# Follows links inside the base path, checking the rules on their target
# (collectors/errors.py passes it a target_check): the agent says where each
# path really is (FEATURE_LINKS) and reads it only once the server asked for
# that target.
FOLLOWS_LINKS = True

# The audit reason of content refused because the source's agent doesn't
# say where links lead (an agent older than FEATURE_LINKS).
AGENT_UPDATE_REQUIRED = "agent_update_required"

# Size of each fetch_chunk the agent sends (#205): about 1.4 MB once
# base64-encoded, well under uvicorn's 16 MiB WebSocket message limit.
FETCH_CHUNK_BYTES = 1024 * 1024
# The most entries one agent listing may carry (#347). An agent from 0.7 on
# stops there, or earlier once the message nears 8 MiB, and says the
# listing is truncated, so a huge folder stays well under the 16 MiB
# WebSocket message limit instead of failing every time. Rules are applied
# here, after, so this is above the explorer's own cap.
AGENT_LIST_MAX_ENTRIES = 50_000


class AgentUpdateRequiredError(SourceError):
    """The source's agent doesn't say where links lead (FEATURE_LINKS), so
    nothing of its files is read: open, follow, download, folder ZIP,
    archives and alerts are refused until it is updated. Listing goes on.
    The message only asks for the update."""

    status_code = 409
    public_message = (
        "This source's agent must be updated before its files can be read. "
        "Ask an administrator to update it from Settings → Agents."
    )
    error_code = AGENT_UPDATE_REQUIRED


def require_links(source: Source) -> None:
    """Refuses content from a connected agent that doesn't say where links
    lead (AgentUpdateRequiredError). An agent that isn't connected fails
    the command itself, as before."""
    registry = get_agent_registry()
    if registry.is_connected(source.id) and FEATURE_LINKS not in registry.features(source.id):
        raise AgentUpdateRequiredError(
            f"the agent of source {source.id} doesn't announce {FEATURE_LINKS}"
        )


def agent_update_required(source: Source) -> bool:
    """Whether the source's agent, as it last connected, doesn't say where
    links lead, so its files can't be read until it is updated (shown in
    Settings → Agents). False for one that never connected."""
    if source.agent_last_seen_at is None:
        return False
    return FEATURE_LINKS not in (source.agent_features or "").split(",")


def _real_path(value) -> str | None:
    """A real path from the agent, when it is one a rule can be checked on:
    canonical, relative to the base path, and not the base itself."""
    if isinstance(value, str) and value and is_safe_relative_path(value):
        return value
    return None


def _following_links(source: Source, relative_path: str, target_check: TargetCheck, send):
    """Sends a content command through `send(expect_real)`, first expecting
    `relative_path` itself. When the agent answers that the path really is
    elsewhere (a link, the path itself or a folder above it), it is asked
    again for that target, only if `target_check` allows it, so the rules
    see where the link leads, not only its name; a link out of the base
    path, to something the rules hide, or that changed between the two
    commands is refused (LinkTargetHiddenError). The agent reads nothing
    before it is asked for where the path really is."""
    require_links(source)
    attempt = relative_path
    for _ in range(2):
        try:
            return send(attempt)
        except AgentCommandError as exc:
            if exc.code == "links_missing":
                raise AgentUpdateRequiredError(str(exc)) from exc
            if exc.code != "link_target":
                raise
            real = None if exc.outside else _real_path(exc.real)
            if (
                attempt != relative_path
                or real is None
                or target_check is None
                or not target_check(real)
            ):
                raise LinkTargetHiddenError(relative_path) from exc
            attempt = real
    raise LinkTargetHiddenError(relative_path)  # pragma: no cover - the loop returns or raises


def list_directory(source: Source, rules: list[Rule], relative_path: str = "") -> list[DirEntry]:
    """Sends a `list` command down the source's connected agent (see
    app/agent_registry.py) and waits for its response — same live,
    fetch-on-browse semantics as every other connector, just relayed
    through a connection the agent dialed out to establish rather than one
    this app dials directly.

    The listing is cut at AGENT_LIST_MAX_ENTRIES entries (#347), here too
    in case an older agent sent more: a DirListing with `truncated` set.

    From an agent that says where links lead (FEATURE_LINKS), as for local
    and SSH sources: a folder a link leads to must be one the rules show
    (`real_dir`), and an entry a link leads elsewhere is listed only when
    the rules show its own path and its target too (`target`). An entry
    whose target isn't a path the rules can be checked on is left out."""
    links = FEATURE_LINKS in get_agent_registry().features(source.id)
    result = get_agent_registry().send_command_sync(
        source.id, "list", path=relative_path, max_entries=AGENT_LIST_MAX_ENTRIES
    )
    case_insensitive = rules_case_insensitive(source)
    raw_entries = result.get("entries") or []
    entries = DirListing()
    entries.truncated = bool(result.get("truncated")) or len(raw_entries) > AGENT_LIST_MAX_ENTRIES
    if links and "real_dir" in result:
        real_dir = result.get("real_dir")
        if not (
            isinstance(real_dir, str)
            and (real_dir == "" or is_safe_relative_path(real_dir))
            and is_dir_visible(real_dir, rules, case_insensitive=case_insensitive)
        ):
            raise LinkTargetHiddenError(relative_path)
        entries.real_dir = real_dir if real_dir != relative_path else None
    for raw in raw_entries[:AGENT_LIST_MAX_ENTRIES]:
        child_path = f"{relative_path}/{raw['name']}" if relative_path else raw["name"]
        target = None
        if links and "target" in raw:
            target = raw.get("target")
            if not (isinstance(target, str) and (target == "" or is_safe_relative_path(target))):
                continue
            if target == child_path:
                target = None
        if target == "":
            visible = False  # a link to the base path itself: nothing to check it on
        elif links:
            visible = is_linked_entry_visible(
                child_path, raw["is_dir"], target, rules, case_insensitive=case_insensitive
            )
        else:
            visible = is_entry_visible(
                child_path, raw["is_dir"], rules, case_insensitive=case_insensitive
            )
        if not visible:
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
                target=target,
            )
        )
    return entries


def fetch_file(
    source: Source, relative_path: str, destination: Path, *, target_check: TargetCheck = None
) -> None:
    """Fetch-on-open into `destination` — the agent reads the file off its
    own local disk and sends it back over the same connection,
    base64-encoded (same trade-off as collectors/winrm.py: correctness and
    a simple wire format over squeezing the last bit of transfer
    efficiency out of what's still just a log-file viewer).

    The file comes in chunks of FETCH_CHUNK_BYTES, each written to
    `destination` as it arrives (#205), so neither side holds the whole
    file in memory, and a large file stays under the WebSocket message
    limit. An agent older than 0.5.3 ignores `chunk_size` and sends the
    whole file in one message, handled as before.

    Through a link, only when `target_check` allows where it leads
    (_following_links)."""
    limit = collector_base.max_fetch_bytes()

    def send(expect_real: str) -> dict:
        with destination.open("wb") as sink:
            return get_agent_registry().send_command_sync(
                source.id,
                "fetch",
                path=relative_path,
                expect_real=expect_real,
                chunk_size=FETCH_CHUNK_BYTES,
                max_bytes=limit,
                sink=sink,
                limit=limit,
                require_feature=FEATURE_LINKS,
            )

    try:
        result = _following_links(source, relative_path, target_check, send)
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
def local_copy(source: Source, relative_path: str, *, target_check: TargetCheck = None):
    """Yields a local filesystem path with `relative_path`'s content, for
    callers that need to run zipfile/tarfile/gzip against it — same pattern
    as the other remote connectors (ssh.py, smb.py, winrm.py)."""
    with scratch_temp_dir() as tmp:
        path = Path(tmp) / "fetched"
        fetch_file(source, relative_path, path, target_check=target_check)
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
    """Incremental follow (#254): the agent reads the file's first `head`
    bytes and up to `length` bytes from `offset` off its own disk, and
    answers with the file's size. Every agent that is asked knows the
    command: it was added in 0.6, and an agent without links/1 (older than
    0.7) is refused first (require_links). Through a link, only when
    `target_check` allows where it leads (_following_links)."""

    def send(expect_real: str) -> dict:
        return get_agent_registry().send_command_sync(
            source.id,
            "read_range",
            path=relative_path,
            expect_real=expect_real,
            offset=max(int(offset), 0),
            length=max(min(int(length), collector_base.RANGE_MAX_BYTES), 0),
            head_length=max(min(int(head), collector_base.RANGE_MAX_HEAD_BYTES), 0),
            require_feature=FEATURE_LINKS,
        )

    result = _following_links(source, relative_path, target_check, send)
    head_bytes = base64.b64decode(result.get("head_b64") or "")
    data = base64.b64decode(result.get("data_b64") or "")
    if (
        len(data) > collector_base.RANGE_MAX_BYTES
        or len(head_bytes) > collector_base.RANGE_MAX_HEAD_BYTES
    ):
        raise collector_base.SourceError("the agent sent more than it was asked for")
    return collector_base.RangeRead(int(result.get("size", 0)), head_bytes, data)


# --- Redaction on the host (#382, #249) --------------------------------------
#
# An agent that announced FEATURE_REDACTION gets the source's redaction rules
# in a `redact` block with every command that returns content, redacts on its
# host, and lists and extracts archives and .gz files itself, so the original
# never leaves the host. Its answers are checked before anything is used: a
# content answer must say it was redacted, with the digest of the rules sent
# (fail closed: anything else is discarded and nothing is served). Commands
# are only ever sent to a connection that announced the feature.

# Redacted, content can be longer than the original (a short value becomes a
# longer marker). What the server accepts from the agent is still bounded, as
# a guard against an agent that misbehaves: this many times the limit on the
# original.
REDACTED_GROWTH_LIMIT = 4

# The codes of a redaction_error (agent/internal/agent/redaction.go).
_REFUSAL_CODES = frozenset(
    {
        "rule_unsupported",
        "unsupported_encoding",
        "unsupported_format",
        "not_redactable",
        "invalid_redact",
    }
)
# Why a fetch_archive left a member out (ArchiveMember.Skipped in the agent).
SKIPPED_REASONS = frozenset(
    {
        "not_found",
        "encrypted",
        "nested_archive",
        "unsupported_encoding",
        "unsupported_format",
        "too_large",
        "unreadable",
    }
)
# Left out once part of it was sent: the agent announces the member again,
# right after its content, which withdraws what was sent of it (over the
# limit or unreadable part way, or turned UTF-32 part way).
_WITHDRAWN_REASONS = frozenset({"too_large", "unreadable", "unsupported_encoding"})
_HEAD_DIGEST_RE = re.compile(r"^[0-9a-f]{32}$")
_LIMIT_RE = re.compile(r"over the (\d+)-byte limit")


class AgentRedactionError(SourceError):
    """Content the agent didn't redact, so nothing of it is served: it
    refused (a redaction_error; `code` says why, `rule` is the id of a rule
    it can't run), or its answer doesn't prove it was redacted with the
    rules sent (`code` "unverified"), or the connection no longer redacts
    on the host ("feature_missing"). 502 unless the caller answers the code
    with a refusal of its own."""

    public_message = "The content couldn't be redacted on the agent, so it isn't shown"

    def __init__(self, detail: str, *, code: str, rule: int | None = None):
        super().__init__(detail)
        self.code = code
        self.rule = rule


def _unverified(detail: str) -> AgentRedactionError:
    return AgentRedactionError(
        f"the agent's answer isn't redacted as asked: {detail}", code="unverified"
    )


def redaction_block(source_id: int, rules: list[dict], output_encoding: str) -> dict:
    """The `redact` block of a command: the rules (in the engine's format,
    app.redaction_rules.engine_rules) and their digest, the source's key for
    the Tail's head digest, the engine's limits, and the output encoding:
    "utf-8" for the viewer, "source" for a download (UTF-16 content encoded
    back, and the end of the content ends its last line)."""
    return {
        "engine": ENGINE_VERSION,
        "digest": rules_digest(rules),
        "rules": [dict(rule) for rule in rules],
        "head_key": base64.b64encode(follow_head_key(source_id)).decode("ascii"),
        "max_line_bytes": MAX_LINE_BYTES,
        "pem_max_lines": PEM_MAX_LINES,
        "output_encoding": output_encoding,
    }


def _max_decompressed_bytes() -> int:
    return int(get_settings().max_decompressed_mb * 1024**2)


def _raise_command_error(exc: AgentCommandError, *, expanding: bool) -> NoReturn:
    """Raises what an agent's refusal of a redacted command means here."""
    if exc.message_type == "redaction_error":
        code = exc.code if exc.code in _REFUSAL_CODES else "invalid_redact"
        raise AgentRedactionError(str(exc), code=code, rule=exc.rule) from exc
    if exc.message_type == "feature_error":
        raise AgentRedactionError(str(exc), code="feature_missing") from exc
    if exc.code == "too_large":
        # The agent says which limit: the file as stored (max_bytes) or what
        # extracting or decompressing it produced (max_decompressed).
        match = _LIMIT_RE.search(str(exc))
        limit = int(match.group(1)) if match else collector_base.max_fetch_bytes()
        if expanding and limit == _max_decompressed_bytes():
            raise DecompressionLimitError(str(exc)) from exc
        raise collector_base.too_large(exc.size or limit + 1, limit) from exc
    raise exc


@dataclass(frozen=True)
class RedactedFetch:
    """What the agent says redacting a fetch gave: the values replaced by
    rule name, how many bytes of the original it read (`source_size`),
    where a Tail goes on from (`source_offset`), how many bytes at the end
    stand for an unfinished last line, and the head digest of the original
    up to `source_offset` (None for an archive's members). When the content
    turned UTF-32 part way, `refused_encoding_at` is where the refused rest
    starts in the original (the content then ends with the refusal
    marker)."""

    counts: dict[str, int]
    source_size: int
    source_offset: int
    trailing_partial: int
    head_digest: str | None
    size: int
    refused_encoding_at: int | None = None


def _counts(value) -> dict[str, int]:
    if not isinstance(value, dict):
        raise _unverified("counts")
    counts: dict[str, int] = {}
    for name, count in value.items():
        number = as_int(count)
        if not isinstance(name, str) or number is None or number < 0:
            raise _unverified("counts")
        counts[name] = number
    return counts


def _verified(result: dict, block: dict) -> RedactedFetch:
    """The redaction a streamed fetch's closing message reports, once it
    proves the content was redacted with the block's rules."""
    if result.get("redacted") is not True or result.get("digest") != block["digest"]:
        raise _unverified("no redaction with the rules sent")
    if result.get("streamed") is not True:
        raise _unverified("not streamed")
    size = as_int(result.get("size"))
    source_size = as_int(result.get("source_size"))
    source_offset = as_int(result.get("source_offset"))
    partial = as_int(result.get("trailing_partial"))
    head = result.get("head_digest")
    if (
        size is None
        or source_size is None
        or source_offset is None
        or partial is None
        or size < 0
        or not 0 <= source_offset <= source_size
        or not 0 <= partial <= size
        or (head is not None and not (isinstance(head, str) and _HEAD_DIGEST_RE.match(head)))
    ):
        raise _unverified("closing message")
    refused = _refused_encoding_at(result, 0, source_size)
    counts = _counts(result.get("counts"))
    return RedactedFetch(counts, source_size, source_offset, partial, head, size, refused)


def _refused_encoding_at(result: dict, low: int, high: int) -> int | None:
    """The agent's `refused_encoding_at` (where it refused the rest of the
    content, UTF-32 part way), which must lie in [low, high]; None when the
    answer has none."""
    if result.get("refused_encoding_at") is None:
        return None
    refused = as_int(result.get("refused_encoding_at"))
    if refused is None or not low <= refused <= high:
        raise _unverified("refused_encoding_at")
    return refused


def fetch_redacted(
    source: Source,
    relative_path: str,
    destination: Path,
    block: dict,
    *,
    member: str | None = None,
    decompress: bool = False,
    target_check: TargetCheck = None,
) -> RedactedFetch:
    """Fetches `relative_path` into `destination`, redacted on the agent with
    `block`: the archive member `member` of it, and/or decompressed (a .gz
    file or member), when asked. Only redacted content ever arrives; an
    answer that doesn't prove it is deleted (AgentRedactionError). A
    refusal raises AgentRedactionError with the agent's code; past
    MAX_FETCH_MB, FetchTooLargeError; past MAX_DECOMPRESSED_MB once
    extracted or decompressed, DecompressionLimitError. Through a link,
    only when `target_check` allows where it leads (_following_links)."""
    limit = collector_base.max_fetch_bytes()
    max_decompressed = _max_decompressed_bytes()
    expanding = member is not None or decompress
    fields: dict = {
        "path": relative_path,
        "chunk_size": FETCH_CHUNK_BYTES,
        "max_bytes": limit,
        "redact": block,
    }
    if member is not None:
        fields["member"] = member
    if decompress:
        fields["decompress"] = "gzip"
    if expanding:
        fields["max_decompressed"] = max_decompressed
    sink_limit = (max_decompressed if expanding else limit) * REDACTED_GROWTH_LIMIT

    def send(expect_real: str) -> dict:
        with destination.open("wb") as sink:
            return get_agent_registry().send_command_sync(
                source.id,
                "fetch",
                sink=sink,
                limit=sink_limit,
                require_feature=(FEATURE_REDACTION, FEATURE_LINKS),
                expect_real=expect_real,
                **fields,
            )

    try:
        result = _following_links(source, relative_path, target_check, send)
        return _verified(result, block)
    except AgentCommandError as exc:
        destination.unlink(missing_ok=True)
        _raise_command_error(exc, expanding=expanding)
    except AgentRedactionError:
        destination.unlink(missing_ok=True)
        raise
    except BaseException as exc:
        destination.unlink(missing_ok=True)
        if _is_agent_error(exc):
            # A stream cut short or out of order: nothing of it is used.
            raise _unverified(str(exc)) from exc
        raise


def _is_agent_error(exc: BaseException) -> bool:
    from app.agent_registry import AgentError, AgentNotConnectedError, AgentTimeoutError

    return isinstance(exc, AgentError) and not isinstance(
        exc, AgentNotConnectedError | AgentTimeoutError
    )


@dataclass(frozen=True)
class RedactedRange:
    """A redacted read_range answer (agent's RedactedReadRangeResult): the
    file's size, the redacted whole lines, where the next poll starts and
    where the read stopped (in bytes of the original), the head digests at
    the offset asked and at `next_offset`, the values replaced, how many
    bytes at the end stand for an unfinished last line, and whether the
    content starts at the line after the offset. With line offsets asked
    (the alerts), `lines` is each whole redacted line with where it starts
    in the original. When the content turned UTF-32 part way,
    `refused_encoding_at` is where the refused rest starts in the original
    (the content then ends with the refusal marker, and `lines` is None)."""

    size: int
    data: bytes
    next_offset: int
    read_end: int
    head_digest: str
    next_head_digest: str
    counts: dict[str, int]
    trailing_partial: int
    aligned: bool
    lines: tuple[tuple[int, bytes], ...] | None = None
    refused_encoding_at: int | None = None


def _line_positions(
    data: bytes, partial: int, offset: int, next_offset: int, aligned: bool, changed
) -> tuple[tuple[int, bytes], ...]:
    """Where each whole redacted line of a read_range answer starts in the
    original, from the agent's `changed_lines` ([index, original length] of
    each line whose length isn't the original's, in order): the lines are
    the original's from `next_offset` back, and must start at `offset` (or
    after it, when the answer was aligned to the next line). Anything else
    isn't used."""
    whole = data[: len(data) - partial]
    lines = whole.split(b"\n")[:-1] if whole else []
    if (whole and not whole.endswith(b"\n")) or not isinstance(changed, list):
        raise _unverified("line offsets")
    lengths = [len(line) + 1 for line in lines]
    previous = -1
    for item in changed:
        if not isinstance(item, list) or len(item) != 2:
            raise _unverified("line offsets")
        index, length = as_int(item[0]), as_int(item[1])
        if index is None or length is None or not previous < index < len(lines) or length < 1:
            raise _unverified("line offsets")
        lengths[index] = length
        previous = index
    position = next_offset - sum(lengths)
    if position < offset or (not aligned and position != offset):
        raise _unverified("line offsets")
    positioned = []
    for line, length in zip(lines, lengths, strict=True):
        positioned.append((position, line))
        position += length
    return tuple(positioned)


def read_range_redacted(
    source: Source,
    relative_path: str,
    offset: int,
    length: int,
    block: dict,
    *,
    line_offsets: bool = False,
    target_check: TargetCheck = None,
) -> RedactedRange:
    """The redacted Tail on the agent: up to `length` bytes from `offset`
    (plus context before it, never sent), only whole lines, redacted on the
    host. The file's first bytes are never asked for, only their digests.
    `line_offsets` (the alerts) also asks where each line starts in the
    original (RedactedRange.lines); an answer without them isn't used.
    Through a link, only when `target_check` allows where it leads
    (_following_links)."""
    offset = max(int(offset), 0)
    fields: dict = {}
    if line_offsets:
        fields["with_line_offsets"] = True

    def send(expect_real: str) -> dict:
        return get_agent_registry().send_command_sync(
            source.id,
            "read_range",
            path=relative_path,
            expect_real=expect_real,
            offset=offset,
            length=max(min(int(length), collector_base.RANGE_MAX_BYTES), 0),
            redact=block,
            require_feature=(FEATURE_REDACTION, FEATURE_LINKS),
            **fields,
        )

    try:
        result = _following_links(source, relative_path, target_check, send)
    except AgentCommandError as exc:
        _raise_command_error(exc, expanding=False)
    if result.get("redacted") is not True or result.get("digest") != block["digest"]:
        raise _unverified("no redaction with the rules sent")
    if result.get("head_b64"):
        raise _unverified("the start of the file was sent")
    try:
        data = base64.b64decode(str(result.get("data_b64") or ""), validate=True)
    except ValueError as exc:
        raise _unverified("content") from exc
    size = as_int(result.get("size"))
    next_offset = as_int(result.get("next_offset"))
    read_end = as_int(result.get("read_end"))
    partial = as_int(result.get("trailing_partial"))
    head = result.get("head_digest")
    next_head = result.get("next_head_digest")
    if (
        len(data) > collector_base.RANGE_MAX_BYTES
        or size is None
        or next_offset is None
        or read_end is None
        or partial is None
        or size < 0
        or next_offset < offset
        or read_end < 0
        or not 0 <= partial <= len(data)
        or not (isinstance(head, str) and _HEAD_DIGEST_RE.match(head))
        or not (isinstance(next_head, str) and _HEAD_DIGEST_RE.match(next_head))
    ):
        raise _unverified("read_range answer")
    aligned = result.get("aligned") is True
    refused = _refused_encoding_at(result, offset, read_end)
    lines = None
    if line_offsets and refused is None:
        lines = _line_positions(
            data, partial, offset, next_offset, aligned, result.get("changed_lines")
        )
    return RedactedRange(
        size=size,
        data=data,
        next_offset=next_offset,
        read_end=read_end,
        head_digest=head,
        next_head_digest=next_head,
        counts=_counts(result.get("counts")),
        trailing_partial=partial,
        aligned=aligned,
        lines=lines,
        refused_encoding_at=refused,
    )


def list_archive(
    source: Source, relative_path: str, *, target_check: TargetCheck = None
) -> tuple[list[ArchiveMember], bool]:
    """The members of the archive at `relative_path`, listed on the agent
    (app.archives.list_members's answer), so the archive never leaves the
    host just to be listed. Returns them and whether the listing was cut.
    Through a link, only when `target_check` allows where it leads
    (_following_links)."""

    def send(expect_real: str) -> dict:
        return get_agent_registry().send_command_sync(
            source.id,
            "list_archive",
            path=relative_path,
            expect_real=expect_real,
            max_entries=AGENT_LIST_MAX_ENTRIES,
            require_feature=(FEATURE_REDACTION, FEATURE_LINKS),
        )

    try:
        result = _following_links(source, relative_path, target_check, send)
    except AgentCommandError as exc:
        if exc.message_type == "feature_error":
            _raise_command_error(exc, expanding=False)
        raise
    raw_members = result.get("members") or []
    if not isinstance(raw_members, list):
        raise SourceError("the agent sent an invalid archive listing")
    members: list[ArchiveMember] = []
    for raw in raw_members[:AGENT_LIST_MAX_ENTRIES]:
        name = raw.get("name") if isinstance(raw, dict) else None
        size = as_int(raw.get("size")) if isinstance(raw, dict) else None
        if not isinstance(name, str) or size is None or size < 0:
            raise SourceError("the agent sent an invalid archive listing")
        mtime_ms = as_int(raw.get("mtime_ms"))
        members.append(
            ArchiveMember(
                name=name,
                size=size,
                is_dir=raw.get("is_dir") is True,
                mtime=mtime_ms / 1000 if mtime_ms else None,
            )
        )
    truncated = bool(result.get("truncated")) or len(raw_members) > AGENT_LIST_MAX_ENTRIES
    return members, truncated


@dataclass
class ReceivedMember:
    """One member a fetch_archive sent: its name as stored, its modification
    time, whether it was a .gz file sent decompressed, why it was left out
    (None when sent), and the file its redacted content is in."""

    name: str
    mtime: float | None
    gunzipped: bool
    skipped: str | None
    path: Path | None
    size: int = 0


class _MemberFiles:
    """The ArchiveSink of a fetch_archive: each member asked for, announced
    once, goes to a file of its own in `directory`, at most `member_limit`
    bytes."""

    def __init__(self, directory: Path, wanted: set[str], member_limit: int):
        self.directory = directory
        self.wanted = wanted
        self.member_limit = member_limit
        self.members: list[ReceivedMember] = []
        self._seen: set[str] = set()
        self._file: BinaryIO | None = None

    def _close_file(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None

    def start_member(self, message: dict) -> None:
        self._close_file()
        name = message.get("name")
        skipped = message.get("skipped") or None
        last = self.members[-1] if self.members else None
        if (
            skipped in _WITHDRAWN_REASONS
            and last is not None
            and last.name == name
            and last.skipped is None
            and last.path is not None
        ):
            # The member just sent couldn't be read to its end: what was
            # sent of it is dropped and it is left out.
            last.path.unlink(missing_ok=True)
            last.path, last.size, last.skipped = None, 0, skipped
            return
        if not isinstance(name, str) or name not in self.wanted or name in self._seen:
            raise ValueError("a member that wasn't asked for")
        if skipped is not None and skipped not in SKIPPED_REASONS:
            raise ValueError("an unknown reason to leave a member out")
        self._seen.add(name)
        mtime_ms = as_int(message.get("mtime_ms"))
        path = None
        if skipped is None:
            path = self.directory / f"member-{len(self.members)}"
            self._file = path.open("wb")
        self.members.append(
            ReceivedMember(
                name=name,
                mtime=mtime_ms / 1000 if mtime_ms else None,
                gunzipped=message.get("gunzipped") is True,
                skipped=skipped,
                path=path,
            )
        )

    def write(self, data: bytes) -> int:
        if self._file is None:
            raise ValueError("content for a member left out")
        member = self.members[-1]
        member.size += len(data)
        if member.size > self.member_limit:
            raise ValueError("a member is over the limit")
        return self._file.write(data)

    def complete(self) -> bool:
        return self._seen == self.wanted

    def close(self) -> None:
        self._close_file()

    def discard(self) -> None:
        self._close_file()
        for member in self.members:
            if member.path is not None:
                member.path.unlink(missing_ok=True)


def fetch_archive(
    source: Source,
    relative_path: str,
    members: list[str],
    directory: Path,
    block: dict,
    *,
    target_check: TargetCheck = None,
) -> tuple[list[ReceivedMember], RedactedFetch]:
    """The members `members` (names as list_archive gave them) of the
    archive at `relative_path`, each redacted on the agent, in one pass
    over the archive, into files in `directory`. A .gz member arrives
    decompressed (`gunzipped`), and one the agent can't redact is announced
    as skipped, with no content. Every member asked for must be accounted
    for; otherwise, or for an answer that doesn't prove it was redacted,
    every file is deleted (AgentRedactionError). Through a link, only when
    `target_check` allows where it leads (_following_links)."""
    max_decompressed = _max_decompressed_bytes()
    files = _MemberFiles(directory, set(members), max_decompressed * REDACTED_GROWTH_LIMIT)

    def send(expect_real: str) -> dict:
        # The agent refuses a link before it sends any member, so nothing
        # of the first attempt is in `files`.
        return get_agent_registry().send_command_sync(
            source.id,
            "fetch_archive",
            path=relative_path,
            expect_real=expect_real,
            members=list(dict.fromkeys(members)),
            chunk_size=FETCH_CHUNK_BYTES,
            max_bytes=collector_base.max_fetch_bytes(),
            max_decompressed=max_decompressed,
            redact=block,
            sink=files,
            archive=True,
            require_feature=(FEATURE_REDACTION, FEATURE_LINKS),
        )

    try:
        result = _following_links(source, relative_path, target_check, send)
        files.close()
        total = _verified(result, block)
        if not files.complete():
            raise _unverified("a member asked for wasn't accounted for")
    except AgentCommandError as exc:
        files.discard()
        _raise_command_error(exc, expanding=True)
    except AgentRedactionError:
        files.discard()
        raise
    except BaseException as exc:
        files.discard()
        if _is_agent_error(exc):
            raise _unverified(str(exc)) from exc
        raise
    return files.members, total
