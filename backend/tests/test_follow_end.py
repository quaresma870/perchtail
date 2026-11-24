"""Opening a large file at its end, and "Load earlier": GET /follow with
`end` reads only what comes before bytes the viewer already holds -- the
bytes of [offset, end), or, redacted, the whole lines that end by `end` --
over every connector the Tail reads through, and the audit of each window
read before the offset already audited."""

from types import SimpleNamespace

import app.collectors.smb as smb_module
import app.collectors.ssh as ssh_module
import pytest
from app.agent_registry import get_agent_registry
from app.api.archive import FOLLOW_HEAD_BYTES, FOLLOW_WINDOW_BYTES
from app.api.archive import router as archive_router
from app.api.auth import get_current_active_user
from app.api.errors import install as install_error_handlers
from app.auth.models import AuditLog, Role, User
from app.collectors.base import RANGE_MAX_BYTES
from app.db import get_session
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.redaction import long_line_label, redact_bytes
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from tests.test_collectors_smb import FakeSMBClient
from tests.test_collectors_ssh import FakeSFTPClient, FakeSSHClient
from tests.test_link_targets_agent import FsAgent
from tests.test_redaction_agent import FakeAgent
from tests.test_redaction_endpoints import (
    SAMPLE,
    _enable_redaction,
    all_rules,
    assert_no_secret,
)

_SSH_BASE = "/var/log/appname"
_SMB_ROOT = "\\\\fileserver.example.com\\AppLogs"

# Lines of many lengths, with a value of every detector and a private key
# block every so often, ending with a newline: about 2.6 MiB, so it takes
# the first window and two of Load earlier.
_SAMPLE_LINES = SAMPLE.splitlines(keepends=True)
_KEY_BLOCK = _SAMPLE_LINES[-3:]
_LINES = [
    line
    for n in range(22_000)
    for line in (
        _KEY_BLOCK
        if n % 500 == 499
        else [f"{n:06d} {'x' * (n % 97)} {_SAMPLE_LINES[n % (len(_SAMPLE_LINES) - 3)]}"]
    )
]
CONTENT = "".join(_LINES).encode()


class _SMBFiles(FakeSMBClient):
    """FakeSMBClient that tells files from folders."""

    def lstat(self, path, **kwargs):
        if path in self.files:
            return SimpleNamespace(st_file_attributes=0, st_size=len(self.files[path]))
        return SimpleNamespace(st_file_attributes=0x10, st_size=0)

    def stat(self, path, **kwargs):
        self.stat_kwargs.append(kwargs)
        return self.lstat(path)


def _app(session, source: Source) -> TestClient:
    app = FastAPI()
    app.include_router(archive_router)
    install_error_handlers(app)
    app.dependency_overrides[get_session] = lambda: session
    role = Role(name="super", is_super_admin=True)
    session.add(role)
    session.commit()
    user = User(username="admin", role_id=role.id)
    session.add(user)
    session.add(source)
    session.commit()
    session.add(
        Rule(
            source_id=source.id,
            order=0,
            type=RuleType.include,
            pattern="**/*",
            pattern_kind=PatternKind.glob,
        )
    )
    session.commit()
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


@pytest.fixture()
def reader(request, session, tmp_path, monkeypatch):
    """(client, source, write): a source of the protocol asked for
    (`request.param`: local, ssh, smb, agent, or agent-redacting, an agent
    that redacts on its host) and `write(content)`, which puts app.log on
    it."""
    protocol = request.param
    folder = tmp_path / "logs"
    folder.mkdir()
    registry = get_agent_registry()
    if protocol == "local":
        source = Source(name="s", protocol=Protocol.local, host="localhost", base_path=str(folder))

        def write(content: bytes) -> None:
            (folder / "app.log").write_bytes(content)

    elif protocol == "ssh":
        source = Source(
            name="s", protocol=Protocol.ssh, host="web-01", base_path=_SSH_BASE, credential_ref="x"
        )
        sftp = FakeSFTPClient(files={}, dirs={_SSH_BASE})
        monkeypatch.setattr(ssh_module, "_connect", lambda source: FakeSSHClient(sftp))

        def write(content: bytes) -> None:
            sftp._files[f"{_SSH_BASE}/app.log"] = content

    elif protocol == "smb":
        source = Source(
            name="s",
            protocol=Protocol.smb,
            host="fileserver.example.com",
            base_path="AppLogs",
            credential_ref="x",
        )
        smb = _SMBFiles()
        monkeypatch.setattr(smb_module, "smbclient", smb)
        monkeypatch.setattr(
            smb_module, "decrypt_credential", lambda ref: {"username": "u", "password": "p"}
        )

        def write(content: bytes) -> None:
            smb.files[f"{_SMB_ROOT}\\app.log"] = content

    elif protocol == "agent":
        source = Source(name="s", protocol=Protocol.agent, host="files-01", base_path="/var/log")
        agent = FsAgent(str(folder))
        monkeypatch.setattr(registry, "send_command_sync", agent)
        monkeypatch.setattr(registry, "is_connected", lambda source_id: True)
        monkeypatch.setattr(registry, "features", lambda source_id: agent.features)

        def write(content: bytes) -> None:
            (folder / "app.log").write_bytes(content)

    else:
        assert protocol == "agent-redacting"
        source = Source(name="s", protocol=Protocol.agent, host="files-01", base_path="/var/log")
        agent = FakeAgent({})
        monkeypatch.setattr(registry, "send_command_sync", agent)
        monkeypatch.setattr(registry, "redacts_on_host", lambda source_id: True)

        def write(content: bytes) -> None:
            agent.files["app.log"] = content

        write.agent = agent  # type: ignore[attr-defined]

    client = _app(session, source)
    return client, source, write


