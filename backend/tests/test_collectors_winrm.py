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


class FakeWinRMSession:
    def __init__(self):
        self.scripts_run: list[str] = []
        self.handler = lambda script: FakeResult()

    def run_ps(self, script):
        self.scripts_run.append(script)
        return self.handler(script)


@pytest.fixture()
def fake_session(monkeypatch):
    session = FakeWinRMSession()
    monkeypatch.setattr(winrm_module, "_session", lambda source: session)
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


def _serve_file(content: bytes):
    """Handler for the size + byte-range scripts fetch_file() runs."""
    import re

    def handler(script):
        if "Get-Item" in script:
            return FakeResult(std_out=str(len(content)).encode())
        m = re.search(r"Seek\((\d+),.*New-Object byte\[\] (\d+)", script, re.S)
        assert m, script
        offset, length = int(m.group(1)), int(m.group(2))
        return FakeResult(std_out=base64.b64encode(content[offset : offset + length]))

    return handler


def test_fetch_file_decodes_base64_output(fake_session, tmp_path):
    content = b"hello world"
    fake_session.handler = _serve_file(content)

    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)
    assert destination.read_bytes() == content


def test_fetch_file_streams_in_chunks_instead_of_one_read(fake_session, tmp_path, monkeypatch):
    monkeypatch.setattr(winrm_module, "_CHUNK_BYTES", 4)
    content = b"0123456789abc"
    fake_session.handler = _serve_file(content)

    destination = tmp_path / "out"
    winrm_module.fetch_file(_source(), "app.log", destination)

    assert destination.read_bytes() == content
    assert not any("ReadAllBytes" in script for script in fake_session.scripts_run)
    assert sum("Seek(" in script for script in fake_session.scripts_run) == 4


def test_fetch_file_refuses_a_file_over_max_fetch_mb(fake_session, tmp_path, monkeypatch):
    from app.collectors.base import FetchTooLargeError

    monkeypatch.setattr("app.collectors.base.max_fetch_bytes", lambda: 8)
    fake_session.handler = _serve_file(b"x" * 100)

    destination = tmp_path / "out"
    with pytest.raises(FetchTooLargeError):
        winrm_module.fetch_file(_source(), "app.log", destination)
    assert not destination.exists()
    assert not any("Seek(" in script for script in fake_session.scripts_run)


def test_local_copy_fetches_into_a_temp_path(fake_session):
    content = b"hello world"
    fake_session.handler = _serve_file(content)

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


def test_every_read_block_repeats_the_guard(fake_session, tmp_path, monkeypatch):
    monkeypatch.setattr(winrm_module, "_CHUNK_BYTES", 4)
    fake_session.handler = _serve_file(b"0123456789")
    winrm_module.fetch_file(_source(), "nested/app.log", tmp_path / "out")
    assert (tmp_path / "out").read_bytes() == b"0123456789"
    assert all("GetAttributes" in s for s in fake_session.scripts_run)


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


def test_a_junction_swapped_in_mid_read_is_refused_and_logged(fake_session, tmp_path, monkeypatch):
    """#206: the re-check on every block refused it, but only the first
    check logged winrm.path_outside_base."""
    from app.collectors.base import PathOutsideBaseError

    monkeypatch.setattr(winrm_module, "_CHUNK_BYTES", 4)
    serve = _serve_file(b"0123456789")

    def handler(script):
        if "Seek(4," in script:
            return FakeResult(status_code=3, std_out=b"PERCHTAIL_OUTSIDE_BASE\r\n")
        return serve(script)

    fake_session.handler = handler
    events = []
    monkeypatch.setattr(
        winrm_module.logger, "warning", lambda event, **kw: events.append((event, kw))
    )
    with pytest.raises(PathOutsideBaseError):
        winrm_module.fetch_file(_source(), "nested/app.log", tmp_path / "out")
    assert not (tmp_path / "out").exists()
    assert events == [
        ("winrm.path_outside_base", {"source_id": None, "path": "nested/app.log", "offset": 4})
    ]


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
