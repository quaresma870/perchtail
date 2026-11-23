import base64
import json
import re

import app.collectors.winrm as winrm_module
import pytest
from app.models import PatternKind, Protocol, Rule, RuleType, Source


class FakeResult:
    def __init__(self, status_code: int = 0, std_out: bytes = b"", std_err: bytes = b""):
        self.status_code = status_code
        self.std_out = std_out
        self.std_err = std_err


class FakeProtocol:
    """The pywinrm Protocol calls of fetch_file's streamed read, recorded in
    order. Each Receive answers the next of the responses the session's
    stream_handler gave for the script; an exception in them is raised."""

    def __init__(self, owner):
        self.owner = owner
        self.events: list[str] = []
        self.responses: list = []
        self.transport = type(
            "Transport", (), {"close_session": lambda _: self.events.append("close_session")}
        )()

    def open_shell(self):
        self.events.append("open_shell")
        return "shell-1"

    def run_command(self, shell_id, command):
        self.events.append("run_command")
        prefix = "powershell -encodedcommand "
        assert command.startswith(prefix)
        script = base64.b64decode(command[len(prefix) :]).decode("utf-16-le")
        self.owner.streamed.append(script)
        self.responses = list(self.owner.stream_handler(script))
        return "command-1"

    def get_command_output_raw(self, shell_id, command_id):
        self.events.append("receive")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def cleanup_command(self, shell_id, command_id):
        self.events.append("cleanup_command")

    def close_shell(self, shell_id, close_session=True):
        self.events.append("close_shell")


class FakeWinRMSession:
    def __init__(self):
        self.scripts_run: list[str] = []
        self.streamed: list[str] = []
        self.envelopes: list[int] = []
        self.handler = lambda script: FakeResult()
        self.stream_handler = lambda script: [(b"", b"boom", 1, True)]
        self.protocol = FakeProtocol(self)

    def run_ps(self, script):
        self.scripts_run.append(script)
        return self.handler(script)

    def _clean_error_msg(self, msg):
        return msg


