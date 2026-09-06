import base64
from unittest.mock import MagicMock

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

    fake_registry.send_command_sync.assert_called_once_with(source.id, "list", path="")
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
    entries = agent_module.list_directory(source, [_rule(0, RuleType.include, "*.log")])
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
    fake_registry.send_command_sync.assert_called_once_with(source.id, "list", path="nested")
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
