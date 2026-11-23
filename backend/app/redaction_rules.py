"""Redaction rules (#382, #249): the configuration the redaction engine
(app/redaction) runs with, stored in redaction_rule and
source_redaction_override (app/models.py), and configured by super admins
only (app/api/redaction_rules.py).

- System rules (`source_id` null) apply to every source: the eleven
  built-in detectors, seeded once and disabled, and custom RE2 patterns.
- A source adds its own custom rules and can turn a system rule off.
- A source's effective rules are the enabled system rules it doesn't turn
  off, then its own enabled rules, each list by `order` then id. When two
  rules match overlapping text, the first in that list gives the label.
- Redaction applies to a source when the `redaction_enabled` setting is on
  and the source has at least one effective rule; then it applies to every
  user, super admins included. On with no enabled rule, nothing changes.

Custom patterns are checked when they're saved (check_custom_rule), so a
saved rule always runs in the engine, here and in the agent's Go engine."""

import re
import re._parser as sre_parse
from collections.abc import Sequence

import re2
from sqlalchemy import delete as sa_delete
from sqlmodel import Session, select

from app import system_settings
from app.models import RedactionRule, Source, SourceRedactionOverride
from app.redaction import DETECTOR_NAMES, MAX_RULE_LENGTH, RedactionRuleError, compile_rules

# Limits on custom rules: a pattern RE2 runs in linear
# time, but its cost still grows with its size and with the number of rules.
MAX_RULES_PER_SCOPE = 50
MAX_PATTERN_CHARS = 1000
MAX_PROGRAM_SIZE = 2000
MAX_PATTERN_MEM_BYTES = 8 * 1024 * 1024
# Names go into every replacement (`[redacted: <name>]`).
MAX_NAME_CHARS = 48
# The longest match a custom rule may declare is MAX_RULE_LENGTH bytes (from
# the engine, which refuses more): the engine keeps that much before the cut
# of a long line, so a value cut by the limit leaves nothing behind.

_NAME_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f\[\]]")


class RedactionRuleInvalid(ValueError):
    """A rule that can't be saved; the message says why, for the admin."""


def check_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise RedactionRuleInvalid("The rule needs a name")
    if len(name) > MAX_NAME_CHARS:
        raise RedactionRuleInvalid(f"The name is longer than {MAX_NAME_CHARS} characters")
    if _NAME_FORBIDDEN.search(name):
        raise RedactionRuleInvalid("The name can't contain control characters, [ or ]")
    return name


def compile_checked(pattern: str):
    """Compiles a custom pattern the way the engine runs it (app.redaction
    compile_pattern: Latin-1 when it's pure ASCII, UTF-8 otherwise, never
    across a newline), with a bounded memory budget. Returns the regex and
    the empty text of the type it matches (bytes or str)."""
    options = re2.Options()
    options.log_errors = False
    options.never_nl = True
    options.max_mem = MAX_PATTERN_MEM_BYTES
    text = "(?m)" + pattern
    if text.isascii():
        options.encoding = re2.Options.Encoding.LATIN1
        return re2.compile(text.encode("ascii"), options), b""
    return re2.compile(text, options), ""


def pattern_max_bytes(pattern: str) -> int | None:
    """The longest match `pattern` can make, in bytes, when it can be worked
    out: Python's regular-expression parser reads most RE2 syntax, and a
    pattern it can't read, or one with an unbounded repeat, gives None. A
    pure-ASCII pattern runs in Latin-1 (a character is a byte); any other
    in UTF-8, where a character takes up to 4 bytes."""
    try:
        _low, high = sre_parse.parse(pattern).getwidth()
    except Exception:  # noqa: BLE001 - RE2 syntax Python doesn't read: unknown
        return None
    if high >= sre_parse.MAXREPEAT:
        return None
    return high if pattern.isascii() else high * 4


def _has_byte_escape(pattern: str) -> bool:
    """True when the pattern uses `\\C` (any byte), which is RE2 C++ only:
    the agent's Go engine refuses it. Walks the escapes one by one (an odd
    number of backslashes before the C makes it the escape), so a long run
    of backslashes in a user's pattern can't make the check slow."""
    i = 0
    while i < len(pattern):
        if pattern[i] == "\\":
            if i + 1 < len(pattern) and pattern[i + 1] == "C":
                return True
            i += 2
            continue
        i += 1
    return False