@pytest.fixture()
def fake_session(monkeypatch):
    session = FakeWinRMSession()

    def factory(source, envelope_size):
        session.envelopes.append(envelope_size)
        return session

    monkeypatch.setattr(winrm_module, "_session", factory)
    monkeypatch.setattr(
        winrm_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    return session


def _source(**overrides) -> Source:
    defaults = dict(
        name="win01",
        protocol=Protocol.winrm,
        host="win01.example.com",
        base_path="C:\\Logs\\AppName",
        credential_ref="encrypted",
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_ps_quote_escapes_single_quotes():
    assert winrm_module._ps_quote("it's a test") == "'it''s a test'"


@pytest.mark.parametrize("quote", ["\u2018", "\u2019", "\u201a", "\u201b"])
def test_ps_quote_escapes_unicode_single_quotes(quote):
    assert winrm_module._ps_quote(f"a{quote}b") == f"'a{quote}{quote}b'"


def test_ps_quote_leaves_no_unpaired_quote_inside_the_string():
    value = "x'\u2018\u2019\u201a\u201b'y"
    inner = winrm_module._ps_quote(value)[1:-1]
    for quote in ("'", "\u2018", "\u2019", "\u201a", "\u201b"):
        assert quote not in inner.replace(quote * 2, ""), quote


def test_remote_path_joins_base_and_relative():
    source = _source()
    assert winrm_module._remote_path(source) == "C:\\Logs\\AppName"
    assert (
        winrm_module._remote_path(source, "nested/app.log") == "C:\\Logs\\AppName\\nested\\app.log"
    )


@pytest.mark.parametrize(
    "base_path",
    ["\\\\fileserver\\share\\logs", "\\\\?\\C:\\Logs", "\\\\.\\C:\\Logs", "Logs", ""],
)
def test_a_saved_base_path_off_a_drive_is_never_read(fake_session, base_path):
    """Refused when saved; one saved before that check never reaches the
    host, which would otherwise reach another machine with its own
    credentials."""
    source = _source(base_path=base_path)
    with pytest.raises(winrm_module.BasePathNotOnDriveError) as caught:
        winrm_module.list_directory(source, [], "")
    assert "edit the source" in caught.value.public_message
    with pytest.raises(winrm_module.BasePathNotOnDriveError):
        winrm_module.read_range(source, "app.log", 0, 10)
    with pytest.raises(winrm_module.BasePathNotOnDriveError):
        winrm_module.list_directory(_source(base_path=base_path, winrm_configuration="PT"), [], "")
    assert fake_session.scripts_run == []


def test_list_directory_always_shows_dirs_but_filters_files(fake_session):
    payload = json.dumps(
        [
            {"Name": "nested", "PSIsContainer": True, "Length": None},
            {"Name": "app.log", "PSIsContainer": False, "Length": 100},
            {"Name": "secret.txt", "PSIsContainer": False, "Length": 5},
        ]
    ).encode()
    fake_session.handler = lambda script: FakeResult(std_out=base64.b64encode(payload))

    rules = [_rule(0, RuleType.include, "**/*.log")]
    entries = {e.name: e for e in winrm_module.list_directory(_source(), rules)}

    assert "nested" in entries and entries["nested"].is_dir is True
    assert "app.log" in entries and entries["app.log"].size == 100
    assert "secret.txt" not in entries


def test_list_directory_handles_a_single_result_object(fake_session):
    # Get-ChildItem | ConvertTo-Json emits a bare object, not a list, when
    # there's exactly one match.
    payload = json.dumps({"Name": "app.log", "PSIsContainer": False, "Length": 10}).encode()
    fake_session.handler = lambda script: FakeResult(std_out=base64.b64encode(payload))

    entries = winrm_module.list_directory(_source(), [_rule(0, RuleType.include, "*.log")])
    assert entries[0].name == "app.log"


def test_list_directory_empty_output_returns_empty_list(fake_session):
    fake_session.handler = lambda script: FakeResult(std_out=b"")
    assert winrm_module.list_directory(_source(), []) == []


def test_run_ps_raises_on_nonzero_status(fake_session):
    fake_session.handler = lambda script: FakeResult(status_code=1, std_err=b"boom")
    with pytest.raises(RuntimeError):
        winrm_module._run_ps(_source(), "whatever")


def _read_output(content: bytes, script: str, end: int | None = None) -> bytes:
    """What the read script writes for `content`: one base64 line per
    sub-block, up to the size it was given, then the end marker."""
    size = int(re.search(r"\$left = \[long\](\d+);", script).group(1))
    block = int(re.search(r"New-Object byte\[\] (\d+);", script).group(1))
    content = content[:size]
    lines = [
        base64.b64encode(content[i : i + block]) + b"\r\n" for i in range(0, len(content), block)
    ]
    total = len(content) if end is None else end
    return b"".join(lines) + f"PERCHTAIL_END {total}\r\n".encode()


def _responses(out: bytes, cut: int = 0, code: int = 0, err: bytes = b"") -> list:
    """`out` as Receive answers of `cut` bytes each (one answer when 0); only
    the last says the command is done."""
    pieces = [out[i : i + cut] for i in range(0, len(out), cut)] if cut else [out]
    answers = [(piece, b"", -1, False) for piece in pieces[:-1]]
    return answers + [(pieces[-1] if pieces else b"", err, code, True)]


def _serve_file(session, content: bytes, size: int | None = None, cut: int = 0):
    """Answers fetch_file()'s size request and its streamed read of
    `content` (reporting `size` as the file's size when given)."""

    def handler(script):
        assert "Get-Item" in script, script
        return FakeResult(std_out=str(len(content) if size is None else size).encode())

    session.handler = handler
    session.stream_handler = lambda script: _responses(_read_output(content, script), cut)


def test_fetch_file_decodes_base64_output(fake_session, tmp_path):
    content = b"hello world"
    _serve_file(fake_session, content)

    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)
    assert destination.read_bytes() == content


def test_fetch_file_reads_through_one_shell_and_one_command(fake_session, tmp_path, monkeypatch):
    """T-0075: one request for the size, then one shell and one PowerShell
    command for all the content, however many sub-blocks it takes."""
    monkeypatch.setattr(winrm_module, "_SUB_BLOCK_BYTES", 4)
    content = b"0123456789abc"
    _serve_file(fake_session, content, cut=5)

    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)

    assert destination.read_bytes() == content
    assert len(fake_session.scripts_run) == 1  # the size
    events = fake_session.protocol.events
    assert events.count("open_shell") == 1
    assert events.count("run_command") == 1
    assert events[0:2] == ["open_shell", "run_command"]
    assert events[-3:] == ["cleanup_command", "close_shell", "close_session"]
    # Four base64 lines and the end marker, five bytes per answer.
    assert events.count("receive") > 4
    (script,) = fake_session.streamed
    assert "New-Object byte[] 4;" in script and "$left = [long]13;" in script


