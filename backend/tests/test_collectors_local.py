from app.collectors import local as local_module
from app.models import PatternKind, Protocol, Rule, RuleType, Source


def _source(tmp_path, **overrides) -> Source:
    defaults = dict(
        name="perchtail-logs",
        protocol=Protocol.local,
        host="localhost",
        base_path=str(tmp_path),
    )
    defaults.update(overrides)
    return Source(**defaults)


def _rule(order, type_, pattern, kind=PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_resolve_path_joins_base_and_relative(tmp_path):
    source = _source(tmp_path)
    assert local_module.resolve_path(source, "app.log") == tmp_path / "app.log"
    assert local_module.resolve_path(source, "") == tmp_path


def test_list_directory_always_shows_dirs_but_filters_files(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "app.log").write_text("hello")
    (tmp_path / "secret.txt").write_text("top secret")

    source = _source(tmp_path)
    rules = [_rule(0, RuleType.include, "*.log")]
    entries = {e.name: e for e in local_module.list_directory(source, rules)}

    assert "nested" in entries and entries["nested"].is_dir is True
    assert "app.log" in entries
    assert "secret.txt" not in entries


def test_list_directory_builds_relative_paths_for_nested_calls(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "debug.log").write_text("hello")

    source = _source(tmp_path)
    rules = [_rule(0, RuleType.include, "nested/*.log")]
    entries = local_module.list_directory(source, rules, relative_path="nested")

    assert entries[0].path == "nested/debug.log"


def test_fetch_file_copies_content(tmp_path):
    (tmp_path / "app.log").write_bytes(b"hello world")
    destination = tmp_path / "out" / "copy"
    destination.parent.mkdir()

    source = _source(tmp_path)
    local_module.fetch_file(source, "app.log", destination)

    assert destination.read_bytes() == b"hello world"


def test_local_copy_yields_the_real_path_without_copying(tmp_path):
    (tmp_path / "app.log").write_bytes(b"hello world")
    source = _source(tmp_path)

    with local_module.local_copy(source, "app.log") as path:
        assert path == tmp_path / "app.log"
        assert path.read_bytes() == b"hello world"
