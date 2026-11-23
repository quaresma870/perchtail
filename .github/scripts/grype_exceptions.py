"""Checks the Grype exceptions in .grype.yaml before the daily image scan.

Every exception needs a reason ending in "Review by YYYY-MM-DD." The check
fails once a date has passed, so an accepted advisory is looked at again
instead of staying ignored for good.

Usage: grype_exceptions.py [--config FILE] [--today YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / ".grype.yaml"
_VULN = re.compile(r"^\s*-\s*vulnerability:\s*(\S+)\s*$")
_REASON = re.compile(r'^\s*reason:\s*"(.*)"\s*$')
_REVIEW = re.compile(r"Review by (\d{4}-\d{2}-\d{2})\.")


def exceptions(text: str) -> list[dict]:
    """The ignore rules, as {id, reason}, read line by line (the file is
    written by hand in one fixed shape; no YAML library on the runner)."""
    found: list[dict] = []
    for line in text.splitlines():
        vuln = _VULN.match(line)
        if vuln:
            found.append({"id": vuln.group(1), "reason": ""})
            continue
        reason = _REASON.match(line)
        if reason and found:
            found[-1]["reason"] = reason.group(1)
    return found


def problems(entries: list[dict], today: dt.date) -> list[str]:
    out: list[str] = []
    for entry in entries:
        review = _REVIEW.search(entry["reason"])
        if not review:
            out.append(f"{entry['id']}: the reason has no \"Review by YYYY-MM-DD.\"")
            continue
        due = dt.date.fromisoformat(review.group(1))
        if due < today:
            out.append(
                f"{entry['id']}: the exception was due for review on {due.isoformat()}; "
                "update the image or review the exception in .grype.yaml"
            )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--today", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args(argv)
    entries = exceptions(args.config.read_text(encoding="utf-8"))
    found = problems(entries, args.today)
    if found:
        print("Grype exceptions need attention:")
        for problem in found:
            print(f"  - {problem}")
            print(f"::error title=Grype exceptions::{problem}")
        return 1
    print(f"{len(entries)} Grype exceptions, none past its review date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