def _follow(client, source, offset, **extra):
    return client.get(
        f"/sources/{source.id}/follow", params={"path": "app.log", "offset": offset, **extra}
    )


def _shown(response) -> bytes:
    """What the viewer shows of an answer: all of it but an unfinished last
    line's marker."""
    partial = int(response.headers.get("x-follow-partial", "0"))
    return response.content[: len(response.content) - partial]


def _first_line_cut(content: bytes, offset: int) -> int:
    """Where the viewer starts what a window read from `offset` shows, not
    redacted (cutToFirstLine): past its first, cut line, unless it starts
    the file or no line starts in it (no newline, or only its last byte)."""
    newline = content.find(b"\n")
    if offset <= 0 or newline in (-1, len(content) - 1):
        return 0
    return newline + 1


def _open_from_end_then_load_earlier(client, source, size: int) -> tuple[bytes, list]:
    """What the viewer does: the last RANGE_MAX_BYTES through Follow, then
    Load earlier until the start of the file. Returns what it shows and
    every answer."""
    offset = max(0, size - RANGE_MAX_BYTES)
    first = _follow(client, source, offset, start="true")
    assert first.status_code == 200, first.text
    answers = [first]
    redacted = first.headers.get("x-redacted") == "1"
    if redacted:
        start = int(first.headers["x-follow-start"])
        shown = _shown(first)
    else:
        cut = first.content.index(b"\n") + 1 if offset > 0 else 0
        start, shown = offset + cut, first.content[cut:]
    head = first.headers["x-follow-head"]
    # Redacted, the context read before the offset counts in the answer's
    # size: what is left is read at once, as the Tail does.
    more = first
    while "x-follow-more" in more.headers:
        more = _follow(client, source, int(more.headers["x-follow-offset"]), head=head)
        assert more.status_code == 200, more.text
        answers.append(more)
        shown += _shown(more)
        head = more.headers["x-follow-head"]
    while start > 0:
        offset = max(0, start - RANGE_MAX_BYTES)
        response = _follow(client, source, offset, end=start, head=head)
        assert response.status_code == 200, response.text
        assert "x-follow-reset" not in response.headers
        assert "x-follow-more" not in response.headers
        answers.append(response)
        if redacted:
            assert response.headers["x-follow-partial"] == "0"
            next_start = int(response.headers["x-follow-start"])
            earlier = response.content
        else:
            assert response.headers["x-follow-offset"] == str(start)
            cut = _first_line_cut(response.content, offset)
            next_start, earlier = offset + cut, response.content[cut:]
        assert next_start < start
        start, shown = next_start, earlier + shown
    return shown, answers


_PLAIN = ["local", "ssh", "smb", "agent"]


@pytest.mark.parametrize("reader", _PLAIN, indirect=True)
def test_load_earlier_rebuilds_the_whole_file(reader):
    """From the end to the start, a window at a time, the viewer ends up
    with the file exactly: nothing repeated, nothing lost."""
    client, source, write = reader
    write(CONTENT)
    shown, answers = _open_from_end_then_load_earlier(client, source, len(CONTENT))
    assert shown == CONTENT
    assert len(answers) == 3


