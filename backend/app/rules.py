import re
from functools import lru_cache

import re2

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


def _re2_options(ignore_case: bool = False) -> "re2.Options":
    options = re2.Options()
    options.log_errors = False
    options.case_sensitive = not ignore_case
    return options


def compile_linear(pattern: str, ignore_case: bool = False):
    """A regex compiled with RE2, which runs in linear time: for patterns
    that come from users and run on the backend (rules, file-name search).
    Raises re2.error for one RE2 can't compile (backreferences,
    lookaround)."""
    return re2.compile(pattern, _re2_options(ignore_case))


def parse_pattern(raw: str, *, linear_time: bool = True) -> tuple[str, PatternKind]:
    """Splits the admin-facing `re:` prefix convention (CLAUDE.md's rule
    matching semantics) from the pattern itself. Raises InvalidPatternError
    for a regex that doesn't compile, so a typo is rejected when it's saved
    instead of breaking every browse of the source afterward.

    Rule regexes run on the backend for every path of every browse, so they
    are compiled with RE2, which always matches in linear time: a
    catastrophic pattern like `(a+)+$` can't stall the server (ReDoS). RE2
    has no backreferences or lookaround, so those are rejected here.
    linear_time=False keeps Python's full syntax for patterns that never run
    on the backend (severity patterns, evaluated in the browser)."""
    if raw.startswith("re:"):
        pattern = raw[len("re:") :]
        try:
            if linear_time:
                re2.compile(pattern, _re2_options())
            else:
                re.compile(pattern)
        except re2.error as exc:
            raise InvalidPatternError(
                f"invalid regex {pattern!r} (rules use RE2: no backreferences or "
                f"lookaround): {exc}"
            ) from exc
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
def _compile_regex(pattern: str, ignore_case: bool = False):
    try:
        return re2.compile(pattern, _re2_options(ignore_case))
    except re2.error as exc:
        # Only reachable for rules saved before parse_pattern validated
        # regexes with RE2 (e.g. one using lookaround); logged once per
        # pattern thanks to the cache.
        logger.error("rules.invalid_regex", pattern=pattern, error=str(exc))
        return None


def glob_matches(pattern: str, path: str, *, ignore_case: bool = False) -> bool:
    """Whether `path` matches an rsync/gitignore-style glob, with the same
    semantics as rules (`**/` any number of folders, `*` within one)."""
    return _compile_glob(pattern, ignore_case).match(path) is not None


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


def _segment_matches(pattern_segment: str, name: str, ignore_case: bool) -> bool:
    """One glob path segment (`*`, `?` and literals, never crossing `/`)
    against one path segment -- the per-segment half of _compile_glob."""
    return _compile_glob(pattern_segment, ignore_case).match(name) is not None


def _glob_could_match_below(pattern: str, dir_segments: list[str], ignore_case: bool) -> bool:
    """Whether glob `pattern` could match some path strictly below the
    directory `dir_segments` (i.e. `dir/<at least one more segment>`)."""
    parts = pattern.split("/")
    if any("**" in p and p != "**" for p in parts):
        return True  # `a**b` spans segments in ways this walk doesn't model

    def walk(pi: int, si: int) -> bool:
        if si == len(dir_segments):
            return pi < len(parts)  # something left to match a child
        if pi == len(parts):
            return False
        if parts[pi] == "**":
            return walk(pi + 1, si) or walk(pi, si + 1)
        return _segment_matches(parts[pi], dir_segments[si], ignore_case) and walk(pi + 1, si + 1)

    return walk(0, 0)


def _glob_matches_exactly(parts: list[str], segments: list[str], ignore_case: bool) -> bool:
    def walk(pi: int, si: int) -> bool:
        if pi == len(parts):
            return si == len(segments)
        if parts[pi] == "**":
            return walk(pi + 1, si) or (si < len(segments) and walk(pi, si + 1))
        return (
            si < len(segments)
            and _segment_matches(parts[pi], segments[si], ignore_case)
            and walk(pi + 1, si + 1)
        )

    return walk(0, 0)


def _glob_covers_everything_below(pattern: str, dir_segments: list[str], ignore_case: bool) -> bool:
    """Whether glob `pattern` matches *every* path below the directory --
    true for `X/**` where X matches the directory or one of its ancestors
    (and for a bare `**`)."""
    parts = pattern.split("/")
    if parts[-1] != "**" or any("**" in p and p != "**" for p in parts):
        return False
    prefix = parts[:-1]
    return any(
        _glob_matches_exactly(prefix, dir_segments[:k], ignore_case)
        for k in range(len(dir_segments) + 1)
    )


def is_dir_visible(dir_path: str, rules: list[Rule], *, case_insensitive: bool = False) -> bool:
    """Whether a directory should be shown (and be browsable): only if some
    file below it could end up visible under the rule chain. Derived from
    the same ordered, last-match-wins rules files use, without listing the
    remote tree:

    - an include rule that can't match anything below the directory doesn't
      keep it visible (`app/**` hides every other top-level folder);
    - an include that *can* is cancelled by a later exclude that covers the
      whole directory (`secret/**`, an ancestor's `X/**`, or `**`), so an
      excluded folder disappears -- name, sub-folders and all;
    - a later include can still re-open part of an excluded folder, and then
      only the folders leading to it are shown.

    Regex rules can't be analysed this way: a regex include is assumed to
    possibly match anywhere (keeps folders visible) and a regex exclude is
    never assumed to cover a whole folder. To hide a folder reliably, use a
    glob exclude such as `folder/**`. The source root ("") is always
    visible; with zero rules its listing is simply empty."""
    if dir_path == "":
        return True
    segments = dir_path.split("/")
    ordered = sorted(rules, key=lambda r: r.order)
    for i, rule in enumerate(ordered):
        if rule.type != RuleType.include:
            continue
        if rule.pattern_kind == PatternKind.glob and not _glob_could_match_below(
            rule.pattern, segments, case_insensitive
        ):
            continue
        covered = any(
            later.type == RuleType.exclude
            and later.pattern_kind == PatternKind.glob
            and _glob_covers_everything_below(later.pattern, segments, case_insensitive)
            for later in ordered[i + 1 :]
        )
        if not covered:
            return True
    return False


def is_entry_visible(
    path: str, is_dir: bool, rules: list[Rule], *, case_insensitive: bool = False
) -> bool:
    """Listing filter shared by every connector: files go through the rule
    chain, directories through is_dir_visible."""
    if is_dir:
        return is_dir_visible(path, rules, case_insensitive=case_insensitive)
    return is_visible(path, rules, case_insensitive=case_insensitive)


def rules_case_insensitive(source: Source) -> bool:
    """Windows filesystems ignore case, so rules do too: SMB and WinRM
    sources, and agents that reported running on Windows (#189)."""
    if source.protocol in _WINDOWS_PROTOCOLS:
        return True
    return source.protocol == Protocol.agent and source.agent_os == "windows"


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