def check_custom_rule(pattern: str, secret_group: int | None, max_length: int) -> None:
    """Refuses a custom pattern the engine can't run, or that would cost too
    much: it must compile with RE2 within the limits above, not use what
    the agent's Go engine refuses (`\\C`), not match the empty string (it
    would match everywhere), have the secret_group it names, and not match
    more than max_length bytes when that can be worked out."""
    if not pattern:
        raise RedactionRuleInvalid("The rule needs a pattern")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise RedactionRuleInvalid(f"The pattern is longer than {MAX_PATTERN_CHARS} characters")
    if _has_byte_escape(pattern):
        raise RedactionRuleInvalid("\\C (any byte) isn't supported")
    try:
        regex, empty = compile_checked(pattern)
    except re2.error as exc:
        raise RedactionRuleInvalid(
            f"The pattern isn't valid RE2 (no backreferences or lookaround): {exc}"
        ) from exc
    if regex.programsize > MAX_PROGRAM_SIZE:
        raise RedactionRuleInvalid(
            "The pattern is too complex: split it into simpler rules or bound its repeats"
        )
    if regex.search(empty) is not None:
        raise RedactionRuleInvalid("The pattern matches empty text, so it would match everywhere")
    group = secret_group or 0
    if group < 0 or group > regex.groups:
        raise RedactionRuleInvalid(f"The pattern has no group {group}")
    if not 1 <= max_length <= MAX_RULE_LENGTH:
        raise RedactionRuleInvalid(f"max_length must be from 1 to {MAX_RULE_LENGTH} bytes")
    longest = pattern_max_bytes(pattern)
    if longest is not None and longest > max_length:
        raise RedactionRuleInvalid(
            f"The pattern can match up to {longest} bytes: max_length must be at least that"
        )
    try:
        compile_rules(
            [
                {
                    "name": "check",
                    "pattern": pattern,
                    "secret_group": group,
                    "max_length": max_length,
                }
            ]
        )
    except RedactionRuleError as exc:
        raise RedactionRuleInvalid(str(exc)) from exc


def _ordered(rules) -> list[RedactionRule]:
    return sorted(rules, key=lambda rule: (rule.order, rule.id or 0))


def system_rules(session: Session) -> list[RedactionRule]:
    return _ordered(
        session.exec(select(RedactionRule).where(RedactionRule.source_id.is_(None))).all()
    )


def source_rules(session: Session, source_id: int) -> list[RedactionRule]:
    return _ordered(
        session.exec(select(RedactionRule).where(RedactionRule.source_id == source_id)).all()
    )


def disabled_rule_ids(session: Session, source_id: int) -> set[int]:
    return set(
        session.exec(
            select(SourceRedactionOverride.rule_id).where(
                SourceRedactionOverride.source_id == source_id,
                SourceRedactionOverride.disabled.is_(True),
            )
        ).all()
    )


def effective_redaction_rules(session: Session, source_id: int) -> list[RedactionRule]:
    """The rules the engine runs on a source's content: the enabled system
    rules the source doesn't turn off, then its own enabled rules."""
    disabled = disabled_rule_ids(session, source_id)
    return [rule for rule in system_rules(session) if rule.enabled and rule.id not in disabled] + [
        rule for rule in source_rules(session, source_id) if rule.enabled
    ]


def engine_rules(rules: Sequence[RedactionRule]) -> list[dict]:
    """The rules in the engine's (and the agent's `redact` block) format."""
    out: list[dict] = []
    for rule in rules:
        if rule.detector is not None:
            out.append({"id": rule.id, "name": rule.name, "detector": rule.detector})
        else:
            out.append(
                {
                    "id": rule.id,
                    "name": rule.name,
                    "pattern": rule.pattern,
                    "secret_group": rule.secret_group or 0,
                    "max_length": rule.max_length,
                }
            )
    return out


def redaction_applies(session: Session, source: Source) -> bool:
    """Whether a source's content is redacted: redaction is on and the
    source has an effective rule. The same for every user."""
    if not system_settings.get_bool(session, "redaction_enabled"):
        return False
    return bool(effective_redaction_rules(session, source.id))


def seed_builtin_redaction_rules(session: Session) -> None:
    """Adds each built-in detector missing from the system rules, disabled,
    in detectors.json order. Idempotent: run at every start, it only adds
    detectors a new version brings."""
    present = set(
        session.exec(
            select(RedactionRule.detector).where(
                RedactionRule.source_id.is_(None), RedactionRule.detector.is_not(None)
            )
        ).all()
    )
    for order, detector in enumerate(DETECTOR_NAMES):
        if detector in present:
            continue
        session.add(RedactionRule(source_id=None, name=detector, detector=detector, order=order))
    session.commit()


def delete_source_redaction(session: Session, source_id: int) -> None:
    """Everything redaction keeps for a source, when the source is deleted."""
    session.execute(
        sa_delete(SourceRedactionOverride).where(SourceRedactionOverride.source_id == source_id)
    )
    session.execute(sa_delete(RedactionRule).where(RedactionRule.source_id == source_id))