@pytest.mark.parametrize("reader", ["local", "ssh", "smb", "agent-redacting"], indirect=True)
def test_load_earlier_redacted_rebuilds_the_whole_redacted_file(reader, session):
    """Redacted (on the server, or on the agent's host), the windows are
    whole redacted lines that join up to the whole file redacted, and no
    value shows."""
    client, source, write = reader
    _enable_redaction(session)
    write(CONTENT)
    shown, answers = _open_from_end_then_load_earlier(client, source, len(CONTENT))
    assert_no_secret(shown)
    assert shown == redact_bytes(CONTENT, all_rules(), final=True)[0]
    assert all(a.headers["x-redacted"] == "1" for a in answers)
    assert all(a.headers["cache-control"] == "no-store" for a in answers)


@pytest.mark.parametrize("reader", ["agent-redacting"], indirect=True)
def test_line_offsets_are_asked_of_the_agent_only_when_needed(reader, session):
    """X-Follow-Start needs the agent's line offsets: asked when a Tail
    starts and for a read up to `end`, not on every poll."""
    client, source, write = reader
    _enable_redaction(session)
    write(CONTENT)
    agent = write.agent
    size = len(CONTENT)
    first = _follow(client, source, size - 100, start="true")
    assert "x-follow-start" in first.headers
    poll = _follow(client, source, size, head=first.headers["x-follow-head"])
    assert "x-follow-start" not in poll.headers
    earlier = _follow(
        client,
        source,
        size - 300,
        end=int(first.headers["x-follow-start"]),
        head=first.headers["x-follow-head"],
    )
    assert "x-follow-start" in earlier.headers
    asked = [fields.get("with_line_offsets", False) for kind, fields in agent.commands]
    assert asked == [True, False, True]


@pytest.mark.parametrize("reader", ["local"], indirect=True)
def test_end_answers_exactly_the_range(reader):
    client, source, write = reader
    write(CONTENT)
    response = _follow(client, source, 1000, end=5000)
    assert response.status_code == 200
    assert response.content == CONTENT[1000:5000]
    assert response.headers["x-follow-offset"] == "5000"
    assert response.headers["x-file-size"] == str(len(CONTENT))
    assert "x-follow-more" not in response.headers
    assert "x-follow-reset" not in response.headers
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("reader", ["local"], indirect=True)
def test_a_window_of_the_largest_size_is_read_whole(reader):
    client, source, write = reader
    write(CONTENT)
    end = len(CONTENT) - 10
    response = _follow(client, source, end - RANGE_MAX_BYTES, end=end)
    assert response.content == CONTENT[end - RANGE_MAX_BYTES : end]


@pytest.mark.parametrize("reader", ["local", "agent-redacting"], indirect=True)
def test_end_redacted_sends_only_whole_lines_that_end_by_it(reader, session):
    """A line that runs past `end` isn't sent, not even as a marker: `end`
    is the start of a line the viewer already shows."""
    client, source, write = reader
    _enable_redaction(session)
    write(CONTENT)
    line_starts = [0]
    for line in _LINES:
        line_starts.append(line_starts[-1] + len(line.encode()))
    end = line_starts[40] + 7  # inside line 40
    response = _follow(client, source, line_starts[30], end=end)
    assert response.status_code == 200
    expected = redact_bytes("".join(_LINES[30:40]).encode(), all_rules(), final=True)[0]
    assert response.content == expected
    assert response.headers["x-follow-partial"] == "0"
    assert response.headers["x-follow-start"] == str(line_starts[30])
    assert response.headers["x-follow-offset"] == str(line_starts[40])
    assert_no_secret(response.content)


@pytest.mark.parametrize("reader", ["local"], indirect=True)
def test_end_redacted_starts_at_the_next_whole_line(reader, session):
    client, source, write = reader
    _enable_redaction(session)
    write(CONTENT)
    line_starts = [0]
    for line in _LINES:
        line_starts.append(line_starts[-1] + len(line.encode()))
    response = _follow(client, source, line_starts[30] + 3, end=line_starts[40])
    assert response.headers["x-follow-aligned"] == "1"
    assert response.headers["x-follow-start"] == str(line_starts[31])
    assert (
        response.content
        == redact_bytes("".join(_LINES[31:40]).encode(), all_rules(), final=True)[0]
    )


