"""Property tests for path containment and the rule engine (security
scanning, level 2). The earlier advisories (P1-P3) were all about reaching
files outside a source's base, so these check the invariants on arbitrary
inputs rather than on hand-picked examples. The Go agent has the matching
fuzz tests (agent/internal/agent/fuzz_test.go)."""

import os
import posixpath

import pytest
from app.collectors import local
from app.collectors.base import PathOutsideBaseError
from app.collectors.ssh import _is_within
from app.models import PatternKind, Protocol, Rule, RuleType, Source
from app.rules import (
    _rule_matches,
    is_safe_relative_path,
    is_safe_windows_relative_path,
    is_visible,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Paths built from the characters that matter for traversal, plus ordinary ones.
_TRICKY = st.sampled_from(["/", "\\", ".", "..", ":", "~", " ", "\x00", "\n", "%2e"])
_PLAIN = st.text(alphabet="abcXYZ019_-", min_size=1, max_size=6)
paths = st.lists(st.one_of(_TRICKY, _PLAIN), max_size=12).map("".join)
# A canonical segment: no separator, colon or control character, not "." or "..".
segments = st.text(
    alphabet=st.characters(
        blacklist_characters="/\\:", blacklist_categories=("Cc",), max_codepoint=0x2FF
    ),
    min_size=1,
    max_size=8,
).filter(lambda s: s not in (".", ".."))


@given(paths)
def test_an_accepted_path_is_canonical_and_stays_under_the_base(path):
    if not is_safe_relative_path(path):
        return
    if path == "":
        return
    assert posixpath.normpath(path) == path
    assert not path.startswith("/")
    assert ".." not in path.split("/")
    assert "\\" not in path and ":" not in path


@given(st.lists(segments, min_size=1, max_size=6))
def test_every_canonical_path_is_accepted(parts):
    """The check mustn't reject what listings themselves produce."""
    assert is_safe_relative_path("/".join(parts))


@given(st.lists(segments, min_size=1, max_size=6))
def test_windows_check_refuses_names_windows_would_rewrite(parts):
    path = "/".join(parts)
    if is_safe_windows_relative_path(path):
        assert not any(part.endswith((".", " ")) for part in parts)


@given(
    st.lists(_PLAIN, min_size=1, max_size=3).map(lambda p: "/" + "/".join(p)),
    _PLAIN,
)
def test_ssh_containment_does_not_confuse_a_sibling_with_the_base(root, suffix):
    """`/base-evil` is not inside `/base`, though it starts with it."""
    assert not _is_within(root + suffix, root)
    assert _is_within(root + "/" + suffix, root)
    assert _is_within(root, root)


_OUTSIDE = "OUTSIDE-THE-BASE-SECRET"


@pytest.fixture(scope="module")
def escape_source(tmp_path_factory) -> Source:
    """A local source whose base holds a symlinked folder and a symlinked
    file that both lead outside it, next to a marked "secret"."""
    root = tmp_path_factory.mktemp("containment")
    base, outside = root / "base", root / "outside"
    (base / "nested").mkdir(parents=True)
    outside.mkdir()
    (base / "app.log").write_text("inside\n")
    (outside / "secret.log").write_text(_OUTSIDE + "\n")
    os.symlink(outside, base / "escape")
    os.symlink(outside / "secret.log", base / "link.log")
    return Source(name="local", protocol=Protocol.local, host="localhost", base_path=str(base))


_escape_paths = st.sampled_from(
    ["escape/secret.log", "link.log", "../outside/secret.log", "nested/../link.log", "escape"]
)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=300)
@given(path=st.one_of(paths, _escape_paths))
def test_the_local_connector_never_reads_outside_the_base(escape_source, path):
    source = escape_source
    base = os.path.realpath(source.base_path)
    try:
        resolved = local.resolve_path(source, path)
    except (local.LocalPathNotAllowedError, PathOutsideBaseError, ValueError, OSError):
        return
    assert str(resolved).startswith(os.path.join(base, ""))
    if resolved.is_file():
        assert _OUTSIDE not in resolved.read_text(errors="replace")


_rule_patterns = st.sampled_from(
    ["**", "*.log", "app/**", "app/*.log", "secret/**", "**/x.log", "re:\\.log$"]
)
_rules = st.lists(
    st.tuples(st.sampled_from([RuleType.include, RuleType.exclude]), _rule_patterns),
    max_size=6,
)
_file_paths = st.sampled_from(
    [
        "app.log",
        "app/a.log",
        "app/b.txt",
        "secret/x.log",
        "secret/deep/x.log",
        "x.log",
        "other/x.log",
    ]
)


def _build(rules):
    built = []
    for order, (kind, pattern) in enumerate(rules):
        is_regex = pattern.startswith("re:")
        built.append(
            Rule(
                source_id=1,
                order=order,
                type=kind,
                pattern=pattern[3:] if is_regex else pattern,
                pattern_kind=PatternKind.regex if is_regex else PatternKind.glob,
            )
        )
    return built


@given(_rules, _file_paths)
def test_the_last_matching_rule_wins(rules, path):
    built = _build(rules)
    matching = [rule for rule in built if _rule_matches(path, rule, False)]
    expected = bool(matching) and matching[-1].type == RuleType.include
    assert is_visible(path, built) == expected


@given(_rules, _file_paths)
def test_a_final_exclude_everything_hides_everything(rules, path):
    built = _build(rules + [(RuleType.exclude, "**")])
    assert not is_visible(path, built)


@given(_file_paths)
def test_no_rules_shows_nothing(path):
    assert not is_visible(path, [])
