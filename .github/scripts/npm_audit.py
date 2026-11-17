"""Runs npm audit and fails on any advisory at or above a level, except the
ones listed, with a reason and an expiry date, in .github/npm-audit-exceptions.json.

An exception is only for an advisory with no fixed version that doesn't reach
what PerchTail ships. It fails the run once it expires, and once the advisory
it names is gone, so it is removed instead of lingering.

Usage: npm_audit.py --level high [--exceptions FILE] [--report FILE] [--other-branch] [DIR]
With --report, the npm audit JSON is read from FILE instead of running npm.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

LEVELS = ["info", "low", "moderate", "high", "critical"]
DEFAULT_EXCEPTIONS = Path(__file__).resolve().parent.parent / "npm-audit-exceptions.json"


def advisories(report: dict, level: str) -> dict[str, dict]:
    """The advisories at or above level, by GHSA id. Packages that are only
    vulnerable through another package carry no advisory of their own."""
    floor = LEVELS.index(level)
    found: dict[str, dict] = {}
    for vulnerability in report.get("vulnerabilities", {}).values():
        for via in vulnerability.get("via", []):
            if not isinstance(via, dict):
                continue
            if LEVELS.index(via.get("severity", "info")) < floor:
                continue
            url = via.get("url", "")
            ghsa = url.rsplit("/", 1)[-1] if url else str(via.get("source"))
            found.setdefault(ghsa, {"package": via.get("name", "?"), "title": via.get("title", "")})
    return found


def check(
    found: dict[str, dict], exceptions: list[dict], today: dt.date, unused_fails: bool = True
) -> tuple[list, list]:
    """Returns (problems, exceptions in use). With unused_fails off (another
    branch's lockfile checked against these exceptions), an exception that
    matches nothing there is no problem."""
    problems: list[str] = []
    used: list[dict] = []
    by_id = {e["id"]: e for e in exceptions}
    for ghsa, info in sorted(found.items()):
        exception = by_id.get(ghsa)
        if exception is None:
            problems.append(f"{ghsa} ({info['package']}): {info['title']}")
            continue
        expires = dt.date.fromisoformat(exception["expires"])
        if expires < today:
            problems.append(
                f"{ghsa} ({info['package']}): the exception expired on {exception['expires']}; "
                "update the dependency or review the exception"
            )
            continue
        used.append(exception)
    for ghsa, exception in sorted(by_id.items()):
        if unused_fails and ghsa not in found:
            problems.append(
                f"{ghsa} ({exception['package']}): the exception matches no advisory any more; "
                "remove it from .github/npm-audit-exceptions.json"
            )
    return problems, used


def run_audit(directory: Path) -> dict:
    result = subprocess.run(
        ["npm", "audit", "--json"],  # noqa: S607 - npm from the runner's PATH
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        sys.stderr.write(result.stderr)
        raise SystemExit("npm audit gave no JSON report") from None


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", default=".")
    parser.add_argument("--level", choices=LEVELS, required=True)
    parser.add_argument("--exceptions", type=Path, default=DEFAULT_EXCEPTIONS)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--other-branch",
        action="store_true",
        help="the lockfile is another branch's: unused exceptions are not a failure",
    )
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)

    exceptions = json.loads(args.exceptions.read_text(encoding="utf-8"))
    if args.report:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    else:
        report = run_audit(Path(args.directory))
    problems, used = check(
        advisories(report, args.level), exceptions, args.today, not args.other_branch
    )

    for exception in used:
        line = (
            f"Accepted until {exception['expires']}: {exception['id']} "
            f"({exception['package']}): {exception['reason']}"
        )
        print(line)
        summary([f"- npm audit exception: {line}"])
    if problems:
        print(f"npm audit ({args.level} and above) failed:")
        for problem in problems:
            print(f"  - {problem}")
            print(f"::error title=npm audit::{problem}")
        return 1
    print(f"npm audit ({args.level} and above): no advisory outside the listed exceptions")
    return 0


if __name__ == "__main__":
    sys.exit(main())