@pytest.mark.parametrize(
    "reader,redacted",
    [("local", True), ("local", False), ("agent-redacting", True)],
    indirect=["reader"],
)
def test_a_line_longer_than_a_window_becomes_a_marker_and_load_earlier_goes_on(
    reader, redacted, session
):
    """A line with no start in the window: redacted (on the server or on the
    agent's host), it can't be redacted whole and is shown as the long-line
    marker; not redacted, the window is kept whole. Either way the next
    window ends where this one started, so Load earlier still reaches the
    start."""
    client, source, write = reader
    if redacted:
        _enable_redaction(session)
    long_line = b"password=hunter2 " + b"y" * (2 * RANGE_MAX_BYTES) + b"\n"
    content = b"first line\n" + long_line + b"last line\n"
    write(content)
    end = len(content) - len(b"last line\n")
    response = _follow(client, source, end - RANGE_MAX_BYTES, end=end)
    assert response.status_code == 200, response.text
    if redacted:
        assert response.content == f"[redacted: {long_line_label(FOLLOW_WINDOW_BYTES)}]\n".encode()
        start = int(response.headers["x-follow-start"])
        assert start < end
    else:
        assert response.content == content[end - RANGE_MAX_BYTES : end]
        assert _first_line_cut(response.content, end - RANGE_MAX_BYTES) == 0
    shown, _answers = _open_from_end_then_load_earlier(client, source, len(content))
    assert shown.startswith(b"first line\n")
    assert shown.endswith(b"last line\n")
    if redacted:
        assert b"hunter2" not in shown
    else:
        assert shown == content


@pytest.mark.parametrize("reader", ["local"], indirect=True)
@pytest.mark.parametrize(
    "offset,end",
    [(10, 10), (10, 9), (0, RANGE_MAX_BYTES + 1), (5, RANGE_MAX_BYTES + 6), (0, 0), (0, -1)],
)
def test_an_end_out_of_bounds_is_refused(reader, offset, end):
    client, source, write = reader
    write(CONTENT)
    response = _follow(client, source, offset, end=end)
    assert response.status_code == 422


@pytest.mark.parametrize("reader", ["local", "agent-redacting"], indirect=True)
def test_a_file_that_shrank_or_changed_resets(reader, session):
    client, source, write = reader
    if source.protocol == Protocol.agent:
        _enable_redaction(session)
    write(CONTENT)
    size = len(CONTENT)
    first = _follow(client, source, size - 1000, start="true")
    head = first.headers["x-follow-head"]
    start = int(first.headers.get("x-follow-start", size - 1000))

    same = _follow(client, source, start - 2000, end=start, head=head)
    assert "x-follow-reset" not in same.headers

    write(CONTENT[: start - 10])
    shrank = _follow(client, source, start - 2000, end=start, head=head)
    assert shrank.headers["x-follow-reset"] == "1"
    assert shrank.content == b""

    write(b"Z" * FOLLOW_HEAD_BYTES + CONTENT)
    rotated = _follow(client, source, start - 2000, end=start, head=head)
    assert rotated.headers["x-follow-reset"] == "1"


@pytest.mark.parametrize("reader", ["local"], indirect=True)
def test_a_window_near_the_start_still_checks_the_head(reader):
    """The viewer's head covers the first FOLLOW_HEAD_BYTES; a window that
    starts before them is checked against them too, not reset for it."""
    client, source, write = reader
    write(CONTENT)
    head = _follow(client, source, len(CONTENT), start="true").headers["x-follow-head"]
    response = _follow(client, source, 0, end=30, head=head)
    assert "x-follow-reset" not in response.headers
    assert response.content == CONTENT[:30]


@pytest.mark.parametrize("reader", ["local"], indirect=True)
def test_each_window_read_before_the_audited_offset_is_audited(reader, session):
    """The audit is the Tail's: file.follow with the offset of the first
    window, again for each Load earlier (it reads before the offset
    audited), and not for a window read again or the Tail's polls."""
    client, source, write = reader
    write(CONTENT)
    size = len(CONTENT)

    def events():
        rows = session.exec(select(AuditLog).where(AuditLog.action == "file.follow")).all()
        return [row.event_metadata["offset"] for row in rows]

    first_offset = size - RANGE_MAX_BYTES
    first = _follow(client, source, first_offset, start="true")
    head = first.headers["x-follow-head"]
    assert events() == [first_offset]

    earlier = first_offset + 50 - RANGE_MAX_BYTES
    _follow(client, source, earlier, end=first_offset + 50, head=head)
    assert events() == [first_offset, earlier]

    _follow(client, source, earlier, end=first_offset + 50, head=head)
    _follow(client, source, size, head=head)
    assert events() == [first_offset, earlier]

    _follow(client, source, 0, end=earlier + 10, head=head)
    assert events() == [first_offset, earlier, 0]
