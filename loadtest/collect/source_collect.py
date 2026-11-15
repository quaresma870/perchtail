"""A light collector for a Linux test server: CPU, memory and disk I/O of
the processes that serve PerchTail (sshd, smbd, the push agent), every
second, so `report.py costs --source-csv` can charge each measured
operation with what it cost on the source. Standard library only, reads
/proc; run it on the server for the length of a run:

    python3 source_collect.py --out sources-ssh-a.csv [--names sshd,smbd,perchtail-agent]

Each row: the time (seconds since the epoch, the same clock as the load
machine's, so keep both in sync with NTP), the process name, how many
processes have it, their CPU time so far (ms, cumulative), resident memory
(bytes) and bytes read and written so far (cumulative; /proc/<pid>/io needs
the same user or root). Stops on Ctrl+C, or after --seconds.
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from pathlib import Path

FIELDS = ["t", "name", "processes", "cpu_ms", "rss_bytes", "read_bytes", "write_bytes"]


def sample(names: list[str]) -> dict[str, dict]:
    """The processes whose name (comm) is one of `names`, summed per name.
    CPU of processes that ended since the last sample is lost, so short-
    lived ones (an sshd per session) are counted while they run."""
    ticks = os.sysconf("SC_CLK_TCK")
    page = os.sysconf("SC_PAGE_SIZE")
    out = {
        n: {"processes": 0, "cpu_ms": 0.0, "rss_bytes": 0, "read_bytes": 0, "write_bytes": 0}
        for n in names
    }
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
            comm = stat[stat.index("(") + 1 : stat.rindex(")")]
            if comm not in out:
                continue
            fields = stat.rsplit(")", 1)[1].split()
            totals = out[comm]
            totals["processes"] += 1
            totals["cpu_ms"] += (int(fields[11]) + int(fields[12])) * 1000 / ticks
            totals["rss_bytes"] += int(fields[21]) * page
            try:
                io = dict(line.split(": ") for line in (entry / "io").read_text().splitlines())
                totals["read_bytes"] += int(io.get("read_bytes", 0))
                totals["write_bytes"] += int(io.get("write_bytes", 0))
            except (OSError, ValueError):
                pass
        except (OSError, ValueError, IndexError):
            continue
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True)
    parser.add_argument("--names", default="sshd,sshd-session,smbd,perchtail-agent")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--seconds", type=float, default=0)
    args = parser.parse_args()
    names = [n.strip()[:15] for n in args.names.split(",") if n.strip()]  # comm is 15 chars
    path = Path(args.out)
    new = not path.exists()
    deadline = time.time() + args.seconds if args.seconds else None
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        try:
            while deadline is None or time.time() < deadline:
                started = time.time()
                for name, totals in sample(names).items():
                    writer.writerow({"t": round(started, 3), "name": name, **totals})
                handle.flush()
                time.sleep(max(0.0, args.interval - (time.time() - started)))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
