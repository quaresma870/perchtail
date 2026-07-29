from app.models import PatternKind, Rule, RuleType
from app.rules import is_visible, parse_pattern


def _rule(order: int, type_: RuleType, pattern: str, kind: PatternKind = PatternKind.glob) -> Rule:
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_zero_rules_matches_nothing():
    assert is_visible("var/log/app.log", []) is False


def test_single_include_rule_matches():
    rules = [_rule(0, RuleType.include, "var/log/*.log")]
    assert is_visible("var/log/app.log", rules) is True
    assert is_visible("var/log/nested/app.log", rules) is False


def test_double_star_crosses_directories():
    rules = [_rule(0, RuleType.include, "var/log/**/*.log")]
    assert is_visible("var/log/nested/deep/app.log", rules) is True
    # **/ matches zero-or-more segments too, same as .gitignore's a/**/b
    # matching plain a/b.
    assert is_visible("var/log/app.log", rules) is True
    assert is_visible("other/app.log", rules) is False


def test_single_star_does_not_cross_directories():
    rules = [_rule(0, RuleType.include, "var/log/*.log")]
    assert is_visible("var/log/app.log", rules) is True
    assert is_visible("var/log/nested/app.log", rules) is False


def test_double_star_prefix_matches_any_depth():
    rules = [_rule(0, RuleType.include, "**/*.log")]
    assert is_visible("app.log", rules) is True
    assert is_visible("var/log/app.log", rules) is True


def test_question_mark_matches_single_character():
    rules = [_rule(0, RuleType.include, "app.lo?")]
    assert is_visible("app.log", rules) is True
    assert is_visible("app.lo", rules) is False
    assert is_visible("app.logg", rules) is False


def test_last_match_wins_include_then_exclude():
    rules = [
        _rule(0, RuleType.include, "**/*.log"),
        _rule(1, RuleType.exclude, "**/debug.log"),
    ]
    assert is_visible("var/log/app.log", rules) is True
    assert is_visible("var/log/debug.log", rules) is False


def test_last_match_wins_exclude_then_include():
    rules = [
        _rule(0, RuleType.exclude, "**/*.log"),
        _rule(1, RuleType.include, "**/important.log"),
    ]
    assert is_visible("var/log/app.log", rules) is False
    assert is_visible("var/log/important.log", rules) is True


def test_rules_evaluated_by_order_field_not_list_position():
    # Deliberately passed out of insertion order — order field must win.
    rules = [
        _rule(5, RuleType.exclude, "**/*.log"),
        _rule(1, RuleType.include, "**/*.log"),
    ]
    assert is_visible("var/log/app.log", rules) is False


def test_regex_rule():
    rules = [_rule(0, RuleType.include, r"var/log/app-\d+\.log", kind=PatternKind.regex)]
    assert is_visible("var/log/app-42.log", rules) is True
    assert is_visible("var/log/app-abc.log", rules) is False


def test_glob_and_regex_mixed_last_match_wins():
    rules = [
        _rule(0, RuleType.include, "**/*.log"),
        _rule(1, RuleType.exclude, r".*/app-\d+\.log", kind=PatternKind.regex),
    ]
    assert is_visible("var/log/other.log", rules) is True
    assert is_visible("var/log/app-1.log", rules) is False


def test_parse_pattern_defaults_to_glob():
    pattern, kind = parse_pattern("**/*.log")
    assert pattern == "**/*.log"
    assert kind == PatternKind.glob


def test_parse_pattern_strips_re_prefix():
    pattern, kind = parse_pattern(r"re:var/log/app-\d+\.log")
    assert pattern == r"var/log/app-\d+\.log"
    assert kind == PatternKind.regex
