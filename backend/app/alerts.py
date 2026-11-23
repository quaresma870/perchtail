"""Live alerts (#305): a webhook is called when lines matching an alert's
query are written to the files it watches.

A pass runs every `alerts_interval_seconds` (Settings -> System, 60 s by
default). For each enabled alert whose owner may still alert, it resolves
the alert's scope against the owner's current access (enabled sources
only), takes the files the file-name index lists there that match the
alert's glob (never compressed files or archives), and reads from each
only what was appended since the last pass -- the same ranged read Tail
uses, so a file's content is never fetched whole or kept. A file first
seen starts at its end; a rotated or rewritten one (it shrank, or its
first bytes changed) starts again from the beginning.

Each source gets at most FILES_PER_SOURCE files and SOURCE_BUDGET_SECONDS
per alert per pass; the files checked least recently go first, so the
rest come round on the next passes, and the backlog shows in monitoring.

Matches are sent as one webhook call per alert per pass (#328): sent
right away, retried with backoff while it fails for up to an hour
(AlertDelivery), then dropped and counted on the alert.

When redaction applies to a source (#382, #249), its lines are redacted
before the query runs on them, for every alert: a value that was replaced
can't be matched, and only redacted lines are queued and sent. They are
redacted as the Tail's (a little is read before the offset, to know whether
it falls inside a private key), on the agent's host for an agent that
redacts there; offsets and the file's identity stay those of the original,
so turning redaction on or off neither restarts a file nor repeats a match,
and each hit's offset is where its line starts in the original. A delivery
is redacted again as it is sent, with the rules that apply to its sources
then, so one queued before redaction was turned on doesn't go out in clear.
A file redaction can't read (an encoding or a compression it doesn't
support) waits where it is, and is logged once until it changes."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from sqlmodel import Session, col, delete, select

from app import system_settings
from app.archives import is_archive, is_transparent_gzip
from app.audit import record_audit_event
from app.auth.models import Capability, GlobalCapability, User
from app.auth.rbac import resolve_capability, visible_source_ids
from app.crypto import decrypt_secret, encrypt_secret, follow_head_key
from app.logging_config import get_logger
from app.models import (
    Alert,
    AlertDelivery,
    AlertFileState,
    FileNameEntry,
    Folder,
    Protocol,
    Rule,
    Source,
)
from app.redaction import (
    UTF16_BE,
    UTF16_LE,
    CompiledRules,
    UnsupportedEncodingError,
    compile_rules,
    detect_encoding,
    head_digest,
    long_line_label,
    marker,
    redact_bytes,
    redact_window,
)
from app.redaction_rules import effective_redaction_rules, engine_rules
from app.rules import compile_linear, glob_matches, is_visible, rules_case_insensitive
from app.timeutils import utcnow
from app.webhook_safety import UnsafeWebhookURLError, resolve_pinned_webhook_target

logger = get_logger(__name__)

FILES_PER_SOURCE = 20
SOURCE_BUDGET_SECONDS = 15.0
# At most this much is read from one file per pass; the rest is read on the
# next pass.
READ_BYTES = 1024 * 1024
# The start of a file that's hashed to notice rotation (same as Tail).
HEAD_BYTES = 64
MAX_LINE_CHARS = 2000
MAX_HITS_PER_DELIVERY = 50
RETRY_FOR = timedelta(hours=1)
_WEBHOOK_TIMEOUT_SECONDS = 10.0
# Redacted, a read starts this much before the offset, as the Tail's
# (app.api.archive.FOLLOW_CONTEXT_BYTES): only to know whether the offset
# falls inside a private-key block. Never matched or sent.
CONTEXT_BYTES = 16 * 1024


@dataclass
class Hit:
    source_id: int
    source_name: str
    file_path: str
    offset: int
    line: str


@dataclass
class PassStats:
    alerts: int = 0
    files_read: int = 0
    files_waiting: int = 0
    hits: int = 0
    errors: list[str] = field(default_factory=list)


# What the last pass found, for /monitoring/health.
_last_pass: dict = {"at": None, "stats": None}
_pass_lock = threading.Lock()
# The files redaction couldn't read, by (alert id, source id, path): what
# the file looked like then (its listed size and modification time, and the
# identity of its start when it was read), so alert.file_read_failed is
# logged once until the file changes. In memory only, like the scratch
# bookkeeping: after a restart it is logged once again. Written by the pass
# and cleared from request threads (forget_alert, forget_source), so every
# access holds _unreadable_lock.
_unreadable: dict[tuple[int, int, str], tuple] = {}
_unreadable_lock = threading.Lock()


def _note_unreadable(key: tuple[int, int, str], seen: tuple) -> bool:
    """Records how an unreadable file looked; True when it looked the same
    the last time, so it was already logged."""
    with _unreadable_lock:
        repeated = _unreadable.get(key) == seen
        _unreadable[key] = seen
        return repeated


def _clear_unreadable(key: tuple[int, int, str]) -> None:
    """Drops one file, read again or failing for another reason."""
    with _unreadable_lock:
        _unreadable.pop(key, None)


def _forget_unreadable(match: Callable[[tuple[int, int, str]], bool]) -> None:
    """Drops the unreadable files whose key matches."""
    with _unreadable_lock:
        for key in [k for k in _unreadable if match(k)]:
            del _unreadable[key]


class UnreadableContentError(ValueError):
    """Content redaction can't read: an encoding or a compressed format it
    doesn't support. `head` is the identity of the file's start, when the
    read got that far (on the server; an agent doesn't send it)."""

    def __init__(self, message: str, head: str | None = None):
        super().__init__(message)
        self.head = head


def last_pass() -> dict:
    return dict(_last_pass)


def head_hash(source_id: int, start: bytes, offset: int) -> str:
    """The same identity of a file's start as the Tail's X-Follow-Head: an
    HMAC with the source's key (app.redaction.head_digest)."""
    return head_digest(follow_head_key(source_id), start, offset)


def compile_query(query: str, regex: bool):
    """A callable telling whether a line matches: plain text anywhere in
    the line ignoring case, or an RE2 regex (linear time). Raises
    re2.error for a regex RE2 can't run."""
    if regex:
        pattern = compile_linear(query, ignore_case=True)
        return lambda line: pattern.search(line) is not None
    needle = query.casefold()
    return lambda line: needle in line.casefold()


def owner_may_alert(owner: User | None) -> bool:
    """Alerts send log lines out, so they only run while their owner is
    active and still holds manage_alerts, checked at every pass."""
    if owner is None or not owner.active:
        return False
    return owner.role.is_super_admin or GlobalCapability.manage_alerts in (
        owner.role.global_capabilities
    )


def folder_subtree(session: Session, folder_id: int) -> set[int]:
    ids = {folder_id}
    pending = [folder_id]
    while pending:
        children = session.exec(
            select(Folder.id).where(col(Folder.parent_folder_id).in_(pending))
        ).all()
        pending = [c for c in children if c not in ids]
        ids.update(pending)
    return ids


def scope_source_ids(session: Session, owner: User, alert: Alert) -> set[int]:
    """The enabled sources this alert may read now: its scope, within what
    the owner can view."""
    query = select(Source.id).where(Source.enabled.is_(True), Source.is_system.is_(False))
    if alert.source_id is not None:
        query = query.where(Source.id == alert.source_id)
    elif alert.folder_id is not None:
        query = query.where(col(Source.folder_id).in_(folder_subtree(session, alert.folder_id)))
    elif alert.customer_id is not None:
        query = query.where(Source.customer_id == alert.customer_id)
    ids = set(session.exec(query).all())
    visible = visible_source_ids(session, owner, Capability.view)
    return ids if visible is None else ids & set(visible)


def _watchable(path: str, glob: str, rules: list[Rule], case_insensitive: bool) -> bool:
    name = path.rsplit("/", 1)[-1]
    if is_transparent_gzip(name) or is_archive(name):
        return False
    if not glob_matches(glob, path, ignore_case=case_insensitive):
        return False
    return is_visible(path, rules, case_insensitive=case_insensitive)


def _complete_lines(data: bytes, at_limit: bool) -> bytes:
    """Only whole lines are matched; a last line still being written is
    read again on the next pass. When the read hit its size limit with no
    newline at all, it's taken as is so the alert doesn't stall on it."""
    end = data.rfind(b"\n")
    if end < 0:
        return data if at_limit else b""
    return data[: end + 1]


@dataclass(frozen=True)
class SourceRedaction:
    """How a source's lines are redacted for the alerts: the compiled rules
    (on the server), and the `redact` block to send its agent instead when
    the agent redacts on its host."""

    rules: CompiledRules
    block: dict | None = None


def source_redaction(session: Session, source: Source) -> SourceRedaction | None:
    """None when redaction doesn't apply to the source (off, or no
    effective rule), the same for every alert. A stored rule the engine
    can't run raises (RedactionRuleError): check_alert then leaves the
    source's files waiting, rather than matching or sending lines
    unredacted."""
    from app.api.archive import agent_redaction_rules
    from app.collectors.agent import redaction_block

    compiled = redaction_rules_now(session, source.id)
    if compiled is None:
        return None
    agent_rules = agent_redaction_rules(session, source)
    block = None if agent_rules is None else redaction_block(source.id, agent_rules, "utf-8")
    return SourceRedaction(compiled, block)


def redaction_rules_now(session: Session, source_id: int) -> CompiledRules | None:
    """The compiled rules redaction applies to a source's content now, or
    None when it doesn't apply (off, or no effective rule). A source that
    no longer exists gets the system rules. Raises when a stored rule can't
    run (RedactionRuleError)."""
    if not system_settings.get_bool(session, "redaction_enabled"):
        return None
    rules = engine_rules(effective_redaction_rules(session, source_id))
    return compile_rules(rules) if rules else None


def _line_lengths(block: bytes, encoding: str | None, at: int) -> list[int]:
    """The length of each line of `block`, whole lines of the original in
    `encoding` starting at file offset `at`, newline included (a UTF-16
    newline counts only on an even offset, as the engine reads it)."""
    newline, unit = {UTF16_LE: (b"\n\x00", 2), UTF16_BE: (b"\x00\n", 2)}.get(encoding, (b"\n", 1))
    lengths: list[int] = []
    start = 0
    index = block.find(newline)
    while index >= 0:
        if (at + index) % unit:
            index = block.find(newline, index + 1)
            continue
        end = index + len(newline)
        lengths.append(end - start)
        start = end
        index = block.find(newline, end)
    return lengths


def _redacted_lines(output: bytes, lengths: list[int], begin: int) -> list[tuple[int, bytes]]:
    """Pairs the redacted whole lines of `output` with the original lines
    they stand for (one for one: redaction never adds or removes a line
    break), starting at file offset `begin`: (offset, redacted line)."""
    lines = output.split(b"\n")[:-1] if output else []
    if (output and not output.endswith(b"\n")) or len(lines) != len(lengths):
        raise ValueError("the redacted lines don't match the lines read")
    positioned = []
    for line, length in zip(lines, lengths, strict=True):
        positioned.append((begin, line))
        begin += length
    return positioned


def _read_redacted(
    connector,
    source: Source,
    state: AlertFileState,
    rules: CompiledRules,
    visibility: list[Rule],
) -> list[tuple[int, bytes]]:
    """Redacted on the server: what was appended since `state`, as
    (offset in the original, redacted line) for each whole line, as the
    Tail's (app.api.archive._follow_redacted). Moves `state` on. A link is
    only followed when the `visibility` rules show where it leads."""
    from app.api.archive import unsupported_compressed_format

    offset = state.offset
    read_from = max(0, offset - CONTEXT_BYTES)
    size, first, data = connector.read_range(
        source, state.file_path, read_from, READ_BYTES, HEAD_BYTES, rules=visibility
    )
    if offset > size or (
        state.head is not None and offset > 0 and head_hash(source.id, first, offset) != state.head
    ):
        # Rotated or rewritten: everything in it is new.
        offset = read_from = 0
        size, first, data = connector.read_range(
            source, state.file_path, 0, READ_BYTES, HEAD_BYTES, rules=visibility
        )
    if unsupported_compressed_format(first):
        raise UnreadableContentError(
            "content compressed in a format redaction doesn't read",
            head_hash(source.id, first, len(first)),
        )
    try:
        encoding = detect_encoding(first)
    except UnsupportedEncodingError as exc:
        raise UnreadableContentError(
            "content in an encoding redaction doesn't support",
            head_hash(source.id, first, len(first)),
        ) from exc
    window = redact_window(
        data,
        min(offset - read_from, len(data)),
        rules,
        base_offset=read_from,
        final=False,
        source_encoding=encoding,
    )
    if window.refused_at is not None:
        # UTF-32 part way: as content that starts so, it waits where it is.
        raise UnreadableContentError(
            "content in an encoding redaction doesn't support",
            head_hash(source.id, first, len(first)),
        )
    read_end = read_from + len(data)
    next_offset = read_from + window.end
    if next_offset <= offset and read_end < size:
        # No line ends within what one read takes: it can't be redacted
        # whole, so it becomes a marker and the next pass goes on after it.
        lines = [(offset, marker(long_line_label(read_end - offset)))]
        next_offset = read_end
    else:
        output = window.data[: len(window.data) - window.trailing_partial]
        # Read as UTF16_UNSURE from union_at on, the lines there end at every
        # newline byte.
        union = window.end if window.union_at is None else window.union_at
        lengths = _line_lengths(
            data[window.begin : union], encoding, read_from + window.begin
        ) + _line_lengths(data[union : window.end], None, read_from + union)
        lines = _redacted_lines(output, lengths, read_from + window.begin)
    state.offset = next_offset
    state.head = head_hash(source.id, first, next_offset) if next_offset else None
    return lines


def _read_on_agent(
    connector, source: Source, state: AlertFileState, block: dict, visibility: list[Rule]
) -> list[tuple[int, bytes]]:
    """Redacted on the agent's host: the same answer as _read_redacted,
    with where each line starts in the original worked out from the
    agent's line offsets; the original never leaves the host. Moves
    `state` on. A link is only followed when the `visibility` rules show
    where it leads."""
    from app.collectors.agent import AgentRedactionError

    def read_from(offset: int):
        try:
            return connector.call(
                "read_range_redacted",
                source,
                state.file_path,
                offset,
                READ_BYTES,
                block,
                line_offsets=True,
                rules=visibility,
            )
        except AgentRedactionError as exc:
            if exc.code in ("unsupported_encoding", "unsupported_format"):
                raise UnreadableContentError(str(exc)) from exc
            raise

    read = read_from(state.offset)
    if state.offset > read.size or (
        state.head is not None and state.offset > 0 and read.head_digest != state.head
    ):
        # Rotated or rewritten: everything in it is new.
        read = read_from(0)
    if read.refused_encoding_at is not None:
        # UTF-32 part way: as content that starts so, it waits where it is.
        raise UnreadableContentError("content in an encoding redaction doesn't support")
    state.offset = read.next_offset
    state.head = read.next_head_digest if read.next_offset else None
    return list(read.lines or ())


def _reads_from(alert: Alert, source: Source) -> bool:
    """Whether the alert may read the source's files now: not from an agent
    that doesn't say where links lead (collectors.agent.require_links),
    whose files wait where they are until it is updated; that is logged once
    per alert and source, until it reads again."""
    from app.collectors.agent import AgentUpdateRequiredError, require_links

    key = (alert.id, source.id, "")
    if source.protocol == Protocol.agent:
        try:
            require_links(source)
        except AgentUpdateRequiredError:
            if not _note_unreadable(key, ("agent_update_required",)):
                logger.warning(
                    "alert.agent_update_required", alert_id=alert.id, source_id=source.id
                )
            return False
    _clear_unreadable(key)
    return True


def _read_file(
    connector,
    source: Source,
    state: AlertFileState,
    matches,
    stats: PassStats,
    redaction: SourceRedaction | None = None,
    rules: list[Rule] | None = None,
):
    """Reads what was appended to one file since `state` and returns the
    matching lines; moves `state` on. With `redaction`, the query runs on
    the redacted lines and only those are kept. `rules` are the source's
    visibility rules: a file reached through a link is only read when they
    show where the link leads (without them, no link is followed)."""
    if redaction is not None:
        if redaction.block is not None:
            lines = _read_on_agent(connector, source, state, redaction.block, rules)
        else:
            lines = _read_redacted(connector, source, state, redaction.rules, rules)
        hits = []
        for position, raw in lines:
            line = raw.decode("utf-8", errors="replace").rstrip("\r")
            if line and matches(line):
                hits.append(
                    Hit(source.id, source.name, state.file_path, position, line[:MAX_LINE_CHARS])
                )
        stats.files_read += 1
        return hits
    size, first, data = connector.read_range(
        source, state.file_path, state.offset, READ_BYTES, HEAD_BYTES, rules=rules
    )
    if state.offset > size or (
        state.head is not None
        and state.offset > 0
        and head_hash(source.id, first, state.offset) != state.head
    ):
        # Rotated or rewritten: everything in it is new.
        state.offset = 0
        size, first, data = connector.read_range(
            source, state.file_path, 0, READ_BYTES, HEAD_BYTES, rules=rules
        )
    chunk = _complete_lines(data, at_limit=len(data) >= READ_BYTES)
    hits: list[Hit] = []
    position = state.offset
    for raw in chunk.split(b"\n")[:-1] if chunk.endswith(b"\n") else [chunk]:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line and matches(line):
            hits.append(
                Hit(source.id, source.name, state.file_path, position, line[:MAX_LINE_CHARS])
            )
        position += len(raw) + 1
    state.offset += len(chunk)
    state.head = head_hash(source.id, first, state.offset) if state.offset else None
    stats.files_read += 1
    return hits


def _current_sizes(
    connector, source: Source, rules: list[Rule], states: list[AlertFileState], started: float
) -> tuple[dict[str, tuple[int, float | None]], set[str]]:
    """The size and modification time of each watched file now, from one
    listing per folder they sit in. A file whose folder couldn't be listed
    (an error, or the time budget ran out) is returned in the second set, to
    be read directly."""
    folders: dict[str, list[str]] = {}
    for state in states:
        folder = state.file_path.rsplit("/", 1)[0] if "/" in state.file_path else ""
        folders.setdefault(folder, []).append(state.file_path)
    sizes: dict[str, tuple[int, float | None]] = {}
    unlisted: set[str] = set()
    for folder, paths in folders.items():
        if time.monotonic() - started > SOURCE_BUDGET_SECONDS:
            unlisted.update(paths)
            continue
        try:
            for entry in connector.list_directory(source, rules, folder):
                if not entry.is_dir:
                    sizes[entry.path] = (entry.size, entry.mtime)
        except Exception as exc:  # noqa: BLE001 - its files are read directly instead
            logger.info(
                "alert.folder_list_failed", source_id=source.id, path=folder, error=str(exc)
            )
            unlisted.update(paths)
    return sizes, unlisted


def check_alert(session: Session, alert: Alert, stats: PassStats | None = None) -> list[Hit]:
    """One alert's pass: the new matching lines in its files. Updates and
    commits the per-file positions as it goes, source by source."""
    from app.collectors.registry import CONNECTORS

    stats = stats or PassStats()
    owner = session.get(User, alert.user_id)
    if not owner_may_alert(owner):
        return []
    matches = compile_query(alert.query, alert.regex)
    source_ids = scope_source_ids(session, owner, alert)
    if not source_ids:
        return []
    now = utcnow()
    states = {
        (s.source_id, s.file_path): s
        for s in session.exec(
            select(AlertFileState).where(
                AlertFileState.alert_id == alert.id,
                col(AlertFileState.source_id).in_(source_ids),
            )
        ).all()
    }
    hits: list[Hit] = []
    for source in session.exec(select(Source).where(col(Source.id).in_(source_ids))).all():
        connector = CONNECTORS.get(source.protocol)
        if connector is None:
            continue
        try:
            redaction = source_redaction(session, source)
        except Exception as exc:  # noqa: BLE001 - fail closed: the source waits, the rest go on
            logger.error(
                "alert.redaction_failed", alert_id=alert.id, source_id=source.id, error=str(exc)
            )
            continue
        rules = list(session.exec(select(Rule).where(Rule.source_id == source.id)).all())
        case_insensitive = rules_case_insensitive(source)
        entries = session.exec(
            select(FileNameEntry.path, FileNameEntry.size).where(
                FileNameEntry.source_id == source.id
            )
        ).all()
        files = [
            (path, size)
            for path, size in entries
            if _watchable(path, alert.file_glob, rules, case_insensitive)
        ]
        known = []
        for path, size in files:
            state = states.get((source.id, path))
            if state is None:
                # First seen: start at its end, as listed.
                session.add(
                    AlertFileState(
                        alert_id=alert.id,
                        source_id=source.id,
                        file_path=path,
                        offset=size,
                        checked_at=None,
                    )
                )
            else:
                known.append(state)
        # Never hold SQLite's write lock across remote reads: a connector
        # records the source's status from this same thread, and would wait
        # on this session's lock until the busy timeout.
        session.commit()
        if not _reads_from(alert, source):
            continue
        started = time.monotonic()
        # Current sizes come from listing each folder once (one connection
        # per folder), so only files whose size changed are read -- a read
        # is a connection of its own on SSH, SMB and WinRM.
        sizes, unlisted = _current_sizes(connector, source, rules, known, started)
        changed = [
            s
            for s in known
            if s.file_path in unlisted or sizes.get(s.file_path, (None,))[0] != s.offset
        ]
        changed_paths = {s.file_path for s in changed}
        for state in known:
            if state.file_path not in changed_paths:
                state.checked_at = now
                session.add(state)
        changed.sort(key=lambda s: s.checked_at or now - timedelta(days=3650))
        for index, state in enumerate(changed):
            if index >= FILES_PER_SOURCE or time.monotonic() - started > SOURCE_BUDGET_SECONDS:
                stats.files_waiting += len(changed) - index
                break
            key = (alert.id, source.id, state.file_path)
            try:
                hits.extend(_read_file(connector, source, state, matches, stats, redaction, rules))
            except Exception as exc:  # noqa: BLE001 - one file never stops the pass
                if isinstance(exc, UnreadableContentError):
                    # It waits where it is; logged again only once it changed.
                    repeated = _note_unreadable(key, (sizes.get(state.file_path), exc.head))
                else:
                    repeated = False
                    _clear_unreadable(key)
                if not repeated:
                    logger.info(
                        "alert.file_read_failed",
                        alert_id=alert.id,
                        source_id=source.id,
                        path=state.file_path,
                        error=str(exc),
                    )
            else:
                _clear_unreadable(key)
            state.checked_at = now
            session.add(state)
        session.commit()
    return hits


def _payload(alert: Alert, hits: list[Hit]) -> dict:
    return {
        "alert_id": alert.id,
        "alert_name": alert.name,
        "query": alert.query,
        "regex": alert.regex,
        "matched_count": len(hits),
        "hits": [
            {
                "source_id": hit.source_id,
                "source_name": hit.source_name,
                "file_path": hit.file_path,
                "offset": hit.offset,
                "line": hit.line,
            }
            for hit in hits[:MAX_HITS_PER_DELIVERY]
        ],
    }


def webhook_url(alert: Alert) -> str:
    """The alert's webhook URL, decrypted. It is kept encrypted at rest and
    never returned by the API, since its path or query often carries the
    receiver's secret."""
    return decrypt_secret(alert.webhook_url_encrypted)


def set_webhook_url(alert: Alert, url: str) -> None:
    alert.webhook_url_encrypted = encrypt_secret(url)


def masked_webhook_url(url: str) -> str:
    """What the API shows of a webhook URL: its scheme, host and port, with
    the path and query left out (`https://hooks.example.com/…`)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{host}{port}/…"


# What the alert's owner is told when the webhook's server couldn't be
# reached, whatever the reason (refused, timed out, reset, TLS): the same
# words, so the answer can't be used to probe what answers where. The
# reason goes to the server log.
CONNECTION_FAILED = "connection failed"


def post_webhook(url: str, payload: dict) -> str | None:
    """Sends one webhook call; None when it worked, else why it didn't.
    The target is resolved and validated here, and pinned to the address
    that validation saw (see app/webhook_safety.py: no DNS rebinding)."""
    try:
        target = resolve_pinned_webhook_target(url)
        with httpx.Client(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            response = client.post(
                target.url,
                json=payload,
                headers={"Host": target.host_header},
                extensions={"sni_hostname": target.sni_hostname},
            )
        response.raise_for_status()
        return None
    except UnsafeWebhookURLError as exc:
        return f"blocked: {exc}"
    except httpx.HTTPStatusError as exc:
        # Never the URL: its path or query often carries the webhook's secret,
        # and this text is logged and shown on the alert.
        return f"HTTP {exc.response.status_code}"
    except httpx.HTTPError as exc:
        text = str(exc)
        logger.info(
            "alert.webhook_connection_failed",
            error_type=exc.__class__.__name__,
            # Never the URL (see above).
            error=None if "://" in text else text,
        )
        return CONNECTION_FAILED


def sources_out_of_view(session: Session, owner: User, payload: dict) -> list[int]:
    """The sources of a queued delivery's lines that its owner can't view
    any more (or that no longer exist), sorted; empty when every line may
    still go out. Checked at each send, since a delivery waits and is
    retried for up to RETRY_FOR after it was matched."""
    hits = payload.get("hits") if isinstance(payload, dict) else None
    source_ids = {hit.get("source_id") for hit in hits or [] if isinstance(hit, dict)}
    lost = []
    for source_id in source_ids:
        # A source id that isn't one can't be checked, so it counts as lost.
        source = session.get(Source, source_id) if isinstance(source_id, int) else None
        if (
            source is None
            or source.is_system
            or not resolve_capability(session, owner, source, Capability.view)
        ):
            lost.append(source_id if isinstance(source_id, int) else -1)
    return sorted(lost)


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(15, 2 ** max(0, attempts - 1)))


def _payload_as_sent(
    session: Session, payload: dict, rules_by_source: dict[int, CompiledRules | None]
) -> dict:
    """The payload as it goes out now: the hits' lines redacted with the
    rules that apply to their source at this moment, so a delivery queued
    before redaction was turned on doesn't go out in clear. The lines of
    each file are redacted together, in order, so a private key whose BEGIN
    line is among them stays redacted to its END. Lines already redacted
    when they were matched only hold markers where values were. Raises when
    the rules can't run (the delivery then isn't sent)."""
    hits = [dict(hit) for hit in payload.get("hits", [])]
    files: dict[tuple[int, str], list[dict]] = {}
    for hit in hits:
        files.setdefault((hit["source_id"], hit["file_path"]), []).append(hit)
    for (source_id, _), file_hits in files.items():
        if source_id not in rules_by_source:
            rules_by_source[source_id] = redaction_rules_now(session, source_id)
        rules = rules_by_source[source_id]
        if rules is None:
            continue
        lines = b"".join(hit["line"].encode("utf-8") + b"\n" for hit in file_hits)
        redacted, _ = redact_bytes(lines, rules, final=True)
        out = redacted.split(b"\n")
        if out[-1] != b"" or len(out) - 1 != len(file_hits):
            raise ValueError("the redacted lines don't match the lines queued")
        for hit, line in zip(file_hits, out, strict=False):
            hit["line"] = line.decode("utf-8", errors="replace")[:MAX_LINE_CHARS]
    return {**payload, "hits": hits}


def deliver_due(session: Session) -> None:
    """Sends (again) every delivery that's due, its lines redacted as they
    go out when redaction applies to their source. One older than RETRY_FOR
    is dropped and counted on its alert."""
    now = utcnow()
    rules_by_source: dict[int, CompiledRules | None] = {}
    for delivery in session.exec(
        select(AlertDelivery).where(AlertDelivery.next_attempt_at <= now)
    ).all():
        alert = session.get(Alert, delivery.alert_id)
        owner = session.get(User, alert.user_id) if alert is not None else None
        if alert is None or not alert.enabled or not owner_may_alert(owner):
            # Turned off, or its owner may no longer alert: what was matched
            # before doesn't go out any more either.
            session.delete(delivery)
            session.commit()
            continue
        out_of_view = sources_out_of_view(session, owner, delivery.payload)
        if out_of_view:
            # The lines were matched while the owner could view their
            # source; they no longer can (or the source is gone), so the
            # lines don't go out, and that is audited.
            logger.warning("alert.delivery_dropped", alert_id=alert.id, source_ids=out_of_view)
            record_audit_event(
                session,
                user_id=None,
                action="alert.delivery_dropped",
                target_type="alert",
                target_id=alert.id,
                metadata={
                    "reason": "owner_cannot_view_source",
                    "owner_id": owner.id,
                    "source_ids": out_of_view,
                    "attempts": delivery.attempts,
                },
            )
            session.delete(delivery)
            session.commit()
            continue
        try:
            payload = _payload_as_sent(session, delivery.payload, rules_by_source)
        except Exception as exc:  # noqa: BLE001 - fail closed: nothing goes out unredacted
            logger.error("alert.redaction_failed", alert_id=alert.id, error=str(exc))
            error = "redaction failed"
        else:
            error = post_webhook(webhook_url(alert), payload)
        delivery.attempts += 1
        if error is None:
            logger.info("alert.webhook_sent", alert_id=alert.id, attempts=delivery.attempts)
            alert.last_fired_at = now
            alert.last_error = None
            session.delete(delivery)
        elif now - delivery.created_at >= RETRY_FOR:
            logger.warning("alert.webhook_given_up", alert_id=alert.id, error=error)
            alert.failed_deliveries += 1
            alert.last_error = error
            session.delete(delivery)
            # A delivery that never got through is in the audit log: matched
            # log lines that were meant to leave and didn't. Each retry
            # before it is only in the app log.
            record_audit_event(
                session,
                user_id=None,
                action="alert.delivery_failed",
                target_type="alert",
                target_id=alert.id,
                metadata={"attempts": delivery.attempts, "error": error},
            )
        else:
            logger.warning("alert.webhook_failed", alert_id=alert.id, error=error)
            alert.last_error = error
            delivery.next_attempt_at = now + _backoff(delivery.attempts)
            session.add(delivery)
        session.add(alert)
        session.commit()


def queue_delivery(session: Session, alert: Alert, hits: list[Hit]) -> None:
    now = utcnow()
    session.add(
        AlertDelivery(
            alert_id=alert.id, payload=_payload(alert, hits), created_at=now, next_attempt_at=now
        )
    )


def interval(session: Session) -> timedelta:
    return timedelta(seconds=system_settings.get_int(session, "alerts_interval_seconds"))


def run_alert_pass(engine=None, *, force: bool = False) -> PassStats | None:
    """The scheduled pass (every 15 s, doing work once the configured
    interval has gone by). None when it wasn't due or another is running."""
    from app.db import engine as default_engine

    engine = engine or default_engine
    if not _pass_lock.acquire(blocking=False):
        return None
    try:
        with Session(engine) as session:
            if not force and _last_pass["at"] is not None:
                if utcnow() - _last_pass["at"] < interval(session):
                    return None
            stats = PassStats()
            alerts = session.exec(select(Alert).where(Alert.enabled.is_(True))).all()
            for alert in alerts:
                try:
                    hits = check_alert(session, alert, stats)
                    stats.alerts += 1
                    alert.last_checked_at = utcnow()
                    session.add(alert)
                    if hits:
                        stats.hits += len(hits)
                        queue_delivery(session, alert, hits)
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - one alert never stops the others
                    logger.exception("alert.evaluate_failed", alert_id=alert.id)
                    stats.errors.append(f"alert {alert.id}: {exc}")
                    session.rollback()
            deliver_due(session)
            _last_pass["at"] = utcnow()
            _last_pass["stats"] = stats
            return stats
    finally:
        _pass_lock.release()


def forget_alert(session: Session, alert_id: int) -> None:
    """Drops an alert's per-file positions and pending deliveries (the
    caller deletes the alert and commits)."""
    session.execute(delete(AlertFileState).where(AlertFileState.alert_id == alert_id))
    session.execute(delete(AlertDelivery).where(AlertDelivery.alert_id == alert_id))
    _forget_unreadable(lambda k: k[0] == alert_id)


def drop_scoped_alerts(session: Session, **scope: int) -> None:
    """Deletes the alerts scoped to a source, folder or customer that is
    being deleted, with their positions and deliveries (the caller
    commits). Ids are never reused, but an alert pointing at nothing would
    just sit there doing nothing."""
    ((key, value),) = scope.items()
    for alert in session.exec(select(Alert).where(getattr(Alert, key) == value)).all():
        forget_alert(session, alert.id)
        session.delete(alert)


def forget_source(session: Session, source_id: int) -> None:
    """Drops every alert's positions in a source that's deleted or now
    reads somewhere else (the caller commits)."""
    session.execute(delete(AlertFileState).where(AlertFileState.source_id == source_id))
    _forget_unreadable(lambda k: k[1] == source_id)
