import app.rules as rules_module
import pytest
from app.models import PatternKind, Rule, RuleType
from app.rules import InvalidPatternError, is_safe_relative_path, is_visible, parse_pattern


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


def test_is_safe_relative_path_accepts_normal_paths():
    assert is_safe_relative_path("") is True
    assert is_safe_relative_path("var/log/app.log") is True


def test_is_safe_relative_path_rejects_traversal():
    assert is_safe_relative_path("../etc/passwd") is False
    assert is_safe_relative_path("var/../../etc/passwd") is False
    assert is_safe_relative_path("..") is False


def test_is_safe_relative_path_rejects_absolute_paths():
    assert is_safe_relative_path("/etc/passwd") is False


def test_is_safe_relative_path_rejects_backslash_traversal():
    # collectors/smb.py and collectors/winrm.py join a relative path onto a
    # Windows base_path with backslashes — splitting only on "/" would miss
    # this entirely, since the string contains no forward slash at all.
    assert is_safe_relative_path("..\\..\\windows\\system32\\config\\SAM") is False
    assert is_safe_relative_path("logs\\..\\..\\secret") is False
    assert is_safe_relative_path("\\windows\\system32") is False


def test_is_safe_relative_path_rejects_drive_letter_absolute_paths():
    assert is_safe_relative_path("C:\\Windows\\System32") is False


def test_parse_pattern_rejects_an_invalid_regex():
    with pytest.raises(InvalidPatternError):
        parse_pattern("re:app-(\\d+.log")


def test_parse_pattern_accepts_a_valid_regex_and_any_glob():
    assert parse_pattern("re:app-\\d+\\.log") == ("app-\\d+\\.log", PatternKind.regex)
    assert parse_pattern("logs/[") == ("logs/[", PatternKind.glob)


def test_stored_invalid_include_rule_matches_nothing_instead_of_crashing():
    rules = [
        _rule(0, RuleType.include, "**/*.log", PatternKind.glob),
        _rule(1, RuleType.include, "(", PatternKind.regex),
    ]
    assert is_visible("app.log", rules)
    assert not is_visible("app.txt", rules)


def test_stored_invalid_exclude_rule_fails_closed():
    # An exclude that can't be evaluated must not quietly expose what it
    # was written to hide, so it hides everything it might have covered.
    rules = [
        _rule(0, RuleType.include, "**/*", PatternKind.glob),
        _rule(1, RuleType.exclude, "secret(", PatternKind.regex),
    ]
    assert not is_visible("secret(1).log", rules)
    assert not is_visible("app.log", rules)


@pytest.mark.parametrize("pattern", ["re:(a)\\1", "re:app(?=\\.log)", "re:(?<!x)y"])
def test_rule_regexes_reject_syntax_re2_cannot_run_in_linear_time(pattern):
    with pytest.raises(InvalidPatternError, match="RE2"):
        parse_pattern(pattern)


def test_catastrophic_backtracking_pattern_evaluates_quickly():
    # (a+)+$ against "aaa...b" takes exponential time with a backtracking
    # engine -- with Python's re this blocks for seconds; RE2 is linear.
    import time

    rules = [_rule(0, RuleType.include, "(a+)+$", PatternKind.regex)]
    started = time.monotonic()
    assert not is_visible("a" * 26 + "b", rules)
    assert time.monotonic() - started < 0.5


# --- directory visibility (#164) -------------------------------------------


def _r(order, type_, pattern, kind=PatternKind.glob):
    return Rule(source_id=1, order=order, type=type_, pattern=pattern, pattern_kind=kind)


def test_excluded_folder_is_hidden_with_its_whole_subtree():
    rules = [_r(0, RuleType.include, "**/*"), _r(1, RuleType.exclude, "secret/**")]
    assert rules_module.is_dir_visible("app", rules)
    assert not rules_module.is_dir_visible("secret", rules)
    assert not rules_module.is_dir_visible("secret/deeper/still", rules)


