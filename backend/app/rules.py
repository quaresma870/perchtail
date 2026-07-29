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


def is_safe_relative_path(path: str) -> bool:
    """Rejects path traversal (`..` segments) and absolute paths before a
    client-supplied path ever reaches a rule check or a connector — CLAUDE.md
    flags "path traversal or injection via ... the archive browse/download
    endpoints" as explicit security scope. The rule engine denying a
    traversal attempt (most patterns wouldn't match `../../etc/passwd`) is
    not a substitute for this: a permissive rule could coincidentally match
    it, so this must be checked independently of rule outcome.

    Splits on both `/` and `\\`, not just `/`: collectors/smb.py and
    collectors/winrm.py join a relative path onto a Windows `base_path`
    with backslashes, so a `..\\` segment has to be caught here just as
    reliably as a POSIX `../` one — splitting on `/` alone would let
    `..\\..\\windows\\system32` straight through unblocked, since it
    contains no forward slash at all. Also rejects a bare `:` to rule out
    Windows drive-letter absolute paths (`C:\\...`)."""
    if path.startswith("/") or path.startswith("\\") or ":" in path:
        return False
    return ".." not in re.split(r"[\\/]", path)
