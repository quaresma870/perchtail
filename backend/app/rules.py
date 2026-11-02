import re
from functools import lru_cache

from app.logging_config import get_logger
from app.models import PatternKind, Protocol, Rule, RuleType, Source

logger = get_logger(__name__)

# Protocols whose remote filesystem treats paths case-insensitively and
# normalizes some spellings of the same name (Windows: SMB shares and WinRM).
_WINDOWS_PROTOCOLS = frozenset({Protocol.smb, Protocol.winrm})

# An 8.3 short-name alias, e.g. PROGRA~1 or SECRET~1.TXT -- Windows resolves
# these to the long name they abbreviate.
_SHORT_NAME_ALIAS = re.compile(r"^[^~]{1,8}~\d+(\.[^.]*)?$")


class InvalidPatternError(ValueError):
    pass


def parse_pattern(raw: str) -> tuple[str, PatternKind]:
    """Splits the admin-facing `re:` prefix convention (CLAUDE.md's rule
    matching semantics) from the pattern itself. Raises InvalidPatternError
    for a regex that doesn't compile, so a typo is rejected when it's saved
    instead of breaking every browse of the source afterward."""
    if raw.startswith("re:"):
        pattern = raw[len("re:") :]
        try:
            re.compile(pattern)
        except re.error as exc:
            raise InvalidPatternError(f"invalid regex {pattern!r}: {exc}") from exc
        return pattern, PatternKind.regex
    return raw, PatternKind.glob


@lru_cache(maxsize=2048)
def _compile_glob(pattern: str, ignore_case: bool = False) -> re.Pattern[str]:
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
    return re.compile(f"^{''.join(out)}$", re.IGNORECASE if ignore_case else 0)


@lru_cache(maxsize=2048)
def _compile_regex(pattern: str, ignore_case: bool = False) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        # Only reachable for rules saved before parse_pattern validated
        # regexes; logged once per pattern thanks to the cache.
        logger.error("rules.invalid_regex", pattern=pattern, error=str(exc))
        return None


def _rule_matches(path: str, rule: Rule, ignore_case: bool) -> bool:
    if rule.pattern_kind == PatternKind.regex:
        compiled = _compile_regex(rule.pattern, ignore_case)
        if compiled is None:
            # Fail closed: a broken include shows nothing, a broken exclude
            # hides everything it might have been written to cover.
            return rule.type == RuleType.exclude
        return compiled.search(path) is not None
    return _compile_glob(rule.pattern, ignore_case).match(path) is not None


def is_visible(path: str, rules: list[Rule], *, case_insensitive: bool = False) -> bool:
    """Rules are evaluated in `order`, last match wins — same mental model as
    .gitignore (see CLAUDE.md's "Rule matching semantics"). A source with
    zero rules matches nothing: explicit opt-in, not "show everything by
    default".

    `case_insensitive` must be set for sources whose filesystem is
    case-insensitive (see rules_case_insensitive): otherwise `SECRET/x`
    would slip past an exclude rule written as `secret/**` while still
    opening the very same file."""
    verdict = False
    for rule in sorted(rules, key=lambda r: r.order):
        if _rule_matches(path, rule, case_insensitive):
            verdict = rule.type == RuleType.include
    return verdict


def rules_case_insensitive(source: Source) -> bool:
    return source.protocol in _WINDOWS_PROTOCOLS


def is_safe_relative_path(path: str) -> bool:
    """Accepts only a canonical relative path: `/`-separated segments with
    no `..`, `.` or empty segments, no backslash, no `:`, and no control
    characters -- or "" for the source root. Checked before a client-supplied
    path ever reaches a rule check or a connector.

    Canonical, not just traversal-free, because rules are matched against
    the path *as written*: `./secret/x`, `secret//x` or (on Windows, where a
    backslash is a separator) `secret\\x` all reach the same file as
    `secret/x` but wouldn't match an exclude rule written as `secret/**`.
    Every path a listing produces is already canonical, so legitimate
    browsing never sends anything this rejects. Also rules out absolute and
    drive-letter paths (`/etc`, `C:\\...`) and NTFS alternate data streams
    (`file:stream`)."""
    if path == "":
        return True
    if "\\" in path or ":" in path:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in path):
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


def is_safe_windows_relative_path(path: str) -> bool:
    """Extra checks for SMB/WinRM sources, on top of is_safe_relative_path:
    Windows silently strips trailing dots and spaces from a name and
    resolves 8.3 short-name aliases (`SECRE~1`) to the long name, so either
    spelling would reach a file under a different-looking path than the one
    the rules saw."""
    for segment in path.split("/") if path else []:
        if segment.endswith((".", " ")) or _SHORT_NAME_ALIAS.match(segment):
            return False
    return True