def test_folders_outside_every_include_are_hidden():
    rules = [_r(0, RuleType.include, "app/**")]
    assert rules_module.is_dir_visible("app", rules)
    assert rules_module.is_dir_visible("app/x/y", rules)
    assert not rules_module.is_dir_visible("other", rules)


def test_folders_leading_to_a_deep_include_are_shown_and_nothing_else():
    rules = [_r(0, RuleType.include, "var/app/*.log")]
    assert rules_module.is_dir_visible("var", rules)
    assert rules_module.is_dir_visible("var/app", rules)
    assert not rules_module.is_dir_visible("var/other", rules)
    assert not rules_module.is_dir_visible("var/app/sub", rules)


def test_root_level_glob_shows_no_folders():
    # `*.log` never crosses `/`, so it can only match files at the root.
    assert not rules_module.is_dir_visible("nested", [_r(0, RuleType.include, "*.log")])


def test_zero_rules_show_no_folders():
    assert not rules_module.is_dir_visible("anything", [])
    assert rules_module.is_dir_visible("", [])  # the source root itself


def test_a_later_include_reopens_only_the_path_to_it():
    rules = [
        _r(0, RuleType.include, "**/*"),
        _r(1, RuleType.exclude, "secret/**"),
        _r(2, RuleType.include, "secret/keep/*.log"),
    ]
    assert rules_module.is_dir_visible("secret", rules)
    assert rules_module.is_dir_visible("secret/keep", rules)
    assert not rules_module.is_dir_visible("secret/other", rules)


def test_an_ancestor_exclude_hides_descendants():
    rules = [_r(0, RuleType.include, "**/*.log"), _r(1, RuleType.exclude, "a/**")]
    assert not rules_module.is_dir_visible("a/b/c", rules)


def test_exclude_order_matters_like_for_files():
    # An exclude *before* the include doesn't cancel it (last match wins).
    rules = [_r(0, RuleType.exclude, "secret/**"), _r(1, RuleType.include, "**/*")]
    assert rules_module.is_dir_visible("secret", rules)


def test_case_insensitive_sources_hide_differently_cased_folders():
    rules = [_r(0, RuleType.include, "**/*"), _r(1, RuleType.exclude, "secret/**")]
    assert rules_module.is_dir_visible("SECRET", rules)
    assert not rules_module.is_dir_visible("SECRET", rules, case_insensitive=True)


def test_regex_rules_are_treated_conservatively():
    regex_include = [_r(0, RuleType.include, "^app/.*\\.log$", PatternKind.regex)]
    assert rules_module.is_dir_visible("other", regex_include)  # can't analyse: visible
    regex_exclude = [
        _r(0, RuleType.include, "**/*"),
        _r(1, RuleType.exclude, "^secret/", PatternKind.regex),
    ]
    assert rules_module.is_dir_visible("secret", regex_exclude)  # not assumed to cover it


def test_glob_exclude_with_wildcards_covers_matching_folders():
    rules = [_r(0, RuleType.include, "**/*"), _r(1, RuleType.exclude, "**/private-*/**")]
    assert not rules_module.is_dir_visible("x/private-hr", rules)
    assert rules_module.is_dir_visible("x/public", rules)


def test_a_name_with_a_control_character_is_never_visible():
    # RE2's `$` is end of text only: `logs/secret.key\n` slipped past
    # `exclude logs/*.key` while `include logs/*` still matched.
    from app.rules import is_entry_visible

    rules = [
        _rule(1, RuleType.include, "logs/*"),
        _rule(2, RuleType.exclude, "logs/*.key"),
    ]
    assert not is_visible("logs/secret.key", rules)
    assert not is_visible("logs/secret.key\n", rules)
    assert not is_entry_visible("logs/app\x07.log", False, rules)
    assert not is_entry_visible("logs/odd\tdir", True, [_rule(1, RuleType.include, "**")])
    assert is_visible("logs/app.log", rules)
