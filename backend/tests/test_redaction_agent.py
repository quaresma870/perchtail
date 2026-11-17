"""Redaction on the agent (#382, #249): when redaction applies to an agent
source whose agent announced redaction/1, every content command carries a
`redact` block and the agent redacts on its host, so the original never
reaches the server: browsing an archive, opening a file, a member or a .gz
file, the Tail, downloads, whole archives and folder ZIPs. The server checks
every answer says it was redacted with the rules sent, and discards it
otherwise. An agent that can't redact on its host falls back to the server.

The fake agent below runs the server's engine (the agent's Go engine has the
same behaviour, pinned by the shared vectors in
agent/internal/redact/testdata/vectors.json) and fails the test when asked
for content without a `redact` block."""

import base64
import gzip
import io
import tarfile
import zipfile
import zlib
from datetime import UTC, datetime

import pytest
from app import system_settings
from app.agent_registry import FEATURE_REDACTION, AgentCommandError, get_agent_registry
from app.alerts import _line_lengths
from app.api.archive import FOLLOW_CONTEXT_BYTES, FOLLOW_WINDOW_BYTES, unsupported_compressed_format
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.archives import is_archive, is_transparent_gzip, normalize_member_name
from app.auth.models import AuditLog, Role, User
from app.collectors import agent as agent_collector
from app.collectors.base import RANGE_MAX_BYTES
from app.crypto import follow_head_key
from app.db import get_session
from app.models import PatternKind, Protocol, RedactionRule, Rule, RuleType, Source
from app.redaction import (
    HEAD_BYTES,
    UnsupportedEncodingError,
    detect_encoding,
    head_digest,
    long_line_label,
    marker,
    redact_bytes,
    redact_window,
    rules_digest,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.test_redaction_endpoints import (
    MIDSTREAM,
    MIDSTREAM_REFUSED_AT,
    MIDSTREAM_START,
    SAMPLE,
    _enable_redaction,
    all_rules,
    assert_no_midstream_secret,
    assert_no_secret,
    compressed_sample,
)

SAMPLE_BYTES = SAMPLE.encode()
WHEN = (2024, 5, 1, 10, 20, 30)


def _refusal(code: str, rule=None) -> AgentCommandError:
    return AgentCommandError(
        f"refused: {code}", code=code, message_type="redaction_error", rule=rule
    )


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            zf.writestr(zipfile.ZipInfo(name, date_time=WHEN), content)
    return buffer.getvalue()


def _tar_gz_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mtime = 1714558830
            tf.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _archive_entries(name: str, data: bytes) -> list[tuple[str, bool, int, int | None, object]]:
    """(name, is_dir, size, mtime_ms, read) of each entry, as the agent
    walks it."""
    out = []
    if name.lower().endswith(".zip"):
        zf = zipfile.ZipFile(io.BytesIO(data))
        for info in zf.infolist():
            ms = int(datetime(*info.date_time, tzinfo=UTC).timestamp() * 1000)
            out.append(
                (info.filename, info.is_dir(), info.file_size, ms, lambda i=info: zf.read(i))
            )
        return out
    tf = tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")
    for member in tf.getmembers():
        if not (member.isfile() or member.isdir()):
            continue
        out.append(
            (
                member.name,
                member.isdir(),
                member.size,
                member.mtime * 1000 if member.mtime else None,
                lambda m=member: tf.extractfile(m).read(),
            )
        )
    return out


class FakeAgent:
    """An agent with `files` on its host that announced redaction/1. It
    answers content commands only with a `redact` block, redacting with the
    rules in it; anything else fails the test."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.commands: list[tuple[str, dict]] = []
        # Set to change the closing message of every content answer.
        self.tamper: dict | None = None
        self.refuse: AgentCommandError | None = None

    def __call__(
        self,
        source_id,
        command_type,
        timeout=30.0,
        *,
        sink=None,
        limit=None,
        archive=False,
        require_feature=None,
        command_id=None,
        **fields,
    ):
        self.commands.append((command_type, fields))
        if command_type == "list":
            return self._list(fields["path"])
        if command_type == "list_archive":
            assert require_feature == FEATURE_REDACTION
            data = self.files[fields["path"]]
            try:
                entries = _archive_entries(fields["path"], data)
            except (zipfile.BadZipFile, tarfile.TarError, OSError, EOFError) as exc:
                raise AgentCommandError(
                    "zip: not a valid zip file", message_type="list_archive_error"
                ) from exc
            return {
                "members": [
                    {"name": n, "is_dir": d, "size": s, "mtime_ms": m} for n, d, s, m, _ in entries
                ]
            }
        assert command_type in ("fetch", "read_range", "fetch_archive"), command_type
        if "redact" not in fields:
            raise AssertionError(f"{command_type} without a redact block: the original would leave")
        assert require_feature == FEATURE_REDACTION
        block = fields["redact"]
        assert block["digest"] == rules_digest(block["rules"])
        if self.refuse is not None:
            raise self.refuse
        if command_type == "fetch":
            result = self._fetch(fields, block, sink)
        elif command_type == "read_range":
            result = self._read_range(fields, block)
        else:
            result = self._fetch_archive(fields, block, sink)
        if self.tamper is not None:
            result.update(self.tamper)
        return result

    def _list(self, path):
        prefix = f"{path}/" if path else ""
        names: dict[str, tuple[bool, int]] = {}
        for name, data in self.files.items():
            if not name.startswith(prefix):
                continue
            head, _, rest = name[len(prefix) :].partition("/")
            names[head] = (True, 0) if rest else (False, len(data))
        return {"entries": [{"name": n, "is_dir": d, "size": s} for n, (d, s) in names.items()]}

    @staticmethod
    def _redact(content: bytes, block: dict, final: bool):
        head = content[:HEAD_BYTES]
        if unsupported_compressed_format(head):
            raise _refusal("unsupported_format")
        try:
            encoding = detect_encoding(head)
        except UnsupportedEncodingError as exc:
            raise _refusal("unsupported_encoding") from exc
        out, result = redact_bytes(
            content,
            block["rules"],
            final=final,
            source_encoding=encoding,
            output_encoding=block["output_encoding"],
        )
        return out, result, head

    def _fetch(self, fields, block, sink):
        path = fields["path"]
        content = self.files[path]
        member = fields.get("member")
        decompress = fields.get("decompress") == "gzip"
        name = path.rsplit("/", 1)[-1]
        if member is not None:
            target = member.rsplit("/", 1)[-1]
            if is_archive(target) or (is_transparent_gzip(target) and not decompress):
                raise _refusal("not_redactable")
            entries = _archive_entries(name, content)
            by_name = {normalize_member_name(n): read for n, _, _, _, read in entries}
            content = by_name[member]()
        elif is_archive(name) or (is_transparent_gzip(name) and not decompress):
            raise _refusal("not_redactable")
        if decompress:
            content = gzip.decompress(content)
        final = member is not None or decompress or block["output_encoding"] == "source"
        out, result, head = self._redact(content, block, final)
        sink.write(out)
        key = base64.b64decode(block["head_key"])
        answer = {
            "type": "fetch_result",
            "streamed": True,
            "chunks": 1,
            "size": len(out),
            "redacted": True,
            "digest": block["digest"],
            "source_size": result.source_size,
            "source_offset": result.source_offset,
            "trailing_partial": result.trailing_partial,
            "counts": result.counts,
            "head_digest": head_digest(key, head, result.source_offset),
        }
        if result.refused_at is not None:
            answer["refused_encoding_at"] = result.refused_at
        return answer

    def _read_range(self, fields, block):
        assert "head_length" not in fields  # the start of the file is never asked for
        content = self.files[fields["path"]]
        offset, length = fields["offset"], fields["length"]
        size = len(content)
        head = content[:HEAD_BYTES]
        key = base64.b64decode(block["head_key"])
        answer = {
            "type": "read_range_result",
            "size": size,
            "redacted": True,
            "digest": block["digest"],
            "head_digest": head_digest(key, head, offset),
            "counts": {},
            "trailing_partial": 0,
            "aligned": False,
            "data_b64": "",
            "next_offset": offset,
            "read_end": offset,
            "next_head_digest": head_digest(key, head, offset),
        }
        if fields.get("with_line_offsets"):
            answer["changed_lines"] = []
        if offset > size:
            return answer
        if unsupported_compressed_format(head):
            raise _refusal("unsupported_format")
        try:
            encoding = detect_encoding(head)
        except UnsupportedEncodingError as exc:
            raise _refusal("unsupported_encoding") from exc
        read_from = max(0, offset - FOLLOW_CONTEXT_BYTES)
        before = offset - read_from
        data = content[read_from : read_from + min(before + length, RANGE_MAX_BYTES)]
        window = redact_window(
            data,
            min(before, len(data)),
            block["rules"],
            base_offset=read_from,
            final=False,
            source_encoding=encoding,
        )
        body, counts, partial = window.data, window.counts, window.trailing_partial
        read_end = read_from + len(data)
        next_offset = read_from + window.end
        skipped = next_offset <= offset and read_end < size
        if skipped:
            body = marker(long_line_label(min(read_end - offset, FOLLOW_WINDOW_BYTES))) + b"\n"
            counts, partial, next_offset = {}, 0, read_end
        next_offset = max(next_offset, offset)
        if window.refused_at is not None and not skipped:
            answer["refused_encoding_at"] = read_from + window.refused_at
        elif fields.get("with_line_offsets"):
            # As the agent's changedLines: the original length of each whole
            # redacted line whose length changed (lines end at every newline
            # byte from where it was read as UTF16_UNSURE).
            begin = window.begin
            union = window.end if window.union_at is None else window.union_at
            originals = (
                [next_offset - offset]
                if skipped
                else _line_lengths(data[begin:union], encoding, read_from + begin)
                + _line_lengths(data[union : window.end], None, read_from + union)
            )
            lines = body[: len(body) - partial].split(b"\n")[:-1]
            assert len(lines) == len(originals)
            answer["changed_lines"] = [
                [index, original]
                for index, (line, original) in enumerate(zip(lines, originals, strict=True))
                if len(line) + 1 != original
            ]
        answer.update(
            data_b64=base64.b64encode(body).decode(),
            next_offset=next_offset,
            read_end=read_end,
            next_head_digest=head_digest(key, head, next_offset),
            counts=counts,
            trailing_partial=partial,
            aligned=window.aligned,
        )
        return answer

    def _fetch_archive(self, fields, block, sink):
        path = fields["path"]
        name = path.rsplit("/", 1)[-1]
        wanted = list(fields["members"])
        entries = _archive_entries(name, self.files[path])
        last = {entry[0]: index for index, entry in enumerate(entries)}
        seq = 0
        total_size = 0
        source_size = 0
        counts: dict[str, int] = {}
        for index, (raw, is_dir, _, mtime_ms, read) in enumerate(entries):
            if raw not in wanted or last[raw] != index or is_dir:
                continue
            target = raw.rsplit("/", 1)[-1]
            announce = {"type": "archive_member", "seq": seq, "name": raw, "mtime_ms": mtime_ms}
            if is_archive(target):
                sink.start_member({**announce, "skipped": "nested_archive"})
                seq += 1
                continue
            content = read()
            gunzipped = is_transparent_gzip(target)
            failed = None
            if gunzipped:
                try:
                    content = gzip.decompress(content)
                except (OSError, EOFError, zlib.error):
                    failed = "unreadable"
            if failed is None and len(content) > fields["max_decompressed"]:
                failed = "too_large"
            if failed is not None:
                # As the agent: part of it sent before the read failed, then
                # announced again as skipped, which withdraws it.
                sink.start_member({**announce, "gunzipped": gunzipped})
                sink.write(b"redacted lines sent before the read failed\n")
                sink.start_member({**announce, "gunzipped": gunzipped, "skipped": failed})
                seq += 3
                continue
            try:
                out, result, _ = self._redact(content, block, True)
            except AgentCommandError as exc:
                sink.start_member({**announce, "gunzipped": gunzipped, "skipped": exc.code})
                seq += 1
                continue
            sink.start_member({**announce, "gunzipped": gunzipped})
            sink.write(out)
            seq += 2
            if result.refused_at is not None:
                # As the agent: UTF-32 part way, announced again as skipped.
                sink.start_member(
                    {**announce, "gunzipped": gunzipped, "skipped": "unsupported_encoding"}
                )
                seq += 1
                continue
            total_size += len(out)
            source_size += result.source_size
            for rule, count in result.counts.items():
                counts[rule] = counts.get(rule, 0) + count
        for raw in wanted:
            if raw not in last or entries[last[raw]][1]:
                sink.start_member({"type": "archive_member", "name": raw, "skipped": "not_found"})
        return {
            "type": "fetch_result",
            "streamed": True,
            "chunks": 0,
            "size": total_size,
            "redacted": True,
            "digest": block["digest"],
            "source_size": source_size,
            "source_offset": source_size,
            "trailing_partial": 0,
            "counts": counts,
        }


@pytest.fixture()
def agent_source(session, scratch_store, monkeypatch):
    """(client, source, agent): an agent source showing everything but
    `secret*` files, used by a super admin, with redaction on and every
    built-in detector enabled, and a fake agent that redacts on its host."""
    app = FastAPI()
    app.include_router(archive_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    source = Source(name="agent01", protocol=Protocol.agent, host="agent01", base_path="/var/log")
    session.add(source)
    session.commit()
    for order, (kind, pattern) in enumerate(
        ((RuleType.include, "**/*"), (RuleType.exclude, "**/secret*"))
    ):
        session.add(
            Rule(
                source_id=source.id,
                order=order,
                type=kind,
                pattern=pattern,
                pattern_kind=PatternKind.glob,
            )
        )
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: user
    _enable_redaction(session)
    agent = FakeAgent({})
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "send_command_sync", agent)
    monkeypatch.setattr(registry, "redacts_on_host", lambda source_id: True)
    return TestClient(app), source, agent


@pytest.fixture()
def scratch_store(tmp_path, monkeypatch):
    from app.config import get_settings
    from app.scratch import get_scratch_store

    monkeypatch.setenv("SCRATCH_DIR", str(tmp_path / "scratch"))
    get_settings.cache_clear()
    get_scratch_store.cache_clear()
    yield get_scratch_store()
    get_settings.cache_clear()
    get_scratch_store.cache_clear()


def _server_redacted(content: bytes, **options) -> bytes:
    return redact_bytes(content, all_rules(), **options)[0]


def _audit(session, action: str) -> AuditLog:
    return session.exec(select(AuditLog).where(AuditLog.action == action)).one()


def _scratch_files(store):
    return [p for p in store.root.rglob("*") if p.is_file()]


def _kinds(agent: FakeAgent) -> list[str]:
    return [kind for kind, _ in agent.commands]


# -- the viewer -----------------------------------------------------------------


def test_open_is_redacted_on_the_agent(session, agent_source, scratch_store):
    client, source, agent = agent_source
    # The last line is still being written: it comes as the marker.
    content = SAMPLE_BYTES + b"card 4111 1111 11"
    agent.files["app.log"] = content

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    assert response.status_code == 200, response.text
    assert_no_secret(response.content)
    assert b"4111 1111 11" not in response.content
    expected, result = redact_bytes(content, all_rules(), final=False)
    assert response.content == expected
    assert response.headers["x-redacted"] == "1"
    assert response.headers["x-source-offset"] == str(result.source_offset)
    assert response.headers["x-follow-partial"] == str(result.trailing_partial)
    assert response.headers["x-redaction-count"] == str(sum(result.counts.values()))
    # The same head digest the server gives when it redacts itself.
    assert response.headers["x-follow-head"] == head_digest(
        follow_head_key(source.id), content[:HEAD_BYTES], result.source_offset
    )
    kind, fields = agent.commands[-1]
    assert kind == "fetch" and fields["redact"]["output_encoding"] == "utf-8"
    assert "member" not in fields and "decompress" not in fields
    event = _audit(session, "file.open")
    assert event.event_metadata["redaction"] == "applied"
    assert event.event_metadata["redaction_at"] == "agent"
    # Only the redacted copy is in scratch.
    for path in _scratch_files(scratch_store):
        assert_no_secret(path.read_bytes())


def test_the_redact_block_carries_the_rules_and_the_sources_key(session, agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES

    client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    block = agent.commands[-1][1]["redact"]
    rules = session.exec(select(RedactionRule).order_by(RedactionRule.order)).all()
    assert [rule["id"] for rule in block["rules"]] == [rule.id for rule in rules]
    assert base64.b64decode(block["head_key"]) == follow_head_key(source.id)
    assert block["engine"] == 1


def test_open_of_a_gz_file_and_a_member_is_redacted_on_the_agent(agent_source):
    client, source, agent = agent_source
    agent.files["app.log.gz"] = gzip.compress(SAMPLE_BYTES)
    agent.files["logs.zip"] = _zip_bytes(
        {"app.log": SAMPLE_BYTES, "old.log.gz": gzip.compress(SAMPLE_BYTES)}
    )

    gz = client.get(f"/sources/{source.id}/open", params={"path": "app.log.gz"})
    member = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "app.log"}
    )
    gz_member = client.get(
        f"/sources/{source.id}/open", params={"path": "logs.zip", "member": "old.log.gz"}
    )

    for response in (gz, member, gz_member):
        assert response.status_code == 200, response.text
        assert_no_secret(response.content)
        assert response.content == _server_redacted(SAMPLE_BYTES)
        assert "x-follow-head" not in response.headers
    fetches = [fields for kind, fields in agent.commands if kind == "fetch"]
    assert fetches[0]["decompress"] == "gzip" and "member" not in fetches[0]
    assert fetches[1]["member"] == "app.log" and "decompress" not in fetches[1]
    assert fetches[2]["member"] == "old.log.gz" and fetches[2]["decompress"] == "gzip"


def test_browsing_an_archive_lists_it_on_the_agent(agent_source):
    client, source, agent = agent_source
    agent.files["logs.zip"] = _zip_bytes({"app.log": SAMPLE_BYTES, "secret.log": b"x"})

    response = client.get(f"/sources/{source.id}/browse", params={"path": "logs.zip"})

    assert response.status_code == 200, response.text
    assert [entry["name"] for entry in response.json()] == ["app.log"]
    # Listed on the host: the archive itself never travelled.
    assert _kinds(agent) == ["list_archive"]


def test_a_whole_archive_is_never_opened(session, agent_source):
    client, source, agent = agent_source
    agent.files["logs.zip"] = _zip_bytes({"app.log": SAMPLE_BYTES})
    response = client.get(f"/sources/{source.id}/open", params={"path": "logs.zip"})
    assert response.status_code == 403
    assert agent.commands == []
    assert _audit(session, "file.open_denied").event_metadata["reason"] == (
        "redaction_whole_archive"
    )


def test_follow_is_redacted_on_the_agent(session, agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES + b"password=hun"

    opened = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    offset = int(opened.headers["x-source-offset"])
    head = opened.headers["x-follow-head"]
    # The unfinished line is completed on the host.
    agent.files["app.log"] = SAMPLE_BYTES + b"password=hunter2\nnext 10.9.9.9\n"
    polled = client.get(
        f"/sources/{source.id}/follow", params={"path": "app.log", "offset": offset, "head": head}
    )

    assert polled.status_code == 200, polled.text
    assert "x-follow-reset" not in polled.headers
    assert polled.content == b"password=[redacted: password]\nnext [redacted: ip]\n"
    assert polled.headers["x-follow-offset"] == str(len(agent.files["app.log"]))
    assert polled.headers["x-redacted"] == "1"
    kind, fields = agent.commands[-1]
    assert kind == "read_range" and "head_length" not in fields
    event = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
    assert all(e.event_metadata["redaction_at"] == "agent" for e in event)


def test_follow_on_the_agent_takes_a_connector_slot(session, agent_source):
    """When the agent redacts, a poll needs a free connector slot too: 503
    with Retry-After, and nothing is asked of the agent or audited."""
    from app import system_settings
    from app.limits import SOURCE_KEY, current_limits, get_limiter

    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES
    system_settings.set_int(session, SOURCE_KEY, 1)
    session.commit()
    held = get_limiter().connector(current_limits(session), source.id)
    try:
        busy = client.get(f"/sources/{source.id}/follow", params={"path": "app.log", "offset": 0})
    finally:
        held.release()
    assert busy.status_code == 503
    assert "retry-after" in busy.headers
    assert agent.commands == []
    assert session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all() == []
    freed = client.get(f"/sources/{source.id}/follow", params={"path": "app.log", "offset": 0})
    assert freed.status_code == 200, freed.text
    assert [kind for kind, _ in agent.commands] == ["read_range"]


def test_follow_from_the_start_and_a_rotated_file(agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES
    first = client.get(f"/sources/{source.id}/follow", params={"path": "app.log", "offset": 0})
    assert first.status_code == 200
    assert first.content == _server_redacted(SAMPLE_BYTES, final=False)
    assert_no_secret(first.content)

    agent.files["app.log"] = b"another file entirely, longer than before " * 10
    rotated = client.get(
        f"/sources/{source.id}/follow",
        params={
            "path": "app.log",
            "offset": first.headers["x-follow-offset"],
            "head": first.headers["x-follow-head"],
        },
    )
    assert rotated.headers.get("x-follow-reset") == "1"
    assert rotated.content == b""


def test_follow_refusals_are_audited(session, agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE.encode("utf-32")
    response = client.get(f"/sources/{source.id}/follow", params={"path": "app.log", "offset": 0})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "redaction_unsupported_encoding"
    event = _audit(session, "file.open_denied")
    assert event.event_metadata["follow"] is True


@pytest.mark.parametrize(
    "content, error_code",
    [
        (SAMPLE.encode("utf-32"), "redaction_unsupported_encoding"),
        (compressed_sample("xz"), "redaction_unsupported_format"),
    ],
)
def test_open_refusals_of_the_agent_are_the_servers(session, agent_source, content, error_code):
    client, source, agent = agent_source
    agent.files["app.log"] = content
    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == error_code
    assert _audit(session, "file.open_denied").event_metadata["path"] == "app.log"


# -- fail closed ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        {"redacted": False},
        {"digest": "0" * 64},
        {"streamed": False},
        {"counts": {"card": -1}},
        {"source_offset": 10**9},
    ],
)
def test_an_answer_not_proven_redacted_is_discarded(agent_source, scratch_store, tamper):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES
    agent.tamper = tamper

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    assert response.status_code == 502
    assert_no_secret(response.content)
    assert _scratch_files(scratch_store) == []


@pytest.mark.parametrize(
    "tamper",
    [{"redacted": False}, {"digest": "0" * 64}, {"head_b64": "c2VjcmV0"}, {"next_offset": 0}],
)
def test_a_tail_answer_not_proven_redacted_is_discarded(agent_source, tamper):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES * 2
    agent.tamper = tamper
    response = client.get(
        f"/sources/{source.id}/follow", params={"path": "app.log", "offset": len(SAMPLE_BYTES)}
    )
    assert response.status_code == 502
    assert_no_secret(response.content)


def test_a_rule_the_agent_cant_run_is_named_without_falling_back(session, agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES
    rule = session.exec(select(RedactionRule).where(RedactionRule.detector == "card")).one()
    agent.refuse = _refusal("rule_unsupported", rule=rule.id)

    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    assert response.status_code == 502
    assert '"card"' in response.json()["detail"]
    # Asked once, with the rules; never again without them.
    assert len(agent.commands) == 1


def test_a_connection_without_the_feature_is_never_sent_the_rules(agent_source, monkeypatch):
    """The agent reconnected without redaction/1 after the request checked:
    the registry refuses to send the command (feature_missing)."""
    client, source, agent = agent_source

    def refuse(*args, require_feature=None, **kwargs):
        assert require_feature == FEATURE_REDACTION
        raise AgentCommandError(
            "the agent doesn't support redaction/1",
            code="feature_missing",
            message_type="feature_error",
        )

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", refuse)
    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert response.status_code == 502


# -- downloads ------------------------------------------------------------------


def test_downloads_are_redacted_on_the_agent(session, agent_source, scratch_store):
    client, source, agent = agent_source
    content = SAMPLE_BYTES + b"password=hunter2"
    agent.files["app.log"] = content
    agent.files["app.log.gz"] = gzip.compress(content)
    agent.files["logs.zip"] = _zip_bytes({"app.log": content, "old.log.gz": gzip.compress(content)})
    expected = _server_redacted(content, output_encoding="source")

    plain = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    gz = client.get(f"/sources/{source.id}/download", params={"path": "app.log.gz"})
    member = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "app.log"}
    )
    gz_member = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "old.log.gz"}
    )

    for response in (plain, gz, member):
        assert response.status_code == 200, response.text
        assert response.content == expected
        assert response.headers["x-perchtail-redacted"] == "1"
    assert gz.headers["content-disposition"].endswith('"app.redacted.log"')
    assert gz_member.status_code == 200
    assert gzip.decompress(gz_member.content) == expected
    assert gz_member.headers["content-disposition"].endswith('"old.redacted.log.gz"')
    for _kind, fields in agent.commands:
        assert fields["redact"]["output_encoding"] == "source"
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.download")).all()
    assert len(events) == 4
    assert all(e.event_metadata["redaction_at"] == "agent" for e in events)
    assert _scratch_files(scratch_store) == []


def test_utf16_downloads_stay_utf16(agent_source):
    client, source, agent = agent_source
    content = SAMPLE.encode("utf-16")
    agent.files["app.log"] = content
    response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert response.status_code == 200
    assert response.content == _server_redacted(
        content, output_encoding="source", source_encoding=detect_encoding(content[:HEAD_BYTES])
    )
    assert response.content.startswith(b"\xff\xfe")
    assert_no_secret(response.content.decode("utf-16"))


@pytest.mark.parametrize("name, build", [("logs.zip", _zip_bytes), ("logs.tar.gz", _tar_gz_bytes)])
def test_a_whole_archive_is_rebuilt_from_what_the_agent_redacted(
    session, agent_source, scratch_store, name, build
):
    client, source, agent = agent_source
    agent.files[name] = build(
        {
            "app.log": SAMPLE_BYTES,
            "sub/old.log.gz": gzip.compress(SAMPLE_BYTES),
            "secret.log": b"hidden by the rules",
            "inner.zip": _zip_bytes({"x.log": SAMPLE_BYTES}),
            "wide.log": SAMPLE.encode("utf-32"),
        }
    )

    response = client.get(f"/sources/{source.id}/download", params={"path": name})

    assert response.status_code == 200, response.text
    expected = _server_redacted(SAMPLE_BYTES, output_encoding="source")
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = zf.namelist()
            assert zf.read("app.log") == expected
            assert gzip.decompress(zf.read("sub/old.log.gz")) == expected
            assert zf.getinfo("app.log").date_time == WHEN
            listed = zf.read("REDACTION-SKIPPED.txt").decode()
    else:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tf:
            names = tf.getnames()
            assert tf.extractfile("app.log").read() == expected
            assert gzip.decompress(tf.extractfile("sub/old.log.gz").read()) == expected
            assert tf.getmember("app.log").mtime == 1714558830
            listed = tf.extractfile("REDACTION-SKIPPED.txt").read().decode()
    assert "secret.log" not in names
    assert "inner.zip: an archive inside an archive" in listed
    assert "wide.log: in an encoding redaction doesn't support" in listed
    assert "secret.log" not in listed
    # Members the rules hide were never asked for.
    fetch_archive = next(f for kind, f in agent.commands if kind == "fetch_archive")
    assert sorted(fetch_archive["members"]) == ["app.log", "sub/old.log.gz", "wide.log"]
    event = _audit(session, "file.download")
    assert event.event_metadata["redaction_at"] == "agent"
    assert event.event_metadata["skipped_count"] == 2
    assert _scratch_files(scratch_store) == []


def test_an_archive_inside_an_archive_is_refused_on_the_agent(session, agent_source):
    client, source, agent = agent_source
    agent.files["logs.zip"] = _zip_bytes({"inner.zip": _zip_bytes({"x.log": SAMPLE_BYTES})})
    response = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "inner.zip"}
    )
    assert response.status_code == 403
    assert agent.commands == []
    assert _audit(session, "file.download_denied").event_metadata["path"] == "logs.zip/inner.zip"


@pytest.mark.parametrize("fails", ["list_archive", "fetch_archive"])
def test_an_archive_the_agent_cant_read_is_refused_and_audited(
    session, agent_source, scratch_store, monkeypatch, fails
):
    """As a server-side rebuild refuses an archive it can't read (403,
    audited), not a 502 without a trace."""
    client, source, agent = agent_source
    agent.files["broken.zip"] = _zip_bytes({"app.log": SAMPLE_BYTES})

    def failing(source_id, command_type, timeout=30.0, **fields):
        if command_type == fails:
            raise AgentCommandError("zip: not a valid zip file", message_type=f"{fails}_error")
        return agent(source_id, command_type, timeout, **fields)

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", failing)
    response = client.get(f"/sources/{source.id}/download", params={"path": "broken.zip"})

    assert response.status_code == 403, response.text
    event = _audit(session, "file.download_denied")
    assert event.event_metadata["path"] == "broken.zip"
    assert event.event_metadata["reason"] == "redaction_whole_archive"
    assert _scratch_files(scratch_store) == []


def test_a_corrupt_archive_on_the_agent_is_refused_and_audited(session, agent_source):
    client, source, agent = agent_source
    agent.files["broken.zip"] = b"PK\x03\x04 not really a zip"
    response = client.get(f"/sources/{source.id}/download", params={"path": "broken.zip"})
    assert response.status_code == 403, response.text
    reason = _audit(session, "file.download_denied").event_metadata["reason"]
    assert reason == "redaction_whole_archive"


@pytest.mark.parametrize(
    "error, status_code",
    [
        (AgentCommandError("open broken.zip: no such file or directory"), 404),
        (AgentCommandError("open broken.zip: permission denied"), 403),
    ],
)
def test_an_archive_missing_or_denied_on_the_agent_keeps_its_answer(
    session, agent_source, monkeypatch, error, status_code
):
    client, source, agent = agent_source

    def failing(source_id, command_type, timeout=30.0, **fields):
        raise error

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", failing)
    response = client.get(f"/sources/{source.id}/download", params={"path": "broken.zip"})
    assert response.status_code == status_code, response.text
    denied = session.exec(select(AuditLog).where(AuditLog.action == "file.download_denied")).all()
    assert all(e.event_metadata.get("reason") != "redaction_whole_archive" for e in denied)


@pytest.mark.parametrize("name, build", [("logs.zip", _zip_bytes), ("logs.tar.gz", _tar_gz_bytes)])
def test_a_member_the_agent_cant_read_is_left_out_and_listed(
    session, agent_source, scratch_store, monkeypatch, name, build
):
    """As _redact_content on the server: a member over MAX_DECOMPRESSED_MB
    once decompressed, or a corrupt .gz member, is left out and listed in
    REDACTION-SKIPPED.txt (what was sent of it is dropped), and the other
    members are still there."""
    client, source, agent = agent_source
    monkeypatch.setattr(agent_collector, "_max_decompressed_bytes", lambda: 4096)
    agent.files[name] = build(
        {
            "app.log": SAMPLE_BYTES,
            "big.log": b"a line of the log\n" * 1000,
            "broken.log.gz": gzip.compress(SAMPLE_BYTES)[:-12] + b"\x00" * 12,
            "bad-header.log.gz": b"not gzip at all\n",
        }
    )

    response = client.get(f"/sources/{source.id}/download", params={"path": name})

    assert response.status_code == 200, response.text
    expected = _server_redacted(SAMPLE_BYTES, output_encoding="source")
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            names = set(zf.namelist())
            assert zf.read("app.log") == expected
            listed = zf.read("REDACTION-SKIPPED.txt").decode()
    else:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tf:
            names = set(tf.getnames())
            assert tf.extractfile("app.log").read() == expected
            listed = tf.extractfile("REDACTION-SKIPPED.txt").read().decode()
    assert names == {"app.log", "REDACTION-SKIPPED.txt"}
    assert "big.log: too large once decompressed" in listed
    assert "broken.log.gz: can't be read" in listed
    assert "bad-header.log.gz: can't be read" in listed
    assert _audit(session, "file.download").event_metadata["skipped_count"] == 3
    assert _scratch_files(scratch_store) == []


def test_a_download_with_redaction_downloads_off_is_the_original(session, agent_source):
    """redaction_downloads off: the download is the original, as for any
    source, while the viewer stays redacted."""
    client, source, agent = agent_source
    system_settings.set_bool(session, "redaction_downloads", False)
    session.commit()
    agent.files["app.log"] = SAMPLE_BYTES

    def original(source_id, command_type, timeout=30.0, **fields):
        assert command_type == "fetch" and "redact" not in fields
        return {"content_b64": base64.b64encode(SAMPLE_BYTES).decode()}

    monkeypatch_registry = get_agent_registry()
    previous = monkeypatch_registry.send_command_sync
    monkeypatch_registry.send_command_sync = original
    try:
        response = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    finally:
        monkeypatch_registry.send_command_sync = previous
    assert response.status_code == 200
    assert response.content == SAMPLE_BYTES


def test_a_folder_zip_is_redacted_on_the_agent(session, agent_source, scratch_store):
    client, source, agent = agent_source
    agent.files["logs/app.log"] = SAMPLE_BYTES
    agent.files["logs/old.log.gz"] = gzip.compress(SAMPLE_BYTES)
    agent.files["logs/bundle.tar.gz"] = _tar_gz_bytes(
        {"a.log": SAMPLE_BYTES, "secret.log": b"hidden"}
    )
    agent.files["logs/wide.log"] = SAMPLE.encode("utf-32")
    agent.files["logs/packed.log"] = compressed_sample("bz2")
    agent.files["logs/secret.log"] = b"hidden by the rules"

    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "logs"})

    assert response.status_code == 200, response.text
    expected = _server_redacted(SAMPLE_BYTES, output_encoding="source")
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = set(zf.namelist())
        assert zf.read("app.log") == expected
        assert gzip.decompress(zf.read("old.log.gz")) == expected
        with tarfile.open(fileobj=io.BytesIO(zf.read("bundle.tar.gz")), mode="r:gz") as tf:
            assert tf.getnames() == ["a.log"]
            assert tf.extractfile("a.log").read() == expected
        listed = zf.read("REDACTION-SKIPPED.txt").decode()
    assert names == {"app.log", "old.log.gz", "bundle.tar.gz", "REDACTION-SKIPPED.txt"}
    assert "wide.log: in an encoding" in listed
    assert "packed.log: in a compressed format" in listed
    assert "secret" not in listed
    for kind, fields in agent.commands:
        if kind in ("fetch", "fetch_archive"):
            assert fields["redact"]["output_encoding"] == "source"
    event = _audit(session, "folder.download")
    assert event.event_metadata["redaction_at"] == "agent"
    assert _scratch_files(scratch_store) == []


# -- content that turns UTF-32 part way ----------------------------------------------


def test_open_and_follow_on_the_agent_refuse_utf32_part_way(session, agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = MIDSTREAM

    opened = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert opened.status_code == 200, opened.text
    assert opened.content.endswith(b"[redacted: unsupported encoding]")
    assert_no_midstream_secret(opened.content)
    assert opened.headers["x-source-offset"] == str(len(MIDSTREAM))
    event = _audit(session, "file.open")
    assert event.event_metadata["redaction_at"] == "agent"
    assert event.event_metadata["redaction_refused"] == "redaction_unsupported_encoding"
    assert event.event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT
    assert "offset" not in event.event_metadata

    polled = client.get(
        f"/sources/{source.id}/follow",
        params={"path": "app.log", "offset": len(MIDSTREAM_START)},
    )
    assert polled.status_code == 200, polled.text
    assert polled.content.endswith(b"[redacted: unsupported encoding]")
    assert_no_midstream_secret(polled.content)
    assert polled.headers["x-follow-offset"] == str(len(MIDSTREAM))
    # The first poll of this Tail: audited with the offset it starts from
    # and the refusal.
    follow = _audit(session, "file.follow")
    assert follow.event_metadata["offset"] == len(MIDSTREAM_START)
    assert follow.event_metadata["redaction_refused"] == "redaction_unsupported_encoding"
    assert follow.event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT

    # The same poll again doesn't start a Tail, but its refusal is audited.
    again = client.get(
        f"/sources/{source.id}/follow",
        params={"path": "app.log", "offset": len(MIDSTREAM_START)},
    )
    assert again.status_code == 200, again.text
    follows = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
    assert len(follows) == 2
    assert follows[1].event_metadata["offset"] == len(MIDSTREAM_START)
    assert follows[1].event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT

    # A later poll of the same Tail that meets no UTF-32 isn't audited.
    agent.files["app.log"] = MIDSTREAM + b"more plain\n"
    later = client.get(
        f"/sources/{source.id}/follow",
        params={"path": "app.log", "offset": len(MIDSTREAM)},
    )
    assert later.status_code == 200, later.text
    assert later.content == b"more plain\n"
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()) == 2


def test_an_agent_answer_with_a_refusal_out_of_range_is_discarded(agent_source):
    client, source, agent = agent_source
    agent.files["app.log"] = MIDSTREAM
    agent.tamper = {"refused_encoding_at": len(MIDSTREAM) + 1}
    response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    assert response.status_code == 502


def test_downloads_on_the_agent_that_turn_utf32_part_way(session, agent_source, scratch_store):
    client, source, agent = agent_source
    agent.files["app.log"] = MIDSTREAM
    agent.files["logs.zip"] = _zip_bytes({"mixed.log.gz": gzip.compress(MIDSTREAM)})
    agent.files["dir/app.log"] = SAMPLE_BYTES
    agent.files["dir/mixed.log"] = MIDSTREAM
    agent.files["dir/mixed.log.gz"] = gzip.compress(MIDSTREAM)
    agent.files["dir/bundle.zip"] = _zip_bytes({"a.log": SAMPLE_BYTES, "mixed.log": MIDSTREAM})

    # A file is sent with the marker, and its entry says where it was refused.
    plain = client.get(f"/sources/{source.id}/download", params={"path": "app.log"})
    assert plain.status_code == 200, plain.text
    assert plain.content.endswith(b"[redacted: unsupported encoding]")
    assert_no_midstream_secret(plain.content)
    download = _audit(session, "file.download")
    assert download.event_metadata["redaction_refused"] == "redaction_unsupported_encoding"
    assert download.event_metadata["redaction_refused_offset"] == MIDSTREAM_REFUSED_AT
    assert "offset" not in download.event_metadata

    # A .gz member is compressed again whole: refused, as on the server.
    gz_member = client.get(
        f"/sources/{source.id}/download", params={"path": "logs.zip", "member": "mixed.log.gz"}
    )
    assert gz_member.status_code == 409
    denied = _audit(session, "file.download_denied")
    assert denied.event_metadata["reason"] == "redaction_unsupported_encoding"

    # In a folder ZIP, such files and members are left out and listed.
    response = client.get(f"/sources/{source.id}/download-zip", params={"path": "dir"})
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert sorted(zf.namelist()) == ["REDACTION-SKIPPED.txt", "app.log", "bundle.zip"]
        with zipfile.ZipFile(io.BytesIO(zf.read("bundle.zip"))) as inner:
            assert sorted(inner.namelist()) == ["REDACTION-SKIPPED.txt", "a.log"]
        listed = zf.read("REDACTION-SKIPPED.txt").decode()
        for name in zf.namelist():
            assert_no_midstream_secret(zf.read(name))
    for name in ("mixed.log", "mixed.log.gz", "bundle.zip/mixed.log"):
        assert f"{name}: in an encoding redaction doesn't support (UTF-32)" in listed
    late = _audit(session, "file.download_redaction_skipped")
    assert late.event_metadata["reason"] == "redaction_unsupported_encoding"
    assert late.event_metadata["skipped"] == ["dir/mixed.log"]
    assert _scratch_files(scratch_store) == []


def test_no_content_command_goes_without_redaction(agent_source):
    """Every path above, once more in one place: the fake agent fails any
    content command without a `redact` block."""
    client, source, agent = agent_source
    agent.files["app.log"] = SAMPLE_BYTES
    agent.files["app.log.gz"] = gzip.compress(SAMPLE_BYTES)
    agent.files["logs.zip"] = _zip_bytes({"app.log": SAMPLE_BYTES})
    calls = [
        ("browse", {"path": "logs.zip"}),
        ("browse", {"path": "logs.zip", "all": "true"}),
        ("open", {"path": "app.log"}),
        ("open", {"path": "app.log.gz"}),
        ("open", {"path": "logs.zip", "member": "app.log"}),
        ("follow", {"path": "app.log", "offset": 0}),
        ("download", {"path": "app.log"}),
        ("download", {"path": "app.log.gz"}),
        ("download", {"path": "logs.zip"}),
        ("download", {"path": "logs.zip", "member": "app.log"}),
        ("download-zip", {"path": ""}),
    ]
    for endpoint, params in calls:
        response = client.get(f"/sources/{source.id}/{endpoint}", params=params)
        assert response.status_code == 200, (endpoint, params, response.text)
    # Each kind of content command was asked for, and each carried the rules
    # (the fake agent fails one that doesn't).
    assert {"list_archive", "fetch", "read_range", "fetch_archive"} <= set(_kinds(agent))
    assert all(
        "redact" in fields
        for kind, fields in agent.commands
        if kind in ("fetch", "read_range", "fetch_archive")
    )


# -- the fallback for agents that can't redact on the host ---------------------------


def test_an_agent_without_the_feature_falls_back_to_the_server(session, agent_source, monkeypatch):
    import app.api.archive as archive_module

    client, source, _ = agent_source
    logged: list[str] = []
    real_logger = archive_module.logger

    class Recorder:
        def __getattr__(self, level):
            def log(event, **fields):
                logged.append(event)
                getattr(real_logger, level)(event, **fields)

            return log

    monkeypatch.setattr(archive_module, "logger", Recorder())
    registry = get_agent_registry()
    monkeypatch.setattr(registry, "redacts_on_host", lambda source_id: False)
    reported = iter([True, False])
    monkeypatch.setattr(registry, "first_redaction_fallback", lambda source_id: next(reported))
    commands = []

    def older_agent(source_id, command_type, timeout=30.0, **fields):
        commands.append((command_type, fields))
        assert "redact" not in fields
        return {"content_b64": base64.b64encode(SAMPLE_BYTES).decode()}

    monkeypatch.setattr(registry, "send_command_sync", older_agent)

    first = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    second = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})

    for response in (first, second):
        assert response.status_code == 200
        assert_no_secret(response.content)
        assert response.content == _server_redacted(SAMPLE_BYTES, final=False)
    assert [kind for kind, _ in commands] == ["fetch", "fetch"]
    events = session.exec(select(AuditLog).where(AuditLog.action == "file.open")).all()
    assert all(e.event_metadata["redaction_at"] == "server" for e in events)
    assert logged.count("agent.redaction_fallback") == 1


def test_without_redaction_the_agent_gets_the_commands_of_before(session, agent_source):
    client, source, agent = agent_source
    system_settings.set_bool(session, "redaction_enabled", False)
    session.commit()
    seen = []

    def plain_agent(source_id, command_type, timeout=30.0, **fields):
        seen.append((command_type, fields))
        assert "redact" not in fields and "require_feature" not in fields
        return {"content_b64": base64.b64encode(SAMPLE_BYTES).decode()}

    registry = get_agent_registry()
    previous = registry.send_command_sync
    registry.send_command_sync = plain_agent
    try:
        response = client.get(f"/sources/{source.id}/open", params={"path": "app.log"})
    finally:
        registry.send_command_sync = previous
    assert response.content == SAMPLE_BYTES
    assert seen[0][0] == "fetch"


# -- the collector's own checks -----------------------------------------------------


def test_fetch_archive_refuses_members_not_asked_for(tmp_path):
    files = agent_collector._MemberFiles(tmp_path, {"a.log"}, 100)
    with pytest.raises(ValueError):
        files.start_member({"name": "b.log"})
    files.start_member({"name": "a.log"})
    with pytest.raises(ValueError):
        files.start_member({"name": "a.log"})  # announced twice
    files.close()


def test_fetch_archive_bounds_each_member(tmp_path):
    files = agent_collector._MemberFiles(tmp_path, {"a.log", "b.log"}, 4)
    files.start_member({"name": "a.log"})
    files.write(b"abcd")
    with pytest.raises(ValueError):
        files.write(b"e")
    files.start_member({"name": "b.log", "skipped": "encrypted"})
    with pytest.raises(ValueError):
        files.write(b"x")  # nothing comes for a member left out
    files.discard()
    assert list(tmp_path.iterdir()) == []


def test_a_member_announced_again_as_skipped_is_withdrawn(tmp_path):
    files = agent_collector._MemberFiles(tmp_path, {"a.log", "b.log", "c.log"}, 100)
    files.start_member({"name": "a.log"})
    files.write(b"part of a")
    files.start_member({"name": "a.log", "skipped": "too_large"})
    assert files.members[0].skipped == "too_large"
    assert files.members[0].path is None
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        files.write(b"x")  # nothing more comes for it
    files.start_member({"name": "b.log"})
    files.write(b"b")
    files.start_member({"name": "c.log"})
    with pytest.raises(ValueError):
        # Only the member just sent can be withdrawn.
        files.start_member({"name": "b.log", "skipped": "unreadable"})
    with pytest.raises(ValueError):
        # Only for a reason found while reading it.
        files.start_member({"name": "c.log", "skipped": "encrypted"})
    files.discard()


def test_a_fetch_archive_that_leaves_a_member_unaccounted_for_is_discarded(tmp_path, monkeypatch):
    source = Source(id=7, name="a", protocol=Protocol.agent, host="a", base_path="/")
    block = agent_collector.redaction_block(7, all_rules(), "source")

    def agent(source_id, command_type, timeout=30.0, *, sink=None, **fields):
        sink.start_member({"name": "a.log"})
        sink.write(b"redacted")
        return {
            "streamed": True,
            "size": 8,
            "redacted": True,
            "digest": block["digest"],
            "source_size": 8,
            "source_offset": 8,
            "trailing_partial": 0,
            "counts": {},
        }

    monkeypatch.setattr(get_agent_registry(), "send_command_sync", agent)
    with pytest.raises(agent_collector.AgentRedactionError) as caught:
        agent_collector.fetch_archive(source, "logs.zip", ["a.log", "b.log"], tmp_path, block)
    assert caught.value.code == "unverified"
    assert list(tmp_path.iterdir()) == []
