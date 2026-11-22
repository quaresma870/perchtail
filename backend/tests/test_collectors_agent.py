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


def _mock_registry(
    monkeypatch, send_command_sync_result=None, side_effect=None, features=("links/1",)
):
    fake_registry = MagicMock()
    fake_registry.is_connected.return_value = True
    fake_registry.features.return_value = frozenset(features)
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
    over SMB and WinRM. Other agents keep case-sensitive include rules;
    exclude rules ignore case everywhere."""
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
        # SECRET is hidden by the exclude whatever the case; App.LOG isn't
        # matched by the case-sensitive include.
        assert [e.name for e in agent_module.list_directory(other, rules)] == []


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
        7,
        "read_range",
        path="app.log",
        expect_real="app.log",
        offset=9,
        length=1 << 20,
        head_length=4096,
        require_feature="links/1",
    )


def test_an_agent_error_on_a_range_read_is_not_taken_for_an_old_agent(monkeypatch):
    """Every agent asked for a range knows read_range (0.6), since one
    without links/1 (older than 0.7) is refused first: an error naming an
    unknown command is an agent error like any other, not a fallback."""
    from app.agent_registry import AgentCommandError

    _mock_registry(monkeypatch, side_effect=AgentCommandError('unknown command type "read_range"'))
    with pytest.raises(AgentCommandError):
        agent_module.read_range(_source(), "app.log", 0, 10)


def test_an_agent_sending_too_much_is_refused(monkeypatch):
    from app.collectors.base import SourceError

    too_much = base64.b64encode(b"x" * ((1 << 20) + 1)).decode()
    _mock_registry(monkeypatch, send_command_sync_result={"size": 5, "data_b64": too_much})
    with pytest.raises(SourceError):
        agent_module.read_range(_source(), "app.log", 0, 10)


# --- F4 (rc.3): which limit a redacted fetch passed --------------------------


@pytest.fixture()
def equal_limits(monkeypatch):
    """MAX_FETCH_MB and MAX_DECOMPRESSED_MB the same number, as by default:
    the number in the agent's message can't tell them apart."""
    from app.config import get_settings

    monkeypatch.setenv("MAX_FETCH_MB", "1")
    monkeypatch.setenv("MAX_DECOMPRESSED_MB", "1")
    get_settings.cache_clear()
    yield 1024**2
    get_settings.cache_clear()


def _refuse_as_too_large(limit_bytes: int, limit: str | None):
    from app.agent_registry import AgentCommandError

    def refuse(source_id, command_type, **fields):
        raise AgentCommandError(
            f"file is {limit_bytes * 2} bytes, over the {limit_bytes}-byte limit",
            code="too_large",
            size=limit_bytes * 2,
            limit=limit,
        )

    return refuse


@pytest.mark.parametrize(
    "fetch", ["gz", "member", "fetch_archive"], ids=["gz", "member", "fetch_archive"]
)
def test_an_agent_too_large_over_max_fetch_names_max_fetch_mb(
    monkeypatch, tmp_path, equal_limits, fetch
):
    """An archive or .gz file over MAX_FETCH_MB as stored is refused with
    the MAX_FETCH_MB message, not MAX_DECOMPRESSED_MB's, when the agent
    says the fetch limit was passed, even with the two limits equal."""
    from app.collectors.base import FetchTooLargeError

    _mock_registry(
        monkeypatch,
        side_effect=_refuse_as_too_large(equal_limits, "fetch"),
        features=("links/1", "redaction/1"),
    )
    with pytest.raises(FetchTooLargeError, match="MAX_FETCH_MB"):
        _expanding_fetch(fetch, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "fetch", ["gz", "member", "fetch_archive"], ids=["gz", "member", "fetch_archive"]
)
def test_an_agent_too_large_once_decompressed_names_max_decompressed_mb(
    monkeypatch, tmp_path, equal_limits, fetch
):
    from app.archives import DecompressionLimitError

    _mock_registry(
        monkeypatch,
        side_effect=_refuse_as_too_large(equal_limits, "decompressed"),
        features=("links/1", "redaction/1"),
    )
    with pytest.raises(DecompressionLimitError):
        _expanding_fetch(fetch, tmp_path)


@pytest.mark.parametrize(
    "fetch", ["gz", "member", "fetch_archive"], ids=["gz", "member", "fetch_archive"]
)
def test_an_older_agent_too_large_is_still_guessed_from_its_message(
    monkeypatch, tmp_path, equal_limits, fetch
):
    """An agent that doesn't say which limit: as before, the number in its
    message decides, so with the limits equal it is taken for
    MAX_DECOMPRESSED_MB."""
    from app.archives import DecompressionLimitError

    _mock_registry(
        monkeypatch,
        side_effect=_refuse_as_too_large(equal_limits, None),
        features=("links/1", "redaction/1"),
    )
    with pytest.raises(DecompressionLimitError):
        _expanding_fetch(fetch, tmp_path)


def test_a_plain_redacted_fetch_over_the_limit_is_max_fetch_mb(monkeypatch, tmp_path, equal_limits):
    from app.collectors.base import FetchTooLargeError

    for limit in ("fetch", None):
        _mock_registry(
            monkeypatch,
            side_effect=_refuse_as_too_large(equal_limits, limit),
            features=("links/1", "redaction/1"),
        )
        with pytest.raises(FetchTooLargeError, match="MAX_FETCH_MB"):
            agent_module.fetch_redacted(
                _source(), "app.log", tmp_path / "fetched", _block(), target_check=None
            )


def _block() -> dict:
    return agent_module.redaction_block(7, [], "utf-8")


def _expanding_fetch(fetch: str, directory):
    if fetch == "gz":
        return agent_module.fetch_redacted(
            _source(), "app.log.gz", directory / "fetched", _block(), decompress=True
        )
    if fetch == "member":
        return agent_module.fetch_redacted(
            _source(), "logs.zip", directory / "fetched", _block(), member="a.log"
        )
    return agent_module.fetch_archive(_source(), "logs.zip", ["a.log"], directory, _block())
