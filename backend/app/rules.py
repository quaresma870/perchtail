import re
from functools import lru_cache

from app.models import PatternKind, Rule, RuleType


def parse_pattern(raw: str) -> tuple[str, PatternKind]:
    """Splits the admin-facing `re:` prefix convention (CLAUDE.md's rule
    matching semantics) from the pattern itself."""
    if raw.startswith("re:"):
        return raw[len("re:") :], PatternKind.regex
    return raw, PatternKind.glob


@lru_cache(maxsize=2048)
def _compile_glob(pattern: str) -> re.Pattern[str]:
    """Translates an rsync/gitignore-style glob to a regex: `**/` matches
    zero or more whole path segments (so `**/*.log` matches both `app.log`
    and `var/log/app.log` — same as .gitignore), a standalone `**` matches
    anything including `/`, `*` matches within a single segment, and `?`
    matches one character within a segment. Unlike stdlib fnmatch, this is
    path-separator-aware, which is the whole point of distinguishing `*`
    from `**`."""
    out = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i : i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile(f"^{''.join(out)}$")


def _rule_matches(path: str, rule: Rule) -> bool:
    if rule.pattern_kind == PatternKind.regex:
        return re.search(rule.pattern, path) is not None
    return _compile_glob(rule.pattern).match(path) is not None


def is_visible(path: str, rules: list[Rule]) -> bool:
    """Rules are evaluated in `order`, last match wins — same mental model as
    .gitignore (see CLAUDE.md's "Rule matching semantics"). A source with
    zero rules matches nothing: explicit opt-in, not "show everything by
    default"."""
    verdict = False
    for rule in sorted(rules, key=lambda r: r.order):
        if _rule_matches(path, rule):
            verdict = rule.type == RuleType.include
    return verdict
