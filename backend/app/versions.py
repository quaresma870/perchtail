"""PerchTail version strings: X.Y.Z for a final release, X.Y.Z-rc.N for a
release candidate, or X.Y.Z-beta.N for an early pre-release taken from dev
(a leading "v", as in tags, is accepted). They sort as
X.Y.Z-beta.N < X.Y.Z-rc.N < X.Y.Z, numerically within each stage. Compared
here rather than with a general version library: the release workflow only
ever produces these three forms, and anything else is treated as unknown
rather than guessed at."""

import re

_VERSION = re.compile(r"^v?(\d{1,4})\.(\d{1,4})\.(\d{1,4})(?:-(beta|rc)\.(\d{1,4}))?$")
# The stage of a version: a beta sorts before a release candidate, which
# sorts before the final release.
_BETA, _RC, _FINAL = 0, 1, 2
_STAGES = {"beta": _BETA, "rc": _RC}


def parse_version(value: str | None) -> tuple[int, int, int, int, int] | None:
    """(major, minor, patch, stage, n) for a PerchTail version, with stage 0
    for a beta, 1 for a release candidate and 2 for a final release (n is 0
    then), or None if `value` isn't one (a dev build, an agent built without
    a version, garbage)."""
    match = _VERSION.fullmatch(value or "")
    if match is None:
        return None
    major, minor, patch, stage, n = match.groups()
    if stage is None:
        return int(major), int(minor), int(patch), _FINAL, 0
    return int(major), int(minor), int(patch), _STAGES[stage], int(n)


def is_final(value: str | None) -> bool:
    """Whether `value` is a final release (X.Y.Z, with or without a leading
    "v"), not a pre-release or something unknown."""
    parsed = parse_version(value)
    return parsed is not None and parsed[3] == _FINAL


def is_newer(candidate: str | None, current: str | None) -> bool:
    """Whether `candidate` is a later release than `current`. False when
    either isn't a PerchTail version: nothing is offered or warned about on
    a guess."""
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and b is not None and a > b