def test_the_sub_block_is_3_mib():
    assert winrm_module._SUB_BLOCK_BYTES == 3 * 1024 * 1024
    script = winrm_module._read_script(_source(), "app.log", 10)
    assert f"New-Object byte[] {3 * 1024 * 1024};" in script


def test_the_read_never_uses_run_ps_or_get_command_output(fake_session, tmp_path):
    _serve_file(fake_session, b"hello world")
    fake_session.protocol.get_command_output = lambda *a: pytest.fail("whole output kept")
    winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert not any("[IO.File]::Open(" in script for script in fake_session.scripts_run)


def test_lines_cut_between_answers_are_written_as_they_complete(tmp_path):
    """Only whole lines are decoded and written; what is kept is at most the
    unfinished line."""
    import io

    dst = io.BytesIO()
    stream = winrm_module._ReadStream(dst, 9)
    first, second = base64.b64encode(b"abc"), base64.b64encode(b"defghi")
    stream.feed(first[:2])
    assert dst.getvalue() == b"" and stream.pending == first[:2]
    stream.feed(first[2:] + b"\r\n" + second[:3])
    assert dst.getvalue() == b"abc" and stream.pending == second[:3]
    stream.feed(second[3:] + b"\r\nPERCHTAIL_END 9\r\n")
    assert dst.getvalue() == b"abcdefghi" and stream.pending == b""
    stream.finish()
    assert (stream.written, stream.end) == (9, 9)


@pytest.mark.parametrize("cut", [1, 3, 7, 1000])
def test_a_read_cut_anywhere_between_answers_arrives_whole(fake_session, tmp_path, cut):
    import os

    content = os.urandom(50)
    _serve_file(fake_session, content, cut=cut)
    winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == content


def test_a_line_longer_than_a_block_is_refused(monkeypatch):
    import io

    monkeypatch.setattr(winrm_module, "_MAX_LINE_CHARS", 8)
    stream = winrm_module._ReadStream(io.BytesIO(), 100)
    with pytest.raises(winrm_module.collector_base.SourceError):
        stream.feed(b"QUJDREVGR0hJ")


@pytest.mark.parametrize(
    "out, code",
    [
        # The command ended without its end marker (cut short).
        (base64.b64encode(b"hello") + b"\r\n", 0),
        # The end marker doesn't match what arrived.
        (base64.b64encode(b"hello") + b"\r\nPERCHTAIL_END 11\r\n", 0),
        # Nothing after the end marker.
        (b"PERCHTAIL_END 0\r\n" + base64.b64encode(b"hello") + b"\r\n", 0),
        # Not base64.
        (b"not base64!\r\nPERCHTAIL_END 0\r\n", 0),
        # More than the size it was given.
        (base64.b64encode(b"hello world, and more") + b"\r\nPERCHTAIL_END 21\r\n", 0),
    ],
)
def test_a_read_that_doesnt_end_right_fails_and_leaves_nothing(fake_session, tmp_path, out, code):
    _serve_file(fake_session, b"hello world")
    fake_session.stream_handler = lambda script: _responses(out, 4, code)
    destination = tmp_path / "out"
    with pytest.raises(winrm_module.collector_base.SourceError):
        winrm_module.fetch_file(_source(), "app.log", destination)
    assert not destination.exists()
    assert fake_session.protocol.events[-2:] == ["close_shell", "close_session"]


def test_a_read_that_exits_non_zero_fails_and_leaves_nothing(fake_session, tmp_path):
    _serve_file(fake_session, b"hello world")
    good = _read_output(b"hello world", winrm_module._read_script(_source(), "app.log", 11))
    fake_session.stream_handler = lambda script: _responses(good, 0, 1, b"Access denied")
    destination = tmp_path / "out"
    with pytest.raises(RuntimeError, match="Access denied"):
        winrm_module.fetch_file(_source(), "app.log", destination)
    assert not destination.exists()


