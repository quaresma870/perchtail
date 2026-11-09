import base64
from unittest.mock import MagicMock

import pytest
from app.collectors import agent as agent_module
from app.models import PatternKind, Protocol, Rule, RuleType, Source


def _source(**overrides) -> Source:
    defaults = dict(
        id=7,
        name="remote-app",
        protocol=Protocol.agent,
        host="agent",
        base_path="/var/log/app",
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def _mock_registry(monkeypatch, send_command_sync_result=None, side_effect=None):
    fake_registry = MagicMock()
    if side_effect is not None:
        fake_registry.send_command_sync.side_effect = side_effect
    else:
        fake_registry.send_command_sync.return_value = send_command_sync_result
    monkeypatch.setattr(agent_module, "get_agent_registry", lambda: fake_registry)
    return fake_registry


def test_list_directory_sends_a_list_command_with_the_path(monkeypatch):
    fake_registry = _mock_registry(
        monkeypatch,
        send_command_sync_result={
            "entries": [
                {"name": "app.log", "is_dir": False, "size": 123},
                {"name": "nested", "is_dir": True, "size": 0},
            ]
        },
    )
    source = _source()
    entries = agent_module.list_directory(source, [_rule(0, RuleType.include, "**/*.log")])

    fake_registry.send_command_sync.assert_called_once_with(
        source.id, "list", path="", max_entries=agent_module.AGENT_LIST_MAX_ENTRIES
    )
    by_name = {e.name: e for e in entries}
    assert by_name["app.log"].path == "app.log"
    assert by_name["nested"].is_dir is True


def test_list_directory_filters_files_through_rules_but_keeps_all_dirs(monkeypatch):
    _mock_registry(
        monkeypatch,
        send_command_sync_result={
            "entries": [
                {"name": "app.log", "is_dir": False, "size": 1},
                {"name": "secret.txt", "is_dir": False, "size": 1},
                {"name": "nested", "is_dir": True, "size": 0},
            ]
        },
    )
    source = _source()
    entries = agent_module.list_directory(source, [_rule(0, RuleType.include, "**/*.log")])
    names = {e.name for e in entries}
    assert names == {"app.log", "nested"}


def test_list_directory_builds_relative_paths_for_nested_calls(monkeypatch):
    fake_registry = _mock_registry(
        monkeypatch,
        send_command_sync_result={"entries": [{"name": "debug.log", "is_dir": False, "size": 1}]},
    )
    source = _source()
    entries = agent_module.list_directory(
        source, [_rule(0, RuleType.include, "nested/*.log")], relative_path="nested"
    )
    fake_registry.send_command_sync.assert_called_once_with(
        source.id, "list", path="nested", max_entries=agent_module.AGENT_LIST_MAX_ENTRIES
    )
    assert entries[0].path == "nested/debug.log"


def test_fetch_file_writes_decoded_bytes_to_destination(monkeypatch, tmp_path):
    content = b"hello from the agent"
    _mock_registry(
        monkeypatch,
        send_command_sync_result={"content_b64": base64.b64encode(content).decode("ascii")},
    )
    destination = tmp_path / "fetched"
    agent_module.fetch_file(_source(), "app.log", destination)
    assert destination.read_bytes() == content


def test_local_copy_yields_a_temp_path_with_the_fetched_content(monkeypatch):
    content = b"log line one\nlog line two\n"
    _mock_registry(
        monkeypatch,
        send_command_sync_result={"content_b64": base64.b64encode(content).decode("ascii")},
    )
    with agent_module.local_copy(_source(), "app.log") as path:
        assert path.read_bytes() == content


def test_windows_agent_rules_ignore_case(monkeypatch):
    """#189: NTFS treats SECRET and secret as one folder, so an exclude
    written as secret/** must hide SECRET/ on a Windows agent, as it does
    over SMB and WinRM. Other agents keep case-sensitive rules."""
    from app.rules import rules_case_insensitive

    _mock_registry(
        monkeypatch,
        send_command_sync_result={
            "entries": [
                {"name": "SECRET", "is_dir": True, "size": 0},
                {"name": "App.LOG", "is_dir": False, "size": 1},
            ]
        },
    )
    rules = [
        _rule(0, RuleType.include, "**/*.log"),
        _rule(1, RuleType.exclude, "secret/**"),
    ]

    windows = _source(agent_os="windows")
    assert rules_case_insensitive(windows) is True
    assert [e.name for e in agent_module.list_directory(windows, rules)] == ["App.LOG"]

    for other in (_source(agent_os="linux"), _source(agent_os=None)):
        assert rules_case_insensitive(other) is False
        assert [e.name for e in agent_module.list_directory(other, rules)] == ["SECRET"]


def test_fetch_file_asks_for_chunks_and_writes_the_stream(monkeypatch, tmp_path):
    """#205: the agent streams the file into the destination as it comes."""
    fake_registry = _mock_registry(monkeypatch, send_command_sync_result=None)

    def stream(source_id, command_type, *, sink, limit, **fields):
        sink.write(b"chunk one, ")
        sink.write(b"chunk two")
        return {"type": "fetch_result", "streamed": True, "chunks": 2, "size": 20}

    fake_registry.send_command_sync.side_effect = stream
    destination = tmp_path / "fetched"
    agent_module.fetch_file(_source(), "app.log", destination)

    assert destination.read_bytes() == b"chunk one, chunk two"
    fields = fake_registry.send_command_sync.call_args.kwargs
    assert fields["chunk_size"] == agent_module.FETCH_CHUNK_BYTES
    assert fields["max_bytes"] == fields["limit"]


def test_fetch_file_maps_too_large_to_413_and_leaves_nothing_behind(monkeypatch, tmp_path):
    from app.agent_registry import AgentCommandError
    from app.collectors.base import FetchTooLargeError

    fake_registry = _mock_registry(monkeypatch, send_command_sync_result=None)

    def refuse(source_id, command_type, *, sink, limit, **fields):
        sink.write(b"partial")
        raise AgentCommandError("too big", code="too_large", size=limit + 10)

    fake_registry.send_command_sync.side_effect = refuse
    destination = tmp_path / "fetched"
    with pytest.raises(FetchTooLargeError):
        agent_module.fetch_file(_source(), "big.log", destination)
    assert not destination.exists()


# --- incremental follow (#254) ----------------------------------------------


def test_read_range_sends_the_offset_and_a_capped_length(monkeypatch):
    registry = _mock_registry(
        monkeypatch,
        send_command_sync_result={
            "size": 18,
            "head_b64": base64.b64encode(b"line").decode(),
            "data_b64": base64.b64encode(b"line two\n").decode(),
        },
    )
    assert agent_module.read_range(_source(), "app.log", 9, 10**9, 10**9) == (
        18,
        b"line",
        b"line two\n",
    )
    registry.send_command_sync.assert_called_once_with(
        7, "read_range", path="app.log", offset=9, length=1 << 20, head_length=4096
    )


def test_an_agent_older_than_0_6_cant_read_a_range(monkeypatch):
    from app.agent_registry import AgentCommandError
    from app.collectors.base import RangeNotSupportedError

    _mock_registry(monkeypatch, side_effect=AgentCommandError('unknown command type "read_range"'))
    with pytest.raises(RangeNotSupportedError):
        agent_module.read_range(_source(), "app.log", 0, 10)


def test_an_agent_sending_too_much_is_refused(monkeypatch):
    from app.collectors.base import SourceError

    too_much = base64.b64encode(b"x" * ((1 << 20) + 1)).decode()
    _mock_registry(monkeypatch, send_command_sync_result={"size": 5, "data_b64": too_much})
    with pytest.raises(SourceError):
        agent_module.read_range(_source(), "app.log", 0, 10)
