"""PerchTail version strings: X.Y.Z, or X.Y.Z-rc.N for a release candidate
(a leading "v", as in tags, is accepted). Compared here rather than with a
general version library: the release workflow only ever produces these two
forms, and anything else is treated as unknown rather than guessed at."""

import re

_VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-rc\.(\d{1,4}))?$")
# A final release sorts after all of its release candidates.
_FINAL = 10**9


def parse_version(value: str | None) -> tuple[int, int, int, int] | None:
    """(major, minor, patch, rc) for a PerchTail version, or None if `value`
    isn't one (a dev build, an agent built without a version, garbage)."""
    match = _VERSION.fullmatch(value or "")
    if match is None:
        return None
    major, minor, patch, rc = match.groups()
    return int(major), int(minor), int(patch), int(rc) if rc is not None else _FINAL


def is_newer(candidate: str | None, current: str | None) -> bool:
    """Whether `candidate` is a later release than `current`. False when
    either isn't a PerchTail version: nothing is offered or warned about on
    a guess."""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b
