"""Turns one run's results into Markdown tables ready for docs/sizing.md.

    python loadtest/report/report.py loadtest/results/<date>-<profile>

Reads run.json (written by run.py), and in each phase folder the k6 summary
(k6-<phase>.json), the collector's server.csv and hardware.json. Writes
summary.md next to run.json and prints it:

- the run: profile, PerchTail version (read from the instance), date,
  hardware, k6 version, commit;
- latency per phase and scenario: requests, p50/p95/p99, error rate,
  thresholds;
- server resources per phase: CPU (peak and average), memory, scratch,
  SQLite database and WAL, connector slots, requests refused by limits;
- the ramp: each step's users, and the p95 and error rate of its steady
  part (after the step's warm-up), and the breaking point: the first
  complete step past either limit, or with fewer than the profile's
  ramp.min_requests steady requests and fewer than the step before (a
  server that collapses completes few requests, held until they time out);
- a short table per scenario (p95, errors, the phase's CPU and memory
  peaks), the form docs/sizing.md uses per profile.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "seed"))

from common import load_profile  # noqa: E402

MIB = 1024 * 1024
RAMP_UP_SECONDS = 10  # k6/lib/options.js
DEFAULT_MIN_REQUESTS = 100


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(value) -> str:
    return "–" if value is None else f"{value:,.0f}"


def _mib(value) -> str:
    return "–" if value is None else f"{value / MIB:,.1f}"


def _pct(value) -> str:
    return "–" if value is None else f"{value * 100:.2f} %"


def read_server(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def column(rows: list[dict], name: str) -> list[float]:
    return [v for v in (_num(r.get(name)) for r in rows) if v is not None]


def scenario_rows(summary: dict) -> list[dict]:
    out = []
    metrics = summary.get("metrics", {})
    for key, metric in metrics.items():
        match = re.fullmatch(r"http_req_duration\{scenario:([^,}]+)(,flow:(ui|search))?\}", key)
        if not match:
            continue
        failed = metrics.get(f"http_req_failed{{scenario:{match.group(1)}}}", {})
        values = metric.get("values", {})
        thresholds = {**metric.get("thresholds", {}), **failed.get("thresholds", {})}
        out.append(
            {
                "scenario": match.group(1),
                "requests": int(values.get("count", 0)),
                "p50": values.get("med"),
                "p95": values.get("p(95)"),
                "p99": values.get("p(99)"),
                "errors": failed.get("values", {}).get("rate"),
                "ok": all(t.get("ok", True) for t in thresholds.values()),
            }
        )
    return sorted(out, key=lambda r: r["scenario"])


def ramp_steps(p: dict) -> list[dict]:
    """The same steps as k6/lib/options.js rampSteps (seconds from the
    scenario's start)."""
    hold = float(p.get("step_seconds", 60))
    steps, start = [], 0.0
    for i in range(int(p.get("steps", 5))):
        end = start + RAMP_UP_SECONDS + hold
        steps.append(
            {
                "step": i + 1,
                "vus": int(p.get("start_vus", 1)) + i * int(p.get("step_vus", 1)),
                "end": end,
            }
        )
        start = end
    return steps


def ramp_rows(summary: dict, profile: dict) -> list[dict]:
    """Each step's steady part, judged against the limits. `complete` when
    the run lasted past the step's end, `enough` when it has at least
    min_requests steady requests. A complete step `breaks` when it is past
    a limit, whatever its count, or when it has too few requests and fewer
    than the step before: when a server collapses, requests hang until
    they time out and a step completes few of them, so a low count is a
    symptom, never a reason to skip the step. A complete step with too few
    requests that is within limits and not below the step before is
    `skipped`: too little to judge either way."""
    p = profile.get("scenarios", {}).get("ramp", {})
    limits = p.get("thresholds", {})
    minimum = int(p.get("min_requests", DEFAULT_MIN_REQUESTS))
    metrics = summary.get("metrics", {})
    run_seconds = (summary.get("state", {}).get("testRunDurationMs") or 0) / 1000
    rows: list[dict] = []
    previous: int | None = None
    for step in ramp_steps(p):
        n = step["step"]
        duration = metrics.get(
            f"http_req_duration{{scenario:ramp,step:{n},stage:steady,flow:ui}}", {}
        )
        failed = metrics.get(f"http_req_failed{{scenario:ramp,step:{n},stage:steady}}", {})
        values = duration.get("values", {})
        p95 = values.get("p(95)")
        errors = failed.get("values", {}).get("rate")
        complete = run_seconds >= step["end"]
        enough = int(values.get("count", 0)) >= minimum
        within = (p95 is None or p95 < float(limits.get("p95_ms", float("inf")))) and (
            errors is None or errors < float(limits.get("error_rate", 1))
        )
        requests = int(values.get("count", 0))
        dropped = not enough and previous is not None and requests < previous
        breaks = complete and (not within or dropped)
        rows.append(
            {
                **step,
                "requests": requests,
                "p95": p95,
                "errors": errors,
                "complete": complete,
                "enough": enough,
                "counted": complete and enough,
                "ok": within,
                "dropped": dropped,
                "breaks": breaks,
                "skipped": complete and not enough and not breaks,
                "previous": previous,
            }
        )
        if complete:
            previous = requests
    return rows


def ramp_verdict(row: dict) -> str:
    if not row["complete"]:
        return "not run to the end"
    if row["breaks"] and row["enough"]:
        return "**no**"
    if row["breaks"] and not row["ok"]:
        return "**no** (too few requests, and past a limit)"
    if row["breaks"]:
        return f"**no** (too few requests, fewer than the step before's {row['previous']:,})"
    if row["skipped"]:
        return "too few requests to judge"
    return "yes"


def ramp_markdown(phase_name: str, summary: dict, profile: dict) -> list[str]:
    p = profile["scenarios"]["ramp"]
    limit = p.get("thresholds", {})
    steps = ramp_rows(summary, profile)
    broken = next((s for s in steps if s["breaks"]), None)
    counted = [s for s in steps if s["counted"]]
    warmup = min(float(p.get("warmup_seconds", 30)), float(p.get("step_seconds", 60)) / 2)
    minimum = int(p.get("min_requests", DEFAULT_MIN_REQUESTS))
    out = [
        f"### Ramp (phase `{phase_name}`)",
        "",
        f"Limits: p95 < {limit.get('p95_ms')} ms and errors < "
        f"{limit.get('error_rate', 0) * 100:g} %, over each step's steady part (after "
        f"{RAMP_UP_SECONDS} s of ramp-up and {warmup:g} s of warm-up). A step that ran to the end "
        f"breaks when it is past a limit, or when it has fewer than {minimum} steady requests "
        "and fewer than the step before (a collapsing server completes few requests).",
        "",
        "| Step | Users | Steady requests | p95 (ms) | Errors | Within limits |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for row in steps:
        out.append(
            f"| {row['step']} | {row['vus']} | {row['requests']:,} | {_ms(row['p95'])} | "
            f"{_pct(row['errors'])} | {ramp_verdict(row)} |"
        )
    out.append("")
    if broken:
        out.append(f"**Breaking point: step {broken['step']}, {broken['vus']} concurrent users.**")
    elif counted:
        out.append(
            "Breaking point not reached: within limits up to "
            f"{counted[-1]['vus']} concurrent users."
        )
    else:
        out.append("No step ran to the end with enough steady requests to judge.")
    out.append("")
    return out


def hardware_line(hw: dict) -> str:
    parts = [f"{hw.get('cpu_model', '?')}, {hw.get('logical_cpus', '?')} logical CPUs"]
    if hw.get("ram_bytes"):
        parts.append(f"{hw['ram_bytes'] / 1024**3:.0f} GiB RAM")
    docker = hw.get("docker")
    if docker:
        parts.append(
            f"Docker ({docker.get('os')}): {docker.get('cpus')} CPUs, "
            f"{(docker.get('mem_bytes') or 0) / 1024**3:.1f} GiB"
        )
    disk = (hw.get("perchtail_disks") or {}).get("scratch") or {}
    if disk.get("total_bytes"):
        parts.append(f"scratch filesystem {disk['total_bytes'] / 1024**3:.0f} GiB")
    parts.append(hw.get("os", ""))
    return "; ".join(p for p in parts if p)


def build(run_dir: Path) -> str:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    profile_path = Path(run["profile_file"])
    profile = (
        load_profile(str(profile_path)) if profile_path.exists() else load_profile(run["profile"])
    )
    hw: dict = {}
    for phase in run["phases"]:
        path = run_dir / phase["name"] / "hardware.json"
        if path.exists():
            hw = json.loads(path.read_text(encoding="utf-8"))
            break

    lines = [
        f"## Profile `{run['profile']}` — PerchTail {hw.get('perchtail_version', '?')}",
        "",
        f"- Measured: {run['phases'][0]['started'] if run['phases'] else '?'} (UTC), "
        f"commit `{(run.get('git_commit') or '?')[:12]}`, "
        f"k6 {run['k6']['version']} ({run['k6']['mode']})",
        f"- Hardware: {hardware_line(hw)}",
        f"- Profile: {profile.get('users')} users; "
        f"sources {json.dumps(profile.get('sources'))}; "
        f"agents {json.dumps(profile.get('agents'))}; "
        f"largest file {profile.get('files', {}).get('large_mb', '?')} MB",
        f"- Duration: seeding {run.get('seed_seconds', 0):.0f} s, "
        f"whole run {run.get('total_seconds', 0):.0f} s",
        "",
        "### Latency per scenario",
        "",
        "| Phase | Scenario | Requests | p50 (ms) | p95 (ms) | p99 (ms) | Errors | Thresholds |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    resources = [
        "### Server resources per phase",
        "",
        "| Phase | CPU peak | CPU avg | Memory peak (MiB) | Scratch peak (MiB) | DB peak (MiB) "
        "| WAL peak (MiB) | Slots in use (max) | Refused by limits |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    ramp_section: list[str] = []
    sizing = [
        "### For docs/sizing.md",
        "",
        "| Phase | Scenario | p95 (ms) | Errors | CPU peak | Memory peak (MiB) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for phase in run["phases"]:
        name = phase["name"]
        summary_path = run_dir / name / f"k6-{name}.json"
        summary = (
            json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
        )
        for row in scenario_rows(summary):
            lines.append(
                f"| {name} | {row['scenario']} | {row['requests']:,} | {_ms(row['p50'])} | "
                f"{_ms(row['p95'])} | {_ms(row['p99'])} | {_pct(row['errors'])} | "
                f"{'pass' if row['ok'] else '**fail**'} |"
            )
        login = (
            summary.get("metrics", {}).get("http_req_duration{flow:login}", {}).get("values", {})
        )
        if login.get("count"):
            lines.append(
                f"| {name} | (sign-in, all scenarios) | {int(login['count']):,} | "
                f"{_ms(login.get('med'))} | "
                f"{_ms(login.get('p(95)'))} | {_ms(login.get('p(99)'))} | – | – |"
            )
        if "background" in phase["scenarios"]:
            lines.append(f"| {name} | background | – | – | – | – | – | no users |")
        server = read_server(run_dir / name / "server.csv")
        cpu = column(server, "cpu_percent")
        mem = column(server, "mem_bytes")
        scratch = column(server, "scratch_bytes") or column(server, "health_scratch_used_bytes")
        db = column(server, "db_bytes") or column(server, "health_db_size_bytes")
        wal = column(server, "wal_bytes") or column(server, "health_db_wal_bytes")
        slots = column(server, "slots_in_use")
        refused = column(server, "limit_rejections_total")
        resources.append(
            f"| {name} | {max(cpu) if cpu else 0:.0f} % | "
            f"{sum(cpu) / len(cpu) if cpu else 0:.0f} % | "
            f"{_mib(max(mem) if mem else None)} | {_mib(max(scratch) if scratch else None)} | "
            f"{_mib(max(db) if db else None)} | {_mib(max(wal) if wal else None)} | "
            f"{int(max(slots)) if slots else '–'} | "
            f"{int(refused[-1] - refused[0]) if refused else '–'} |"
        )
        cpu_peak = f"{max(cpu):.0f} %" if cpu else "–"
        for row in scenario_rows(summary):
            sizing.append(
                f"| {name} | {row['scenario']} | {_ms(row['p95'])} | {_pct(row['errors'])} | "
                f"{cpu_peak} | {_mib(max(mem) if mem else None)} |"
            )
        if "ramp" in phase["scenarios"]:
            ramp_section += ramp_markdown(name, summary, profile)
    return "\n".join(lines + [""] + resources + [""] + ramp_section + sizing) + "\n"


def main() -> None:
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    run_dir = Path(sys.argv[1])
    text = build(run_dir)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    (run_dir / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