def test_an_error_mid_read_closes_the_shell_and_leaves_nothing(fake_session, tmp_path):
    import requests

    _serve_file(fake_session, b"hello world")
    good = base64.b64encode(b"hello") + b"\r\n"
    fake_session.stream_handler = lambda script: [
        (good, b"", -1, False),
        requests.exceptions.ConnectionError("reset"),
    ]
    destination = tmp_path / "out"
    with pytest.raises(requests.exceptions.ConnectionError):
        winrm_module.fetch_file(_source(), "app.log", destination)
    assert not destination.exists()
    assert fake_session.protocol.events[-3:] == ["cleanup_command", "close_shell", "close_session"]


def test_an_operation_timeout_while_waiting_is_asked_again(fake_session, tmp_path):
    from winrm.exceptions import WinRMOperationTimeoutError

    _serve_file(fake_session, b"hello world")
    good = _read_output(b"hello world", winrm_module._read_script(_source(), "app.log", 11))
    fake_session.stream_handler = lambda script: [WinRMOperationTimeoutError()] + _responses(good)
    winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"hello world"


def test_a_file_that_shrank_ends_with_what_was_read(fake_session, tmp_path):
    """Rotated mid-read: the script stops at the end of the file and says
    how much it read."""
    _serve_file(fake_session, b"hello", size=11)
    winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"hello"
    assert "$left = [long]11;" in fake_session.streamed[0]


def test_a_file_that_grew_is_read_only_up_to_its_size(fake_session, tmp_path):
    _serve_file(fake_session, b"hello world, grown since", size=11)
    winrm_module.fetch_file(_source(), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"hello world"
    script = fake_session.streamed[0]
    assert "$left = [long]11;" in script and "while ($left -gt 0)" in script


def test_the_read_script_fills_each_sub_block(fake_session):
    script = winrm_module._read_script(_source(), "app.log", 11)
    # Read() may return less than asked: it loops until full or EOF.
    assert "while ($n -lt $want) { $r = $f.Read($b, $n, $want - $n);" in script
    assert script.endswith('"PERCHTAIL_END $t" } finally { $f.Close() }')


def test_fetch_file_refuses_a_file_over_max_fetch_mb(fake_session, tmp_path, monkeypatch):
    from app.collectors.base import FetchTooLargeError

    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    _serve_file(fake_session, b"x" * 100)

    destination = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        winrm_module.fetch_file(_source(), "app.log", destination)
    assert not destination.exists()
    assert fake_session.protocol.events == []


def test_local_copy_fetches_into_a_temp_path(fake_session):
    content = b"hello world"
    _serve_file(fake_session, content)

    with winrm_module.local_copy(_source(), "app.log") as path:
        assert path.read_bytes() == content


def test_list_directory_decodes_non_ascii_names(fake_session):
    """#168: an accented name used to break the whole listing, because the
    console wrote it in the OEM code page and the bytes weren't UTF-8."""
    payload = json.dumps(
        [
            {"Name": "ação-é.log", "PSIsContainer": False, "Length": 3},
            {"Name": "aspas ’curvas“ ”.log", "PSIsContainer": False, "Length": 4},
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    seen = []

    def handler(script):
        seen.append(script)
        return FakeResult(std_out=base64.b64encode(payload))

    fake_session.handler = handler

    entries = winrm_module.list_directory(_source(), [_rule(0, RuleType.include, "**/*")])

    assert {e.name for e in entries} == {"ação-é.log", "aspas ’curvas“ ”.log"}
    assert "ToBase64String" in seen[0] and "UTF8" in seen[0]


def test_remote_path_of_a_drive_root_keeps_the_backslash():
    """ "D:" alone is the current directory on D:, not its root."""
    source = _source(base_path="D:\\")
    assert winrm_module._remote_path(source) == "D:\\"
    assert winrm_module._remote_path(source, "Logs/app.log") == "D:\\Logs\\app.log"


def test_every_component_below_the_base_is_checked_for_reparse_points(fake_session):
    seen = []

    def handler(script):
        seen.append(script)
        return FakeResult(std_out=base64.b64encode(b"[]"))

    fake_session.handler = handler
    winrm_module.list_directory(_source(), [_rule(0, RuleType.include, "**")], "jx/deep")

    script = seen[0]
    assert "[IO.File]::GetAttributes($__c) -band [IO.FileAttributes]::ReparsePoint" in script
    assert "'C:\\Logs\\AppName\\jx', 'C:\\Logs\\AppName\\jx\\deep'" in script
    # Junctions and symlinks inside the folder are filtered out, not followed.
    assert "Where-Object { -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) }" in script
    assert script.index("GetAttributes") < script.index("Get-ChildItem")


def test_root_listing_has_no_guard_but_still_filters_links(fake_session):
    seen = []
    fake_session.handler = lambda s: seen.append(s) or FakeResult(std_out=b"")
    winrm_module.list_directory(_source(), [])
    assert "GetAttributes" not in seen[0]
    assert "ReparsePoint" in seen[0]


def test_a_path_through_a_junction_is_refused(fake_session, tmp_path):
    from app.collectors.base import PathOutsideBaseError

    fake_session.handler = lambda s: FakeResult(
        status_code=3, std_out=b"PERCHTAIL_OUTSIDE_BASE\r\n"
    )
    with pytest.raises(PathOutsideBaseError):
        winrm_module.list_directory(_source(), [_rule(0, RuleType.include, "**")], "jx")
    with pytest.raises(PathOutsideBaseError):
        winrm_module.fetch_file(_source(), "jx/secret.txt", tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_the_guard_runs_before_the_file_is_opened_once(fake_session, tmp_path, monkeypatch):
    """T-0075 (replaces the guard on every block): the read checks every
    component for links before it opens the file, and reads all of it
    through that one handle, so a link swapped in afterwards can't divert it."""
    monkeypatch.setattr(winrm_module, "_SUB_BLOCK_BYTES", 4)
    _serve_file(fake_session, b"0123456789")
    winrm_module.fetch_file(_source(), "nested/app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"0123456789"
    assert "GetAttributes" in fake_session.scripts_run[0]  # the size request
    (script,) = fake_session.streamed
    assert "'C:\\Logs\\AppName\\nested', 'C:\\Logs\\AppName\\nested\\app.log'" in script
    assert (
        script.index("GetAttributes")
        < script.index("[IO.File]::Exists(")
        < script.index("[IO.File]::Open(")
    )
    assert script.count("[IO.File]::Open(") == 1
    assert "'Open', 'Read', 'ReadWrite'" in script
    assert "Seek(" not in script
    assert script.count("$f.Close()") == 1 and script.index("$f.Close()") > script.index("$f.Read(")


def test_listing_a_file_is_not_found(fake_session):
    from app.collectors.base import SourceNotFoundError
    from app.collectors.registry import CONNECTORS
    from app.models import Protocol

    fake_session.handler = lambda s: FakeResult(status_code=4, std_out=b"PERCHTAIL_NOT_A_DIRECTORY")
    connector = CONNECTORS[Protocol.winrm]
    original = connector._module._session
    with pytest.raises(SourceNotFoundError):
        connector.list_directory(_source(), [_rule(0, RuleType.include, "**")], "jx/secret.txt")
    assert connector._module._session is original


@pytest.mark.parametrize(
    "marker, error",
    [
        (b"PERCHTAIL_OUTSIDE_BASE", "PathOutsideBaseError"),
        (b"PERCHTAIL_NOT_FOUND", "FileNotFoundError"),
    ],
)
def test_a_marker_before_the_data_stops_the_read(
    fake_session, tmp_path, monkeypatch, marker, error
):
    """A junction swapped in, or the file removed, between the size request
    and the read: the read's own checks stop it with the same errors as the
    other requests, and a junction is logged (#206)."""
    from app.collectors import base

    _serve_file(fake_session, b"0123456789")
    code = 3 if marker == b"PERCHTAIL_OUTSIDE_BASE" else 5
    fake_session.stream_handler = lambda script: _responses(marker + b"\r\n", 0, code)
    events = []
    monkeypatch.setattr(
        winrm_module.logger, "warning", lambda event, **kw: events.append((event, kw))
    )
    expected = base.PathOutsideBaseError if error == "PathOutsideBaseError" else FileNotFoundError
    with pytest.raises(expected):
        winrm_module.fetch_file(_source(), "nested/app.log", tmp_path / "out")
    assert not (tmp_path / "out").exists()
    if expected is base.PathOutsideBaseError:
        assert events == [
            ("winrm.path_outside_base", {"source_id": None, "path": "nested/app.log"})
        ]
    else:
        assert events == []
    assert fake_session.protocol.events[-2:] == ["close_shell", "close_session"]


# --- the envelope size (T-0075) ----------------------------------------------


def _fault(subcode="w:EncodingLimit", reason="The envelope size is too large"):
    from winrm.exceptions import WSManFaultError

    return WSManFaultError(
        code=500,
        message="Bad HTTP response returned from server. Code 500",
        response="<s:Envelope/>",
        reason=reason,
        fault_subcode=subcode,
        wsman_fault_code=2150858817,
    )


def test_every_request_asks_for_a_512000_byte_envelope(monkeypatch):
    """The header pywinrm sends, not only what it was told."""
    import xml.etree.ElementTree as ET

    session = winrm_module._Session(
        "https://win01:5986/wsman",
        auth=("svc", "s3cret"),
        envelope_size=winrm_module.ENVELOPE_SIZE,
        transport="ntlm",
        server_cert_validation="validate",
    )
    sent = []

    def send_message(message):
        sent.append(message)
        return b'<Envelope><Selector Name="ShellId">shell-1</Selector></Envelope>'

    monkeypatch.setattr(session.protocol.transport, "send_message", send_message)
    assert session.protocol.open_shell() == "shell-1"
    root = ET.fromstring(sent[0])
    (size,) = [n for n in root.iter() if n.tag.endswith("}MaxEnvelopeSize")]
    assert size.text == "512000" == str(winrm_module.ENVELOPE_SIZE)
    header = session.protocol.build_wsman_header(action="a", resource_uri="r")
    assert header["env:Header"]["w:MaxEnvelopeSize"]["#text"] == "512000"


def test_the_session_factory_asks_for_the_size_it_is_given(monkeypatch):
    monkeypatch.setattr(
        winrm_module, "decrypt_credential", lambda ref: {"username": "svc", "password": "s3cret"}
    )
    for size in (winrm_module.ENVELOPE_SIZE, winrm_module.FALLBACK_ENVELOPE_SIZE):
        session = winrm_module._session(_source(), size)
        header = session.protocol.build_wsman_header(action="a", resource_uri="r")
        assert header["env:Header"]["w:MaxEnvelopeSize"]["#text"] == str(size)


def _capture_warnings(monkeypatch) -> list:
    events = []
    monkeypatch.setattr(
        winrm_module.logger, "warning", lambda event, **kw: events.append((event, kw))
    )
    return events


@pytest.mark.parametrize(
    "fault",
    [
        _fault(),
        _fault(subcode="w:InternalError", reason="The MaxEnvelopeSize requested is too large"),
    ],
)
def test_a_refused_envelope_size_is_asked_again_once_smaller(fake_session, monkeypatch, fault):
    events = _capture_warnings(monkeypatch)
    calls = []

    def handler(script):
        calls.append(fake_session.envelopes[-1])
        if fake_session.envelopes[-1] == winrm_module.ENVELOPE_SIZE:
            raise fault
        return FakeResult(std_out=b"ok")

    fake_session.handler = handler
    assert winrm_module._run_ps(_source(id=7), "whatever") == "ok"
    assert fake_session.envelopes == [512000, 153600]
    assert events == [("winrm.envelope_fallback", {"source_id": 7})]


def test_a_smaller_envelope_refused_too_is_not_asked_again(fake_session, monkeypatch):
    _capture_warnings(monkeypatch)

    def handler(script):
        raise _fault()

    fake_session.handler = handler
    with pytest.raises(Exception, match="envelope size"):
        winrm_module._run_ps(_source(), "whatever")
    assert fake_session.envelopes == [512000, 153600]


@pytest.mark.parametrize(
    "fault",
    [
        _fault(subcode="w:AccessDenied", reason="Access is denied."),
        ConnectionError("reset"),
    ],
)
def test_any_other_refusal_is_not_asked_again(fake_session, monkeypatch, fault):
    events = _capture_warnings(monkeypatch)

    def handler(script):
        raise fault

    fake_session.handler = handler
    with pytest.raises(type(fault)):
        winrm_module._run_ps(_source(), "whatever")
    assert fake_session.envelopes == [512000]
    assert events == []


def test_a_fetch_falls_back_once_for_the_size_and_the_read(fake_session, tmp_path, monkeypatch):
    events = _capture_warnings(monkeypatch)
    _serve_file(fake_session, b"hello world")
    size_handler = fake_session.handler

    def handler(script):
        if fake_session.envelopes[-1] == winrm_module.ENVELOPE_SIZE:
            raise _fault()
        return size_handler(script)

    fake_session.handler = handler
    winrm_module.fetch_file(_source(id=3), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"hello world"
    assert fake_session.envelopes == [512000, 153600, 153600]
    assert events == [("winrm.envelope_fallback", {"source_id": 3})]


def test_a_read_refused_its_envelope_is_read_again_from_the_start(
    fake_session, tmp_path, monkeypatch
):
    events = _capture_warnings(monkeypatch)
    _serve_file(fake_session, b"hello world")
    serve = fake_session.stream_handler

    def stream_handler(script):
        if fake_session.envelopes[-1] == winrm_module.ENVELOPE_SIZE:
            return [_fault()]
        return serve(script)

    fake_session.stream_handler = stream_handler
    winrm_module.fetch_file(_source(id=4), "app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"hello world"
    assert fake_session.envelopes == [512000, 512000, 153600]
    assert events == [("winrm.envelope_fallback", {"source_id": 4})]


def test_list_and_range_requests_ask_for_the_larger_envelope(fake_session):
    fake_session.handler = lambda script: FakeResult(std_out=b"")
    winrm_module.list_directory(_source(), [])
    winrm_module.list_drives(_source())
    assert fake_session.envelopes == [512000, 512000]


# --- incremental follow (#254) ----------------------------------------------


def test_read_range_seeks_and_reads_in_one_round_trip(fake_session):
    content = b"line one\nline two\n"

    def handler(script):
        offset = int(re.search(r"if \((\d+) -lt \$len\)", script).group(1))
        head, length = map(int, re.findall(r"New-Object byte\[\] (\d+)", script))
        data = content[offset : offset + length] if offset < len(content) else b""
        out = f"{len(content)}|{base64.b64encode(content[:head]).decode()}|"
        return FakeResult(std_out=out.encode() + base64.b64encode(data))

    fake_session.handler = handler
    assert winrm_module.read_range(_source(), "app.log", 9, 1000, 4) == (
        18,
        b"line",
        b"line two\n",
    )
    assert winrm_module.read_range(_source(), "app.log", 40, 1000) == (18, b"", b"")
    assert len(fake_session.scripts_run) == 2
    # Opened like fetch_file, tolerating the writer, and the length is capped.
    assert "'Read', 'ReadWrite'" in fake_session.scripts_run[0]
    winrm_module.read_range(_source(), "app.log", 0, 10**12)
    assert f"byte[] {1 << 20}" in fake_session.scripts_run[-1]


def test_read_range_loops_for_the_head_and_refuses_garbage(fake_session):
    script_text = {}

    def handler(script):
        script_text["last"] = script
        return FakeResult(std_out=b"not the expected answer")

    fake_session.handler = handler
    with pytest.raises(winrm_module.collector_base.SourceError):
        winrm_module.read_range(_source(), "app.log", 0, 10, 4)
    # A short Read() mustn't leave the head short: it reads until full or EOF.
    assert "while ($hn -lt $h.Length)" in script_text["last"]


# --- a file that isn't there (#272, found in the 0.6.0-rc.1 lab) -----------


def _missing_file(script):
    # What Windows does: the existence check stops the script with the marker.
    if "[IO.File]::Exists(" in script:
        return FakeResult(status_code=5, std_out=b"PERCHTAIL_NOT_FOUND\r\n")
    return FakeResult()


def test_fetch_file_of_a_missing_file_is_not_found_not_empty(fake_session, tmp_path):
    from app.collectors.base import SourceNotFoundError
    from app.collectors.errors import translate_winrm

    fake_session.handler = _missing_file
    destination = tmp_path / "out"
    with pytest.raises(FileNotFoundError) as caught:
        winrm_module.fetch_file(_source(), "app/gone.log", destination)
    assert not destination.exists()
    # The check runs before the size is read, after the junction guard.
    script = fake_session.scripts_run[0]
    assert script.index("[IO.File]::Exists(") < script.index("Get-Item")
    assert script.index("ReparsePoint") < script.index("[IO.File]::Exists(")
    assert isinstance(translate_winrm(caught.value), SourceNotFoundError)


def test_read_range_of_a_missing_file_is_not_found(fake_session):
    fake_session.handler = _missing_file
    with pytest.raises(FileNotFoundError):
        winrm_module.read_range(_source(), "app/gone.log", 0, 10, 4)
    script = fake_session.scripts_run[0]
    assert script.index("[IO.File]::Exists(") < script.index("[IO.File]::Open(")
