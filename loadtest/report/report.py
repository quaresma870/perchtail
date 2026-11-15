"""Turns one run's results into Markdown tables ready for docs/sizing.md.

    python loadtest/report/report.py loadtest/results/<date>-<profile>
    python loadtest/report/report.py costs loadtest/results/<date>-protocol_matrix \
        [--production loadtest/results/<date>-production] [--source-csv FILE ...]

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
- the growing-file scenarios: per protocol and operation, requests,
  p50/p95, HTTP errors, integrity errors and bytes transferred, with the
  first integrity errors logged; Follow's delay per write rate
  (growing_heavy, and from which rate it stops keeping up) and per hour of
  the day (daily_log), with the server's resources at each;
- the production profile: latency and errors per kind of person and
  operation, sessions, the incident, the server's resources over time
  (also written as timeseries.csv), memory's trend per hour, and a
  verdict: within the limits, or where not;
- protocol_matrix: every measured cell (see matrix/matrix.py);
- a short table per scenario (p95, errors, the phase's CPU and memory
  peaks), the form docs/sizing.md uses per profile.

`costs` writes the cost units of a protocol_matrix run (report/costs.py).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "seed"))

import growing  # noqa: E402
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


# --- growing files ------------------------------------------------------------

GROWING_SUITES = ("growing_files", "growing_heavy", "daily_log")
GROWING_OPS = ("open", "download", "download-zip", "follow")
INTEGRITY_LINE = re.compile(r'msg="INTEGRITY ((?:[^"\\]|\\.)*)"')


def _metric(summary: dict, key: str) -> dict:
    return summary.get("metrics", {}).get(key, {}).get("values", {})


def growing_rows(summary: dict, suite: str) -> list[dict]:
    """One row per protocol and operation of a growing-file scenario."""
    rows = []
    for key in summary.get("metrics", {}):
        match = re.fullmatch(
            rf"http_req_duration\{{suite:{suite},gop:([^,}}]+),protocol:([^,}}]+)\}}", key
        )
        if not match:
            continue
        op, protocol = match.groups()
        tags = f"suite:{suite},gop:{op},protocol:{protocol}"
        values = _metric(summary, key)
        if not values.get("count"):
            continue
        rows.append(
            {
                "protocol": protocol,
                "op": op,
                "requests": int(values.get("count", 0)),
                "p50": values.get("med"),
                "p95": values.get("p(95)"),
                "errors": _metric(summary, f"http_req_failed{{{tags}}}").get("rate"),
                "integrity": int(_metric(summary, f"integrity_errors{{{tags}}}").get("count", 0)),
                "bytes": _metric(summary, f"growing_bytes{{{tags}}}").get("count", 0),
            }
        )
    order = {op: i for i, op in enumerate(GROWING_OPS)}
    return sorted(rows, key=lambda r: (r["protocol"], order.get(r["op"], 9)))


def integrity_failures(k6_log: Path) -> list[dict]:
    """The INTEGRITY lines k6/lib/growing.js logged (the first of each VU)."""
    if not k6_log.exists():
        return []
    out = []
    for raw in k6_log.read_text(encoding="utf-8", errors="replace").splitlines():
        match = INTEGRITY_LINE.search(raw)
        if not match:
            continue
        try:
            out.append(json.loads(json.loads(f'"{match.group(1)}"')))
        except json.JSONDecodeError:
            continue
    return out


def _thresholds_ok(summary: dict, suite: str) -> bool:
    for key, metric in summary.get("metrics", {}).items():
        if f"suite:{suite}" in key and "protocol:" not in key:
            if not all(t.get("ok", True) for t in metric.get("thresholds", {}).values()):
                return False
    return True


def _epoch(stamp: str) -> float | None:
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
    except (TypeError, ValueError):
        return None


def writer_events(phase_dir: Path) -> list[dict]:
    path = phase_dir / "writer.jsonl"
    if not path.exists():
        return []
    return [json.loads(raw) for raw in path.read_text(encoding="utf-8").splitlines() if raw]


def window(server: list[dict], start: float, end: float) -> dict:
    """CPU, memory and scratch of the collector's samples in [start, end)."""
    rows = [r for r in server if start <= (_epoch(r.get("timestamp")) or -1) < end]
    cpu = column(rows, "cpu_percent")
    mem = column(rows, "mem_bytes")
    scratch = column(rows, "scratch_bytes") or column(rows, "health_scratch_used_bytes")
    return {
        "cpu_avg": sum(cpu) / len(cpu) if cpu else None,
        "cpu_max": max(cpu) if cpu else None,
        "mem_max": max(mem) if mem else None,
        "scratch_max": max(scratch) if scratch else None,
    }


def _size(value: float) -> str:
    return f"{value / 1024**3:g} GiB" if value >= 1024**3 else f"{value / MIB:,.0f} MiB"


def _cpu(value) -> str:
    return "–" if value is None else f"{value:.0f} %"


def _s(value) -> str:
    return "–" if value is None else f"{value:,.1f}"


def follow_refusal_lines(summary: dict, suite: str) -> list[str]:
    """What Follow did with files too big to open whole: a paragraph, or
    nothing when it never met one."""
    end_only = int(_metric(summary, f"follow_end_only{{suite:{suite}}}").get("count", 0))
    refused = int(_metric(summary, f"refused_too_large{{suite:{suite}}}").get("count", 0))
    if not end_only and not refused:
        return []
    return [
        f"Follow from the end of the file (listed above 200 MB, as the viewer's \"Tail the "
        f"end\"): {end_only:,} tabs. Follow refused as over MAX_FETCH_MB (`refused_too_large`: a "
        "file below 200 MB that the viewer opens whole and the server refuses cleanly with 413; "
        f"the viewer can't follow it, and it isn't counted as an error): {refused:,}.",
        "",
    ]


def growing_markdown(name: str, suite: str, summary: dict, phase_dir: Path) -> list[str]:
    total = int(_metric(summary, f"integrity_errors{{suite:{suite}}}").get("count", 0))
    errors = _metric(summary, f"http_req_failed{{suite:{suite}}}").get("rate")
    resets = int(_metric(summary, f"follow_resets{{suite:{suite}}}").get("count", 0))
    out = [
        f"### Growing files: `{suite}` (phase `{name}`)",
        "",
        f"Integrity errors: **{total}**; HTTP errors: {_pct(errors)}; Follow resets (rotations "
        f"seen): {resets}; limits {'met' if _thresholds_ok(summary, suite) else '**not met**'}.",
        "",
        *follow_refusal_lines(summary, suite),
        "| Protocol | Operation | Requests | p50 (ms) | p95 (ms) | HTTP errors | Integrity errors "
        "| MB transferred |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in growing_rows(summary, suite):
        out.append(
            f"| {row['protocol']} | {row['op']} | {row['requests']:,} | {_ms(row['p50'])} | "
            f"{_ms(row['p95'])} | {_pct(row['errors'])} | {row['integrity']} | "
            f"{_mib(row['bytes'])} |"
        )
    failures = [f for f in integrity_failures(phase_dir / "k6.log") if f.get("suite") == suite]
    if failures:
        out += ["", "First integrity errors (at most 20 per virtual user are logged):", ""]
        for f in failures[:10]:
            out.append(
                f"- {f.get('protocol')} {f.get('op')} `{f.get('path')}` ({f.get('source')}): "
                f"{f.get('error')}"
            )
    out.append("")
    return out


def falls_behind(early: float | None, late: float | None, tick: float) -> bool | None:
    """Whether Follow's delay grew within a step: its median in the second
    half is over 1.5 times the first half's and more than one Follow tick
    longer. None when a half has no samples."""
    if early is None or late is None:
        return None
    return late > early * 1.5 and late - early > tick


def heavy_markdown(
    name: str, summary: dict, phase_dir: Path, profile: dict, tick: float
) -> list[str]:
    steps = growing.heavy_steps(profile)
    events = writer_events(phase_dir)
    start = next((e["t"] for e in events if e.get("event") == "start"), None)
    server = read_server(phase_dir / "server.csv")
    out = [
        f"### Follow at rising write rates (phase `{name}`)",
        "",
        f"{int(growing.setting(profile, 'heavy_count'))} files, each written at the rate of the "
        "step and followed at the viewer's pace. Delay: how far behind the writer Follow is after "
        "each tick, in seconds (from the time stamp of the last line received) and in MB (from the "
        "size the server reports), median of each half of the step. Follow falls behind when the "
        "second half's median is over 1.5 times the first's and more than one tick "
        f"({tick:g} s) longer.",
        "",
        "| MB/s per file | Delay 1st half (s) | Delay 2nd half (s) | Delay 2nd half p95 (s) "
        "| Behind 2nd half (MB) | CPU avg | CPU peak | Memory peak (MiB) | Scratch peak (MiB) "
        "| Keeps up |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    first_behind = None
    for step in steps:
        mb = step["rate"] / growing.MB
        tag = f"{mb:g}"
        early = _metric(summary, f"follow_lag_seconds{{suite:growing_heavy,rate:{tag},part:early}}")
        late = _metric(summary, f"follow_lag_seconds{{suite:growing_heavy,rate:{tag},part:late}}")
        late_bytes = _metric(
            summary, f"follow_lag_bytes{{suite:growing_heavy,rate:{tag},part:late}}"
        )
        res = window(server, start + step["start"], start + step["end"]) if start else {}
        verdict = falls_behind(
            early.get("med") if early.get("count") else None,
            late.get("med") if late.get("count") else None,
            tick,
        )
        if verdict and first_behind is None:
            first_behind = mb
        out.append(
            f"| {mb:g} | {_s(early.get('med'))} | {_s(late.get('med'))} | "
            f"{_s(late.get('p(95)'))} | {_mib(late_bytes.get('med'))} | "
            f"{_cpu(res.get('cpu_avg'))} | {_cpu(res.get('cpu_max'))} | "
            f"{_mib(res.get('mem_max'))} | {_mib(res.get('scratch_max'))} | "
            f"{'–' if verdict is None else ('**no**' if verdict else 'yes')} |"
        )
    out.append("")
    if first_behind is not None:
        out.append(f"**Follow stops keeping up from {first_behind:g} MB/s per file.**")
    else:
        out.append("Follow kept up at every rate measured.")
    out.append("")
    return out


def daily_markdown(name: str, summary: dict, phase_dir: Path, profile: dict) -> list[str]:
    size = float(growing.setting(profile, "daily_size_gb")) * 1024 * growing.MB
    duration = float(growing.setting(profile, "daily_duration_min")) * 60
    segments = growing.daily_schedule(
        size,
        duration,
        float(growing.setting(profile, "daily_business_share")),
        float(growing.setting(profile, "daily_peak_factor")),
    )
    events = writer_events(phase_dir)
    start = next((e["t"] for e in events if e.get("event") == "start"), None)
    sizes = [e for e in events if e.get("event") == "sizes"]
    server = read_server(phase_dir / "server.csv")
    hour_s = duration / 24
    refused = int(_metric(summary, "fetch_refusals{suite:daily_log}").get("count", 0))
    whole = sum(
        int(_metric(summary, f"http_req_duration{{suite:daily_log,gop:{op}}}").get("count", 0))
        for op in ("open", "download")
    )
    out = [
        f"### A day of a busy log (phase `{name}`)",
        "",
        f"One file grows to {_size(size)} over a day compressed into {duration / 60:g} "
        f"minutes ({hour_s:g} s per hour). Whole reads (open, download): {whole:,}, of which "
        f"refused as over MAX_FETCH_MB: {refused:,}. Delay of Follow per hour, and the server's "
        "resources in that hour:",
        "",
        "| Hour | Write MB/s (peak) | File at the end (MiB) | Delay p50 (s) | Delay p95 (s) "
        "| Behind p95 (MB) | CPU avg | CPU peak | Memory peak (MiB) | Scratch peak (MiB) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for hour in range(24):
        segs = [s for s in segments if s["hour"] == hour]
        mean = sum((s["end"] - s["start"]) * s["rate"] for s in segs) / hour_s
        peak = max(s["rate"] for s in segs)
        lag = _metric(summary, f"follow_lag_seconds{{suite:daily_log,hour:{hour}}}")
        behind = _metric(summary, f"follow_lag_bytes{{suite:daily_log,hour:{hour}}}")
        res, at_end = {}, None
        if start:
            res = window(server, start + hour * hour_s, start + (hour + 1) * hour_s)
            done = [e for e in sizes if e.get("elapsed", 0) <= (hour + 1) * hour_s]
            if done:
                at_end = sum(done[-1]["files"].values())
        rate = f"{mean / growing.MB:.2f}" + (
            f" ({peak / growing.MB:.1f})" if peak > mean * 1.01 else ""
        )
        out.append(
            f"| {hour:02d} | {rate} | {_mib(at_end)} | {_s(lag.get('med'))} | "
            f"{_s(lag.get('p(95)'))} | {_mib(behind.get('p(95)'))} | {_cpu(res.get('cpu_avg'))} | "
            f"{_cpu(res.get('cpu_max'))} | {_mib(res.get('mem_max'))} | "
            f"{_mib(res.get('scratch_max'))} |"
        )
    out.append("")
    return out


# --- production ------------------------------------------------------------------

PRODUCTION_OPS = (
    "login",
    "page",
    "browse",
    "open",
    "open-large",
    "follow",
    "search",
    "download",
    "download-zip",
    "admin",
    "close",
    "logout",
    "background",
)
LEAK_SHARE_PER_HOUR = 0.05


def persona_rows(summary: dict, personas: list[str]) -> list[dict]:
    """One row per kind of person and operation that had requests."""
    rows = []
    for persona in personas:
        for op in PRODUCTION_OPS:
            tags = f"persona:{persona},gop:{op}"
            values = _metric(summary, f"http_req_duration{{{tags}}}")
            if not values.get("count"):
                continue
            rows.append(
                {
                    "persona": persona,
                    "op": op,
                    "requests": int(values["count"]),
                    "p50": values.get("med"),
                    "p95": values.get("p(95)"),
                    "errors": _metric(summary, f"http_req_failed{{{tags}}}").get("rate"),
                }
            )
    return rows


def planned_users(stages: list[dict], t: float) -> float:
    """How many people the plan has signed in `t` seconds after the start
    (k6 ramps linearly within a stage)."""
    start, level = 0.0, 0.0
    for stage in stages:
        end = start + stage["duration"]
        if t < end:
            return level + (stage["target"] - level) * (t - start) / max(1, stage["duration"])
        start, level = end, stage["target"]
    return level


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (seconds, value) points, per hour."""
    if len(points) < 3:
        return None
    n = len(points)
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    var = sum((p[0] - mx) ** 2 for p in points)
    if not var:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in points) / var * 3600


def time_series(server: list[dict], stages: list[dict], bucket: float) -> list[dict]:
    """The collector's samples in buckets of `bucket` seconds."""
    if not server:
        return []
    t0 = _epoch(server[0].get("timestamp")) or 0
    buckets: dict[int, list[dict]] = {}
    for row in server:
        t = (_epoch(row.get("timestamp")) or t0) - t0
        buckets.setdefault(int(t // bucket), []).append(row)
    out = []
    previous_refused = None
    for index in sorted(buckets):
        rows = buckets[index]
        cpu = column(rows, "cpu_percent")
        mem = column(rows, "mem_bytes")
        refused = column(rows, "limit_rejections_total")

        def last(name: str, rows=rows):
            values = column(rows, name)
            return values[-1] if values else None

        out.append(
            {
                "minute": index * bucket / 60,
                "users": planned_users(stages, (index + 0.5) * bucket),
                "cpu_avg": sum(cpu) / len(cpu) if cpu else None,
                "cpu_max": max(cpu) if cpu else None,
                "mem_avg": sum(mem) / len(mem) if mem else None,
                "mem_max": max(mem) if mem else None,
                "scratch_max": max(column(rows, "scratch_bytes") or [0]) or None,
                "db": last("db_bytes"),
                "wal": max(column(rows, "wal_bytes") or [0]) or None,
                "audit_rows": last("audit_rows"),
                "slots_max": max(column(rows, "slots_in_use") or [0]),
                "refused": (
                    refused[-1] - (previous_refused if previous_refused is not None else refused[0])
                )
                if refused
                else None,
            }
        )
        if refused:
            previous_refused = refused[-1]
    return out


def write_series_csv(path: Path, series: list[dict]) -> None:
    if not series:
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(series[0]))
        writer.writeheader()
        for row in series:
            writer.writerow({k: "" if v is None else round(v, 3) for k, v in row.items()})


def memory_trend(server: list[dict]) -> dict:
    """Memory's growth per hour, leaving out the first tenth of the run
    (start-up and the first sign-ins), as MiB/h and as a share of the
    median: a steady climb hints at a leak."""
    points = []
    t0 = None
    for row in server:
        t = _epoch(row.get("timestamp"))
        mem = _num(row.get("mem_bytes"))
        if t is None or mem is None:
            continue
        t0 = t if t0 is None else t0
        points.append((t - t0, mem))
    if not points:
        return {}
    span = points[-1][0]
    kept = [p for p in points if p[0] >= span * 0.1]
    slope = slope_per_hour(kept)
    median = sorted(p[1] for p in kept)[len(kept) // 2] if kept else None
    return {
        "hours": span / 3600,
        "slope": slope,
        "share": slope / median if slope is not None and median else None,
    }


def production_verdict(summary: dict, trend: dict, refused: float) -> list[str]:
    """Where the run was outside its limits; empty when it was within."""
    problems = []
    for key, metric in summary.get("metrics", {}).items():
        if "persona:" in key:
            continue
        if not (
            "scenario:production" in key or "scenario:incident" in key or "suite:production" in key
        ):
            continue
        for expr, result in metric.get("thresholds", {}).items():
            if not result.get("ok", True):
                problems.append(f"`{key}` {expr}")
    if trend.get("share") is not None and trend.get("hours", 0) >= 1:
        if trend["share"] > LEAK_SHARE_PER_HOUR:
            problems.append(
                f"memory grew {trend['slope'] / MIB:,.1f} MiB per hour "
                f"({trend['share'] * 100:.1f} % of its median), a possible leak"
            )
    if refused:
        problems.append(f"{refused:,.0f} requests refused by PerchTail's limits (503)")
    return problems


def production_markdown(name: str, summary: dict, phase_dir: Path, run: dict) -> list[str]:
    plan = run.get("production") or {}
    stages = plan.get("stages", [])
    personas = list(plan.get("personas", {}))
    server = read_server(phase_dir / "server.csv")
    total = float(plan.get("duration") or 0)
    bucket = 600 if total >= 3 * 3600 else 300 if total >= 3600 else 60
    series = time_series(server, stages, bucket)
    write_series_csv(phase_dir / "timeseries.csv", series)
    trend = memory_trend(server)
    refused = sum(r["refused"] or 0 for r in series)
    sessions = {
        p: int(_metric(summary, f"sessions{{persona:{p}}}").get("count", 0)) for p in personas
    }
    peak = plan.get("peak", [0, 0])
    inc = plan.get("incident", {})
    agents = run.get("agents", [])
    windows = [a for a in agents if a.get("windows")]
    out = [
        f"### Production (phase `{name}`)",
        "",
        f"- Plan: {sum(len(p.get('users', [])) for p in plan.get('personas', {}).values())} users; "
        f"at most {max([s['target'] for s in stages] or [0])} signed in at once, the peak from "
        f"minute {peak[0] / 60:g} to {peak[1] / 60:g}; sources by write rate "
        f"{json.dumps(plan.get('classes', {}))}; alerts {json.dumps(plan.get('alerts', {}))}.",
        f"- Incident: {inc.get('users', 0)} users on `{inc.get('source')}` `{inc.get('path')}` "
        f"from minute {inc.get('start', 0) / 60:g} for {inc.get('duration', 0) / 60:g} minutes, "
        f"{inc.get('zip_users', 0)} of them downloading its folder as a ZIP.",
        "- Sessions per kind of person: "
        + ", ".join(f"{p} {n}" for p, n in sessions.items())
        + ".",
    ]
    if windows:
        oses = sorted({a.get("os") or "not connected" for a in windows})
        out.append(
            f"- Windows agents: {len(windows)} planned, ran on the "
            f"{plan.get('windows_agents', {}).get('on', '?')}; reported OS: {', '.join(oses)}."
        )
    integrity = int(_metric(summary, "integrity_errors{suite:production}").get("count", 0))
    out += [
        f"- Integrity errors (every open and download checked): **{integrity}**.",
        "",
        *follow_refusal_lines(summary, "production"),
        "| Kind | Operation | Requests | p50 (ms) | p95 (ms) | Errors |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for row in persona_rows(summary, personas + ["incident"]):
        out.append(
            f"| {row['persona']} | {row['op']} | {row['requests']:,} | {_ms(row['p50'])} | "
            f"{_ms(row['p95'])} | {_pct(row['errors'])} |"
        )
    out += [
        "",
        f"Over time (every {bucket / 60:g} minutes; also in `{name}/timeseries.csv`):",
        "",
        "| Minute | Users (plan) | CPU avg | CPU peak | Memory avg (MiB) | Memory peak (MiB) "
        "| Scratch peak (MiB) | DB (MiB) | WAL peak (MiB) | Audit rows | Slots (max) | Refused |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in series:
        out.append(
            f"| {r['minute']:g} | {r['users']:.0f} | {_cpu(r['cpu_avg'])} | {_cpu(r['cpu_max'])} | "
            f"{_mib(r['mem_avg'])} | {_mib(r['mem_max'])} | {_mib(r['scratch_max'])} | "
            f"{_mib(r['db'])} | {_mib(r['wal'])} | "
            f"{'–' if r['audit_rows'] is None else f'{r["audit_rows"]:,.0f}'} | "
            f"{r['slots_max']:.0f} | {'–' if r['refused'] is None else f'{r["refused"]:.0f}'} |"
        )
    out.append("")
    if trend.get("slope") is not None:
        out.append(
            f"Memory trend: {trend['slope'] / MIB:+,.1f} MiB per hour over {trend['hours']:.1f} h "
            f"({trend['share'] * 100:+.1f} % of its median per hour)"
            + ("." if trend["hours"] >= 1 else "; too short a run to judge a leak.")
        )
        out.append("")
    problems = production_verdict(summary, trend, refused)
    if problems:
        out.append("**Verdict: not within the limits.** Where:")
        out.append("")
        out += [f"- {p}" for p in problems]
    else:
        out.append("**Verdict: within the limits.**")
    out.append("")
    return out


def matrix_markdown(name: str, phase_dir: Path) -> list[str]:
    """protocol_matrix: each measured cell, from measurements.csv."""
    path = phase_dir / "measurements.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    out = [
        f"### Protocol matrix (phase `{name}`)",
        "",
        "Every cell measured, one after another (PerchTail's CPU per request from its cgroup over "
        "the cell's window; the full columns are in "
        f"`{name}/measurements.csv`, each request in `{name}/requests.csv`; "
        "`report.py costs` works out the cost units).",
        "",
        "| Step | Protocol | Operation | Size / entries | At once | Delay (ms) | Requests | Errors "
        "| p50 (ms) | p95 (ms) | First byte p50 (ms) | MB/s | CPU ms per request | Notes |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in rows:
        size = _num(r.get("size_bytes"))
        what = f"{size / MIB:g} MB" if size else (r.get("entries") or "")
        requests = _num(r.get("requests")) or 0
        cpu = _num(r.get("server_cpu_ms"))
        per = cpu / requests if cpu is not None and requests else None
        rate = _num(r.get("mb_per_s"))
        out.append(
            f"| {r['step']} | {r['protocol'] or '–'} | {r['operation']} | {what or '–'} | "
            f"{r.get('parallelism') or '–'} | {r.get('delay_ms') or '–'} | {int(requests)} | "
            f"{r.get('errors') or 0} | {_ms(_num(r.get('latency_p50_ms')))} | "
            f"{_ms(_num(r.get('latency_p95_ms')))} | {_ms(_num(r.get('ttfb_p50_ms')))} | "
            f"{'–' if rate is None else f'{rate:.1f}'} | {_ms(per)} | {r.get('notes') or ''} |"
        )
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
    growing_section: list[str] = []
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
        for suite in GROWING_SUITES:
            if suite in phase["scenarios"]:
                growing_section += growing_markdown(name, suite, summary, run_dir / name)
        tick = float(phase.get("settings", {}).get("tail_interval_seconds", 2))
        if "growing_heavy" in phase["scenarios"]:
            growing_section += heavy_markdown(name, summary, run_dir / name, profile, tick)
        if "daily_log" in phase["scenarios"]:
            growing_section += daily_markdown(name, summary, run_dir / name, profile)
        if "production" in phase["scenarios"]:
            growing_section += production_markdown(name, summary, run_dir / name, run)
        if "protocol_matrix" in phase["scenarios"]:
            growing_section += matrix_markdown(name, run_dir / name)
    return (
        "\n".join(lines + [""] + resources + [""] + ramp_section + growing_section + sizing) + "\n"
    )


def costs_main(argv: list[str]) -> None:
    """report.py costs RUN [--production RUN] [--source-csv FILE ...]."""
    import costs  # noqa: PLC0415

    parser = argparse.ArgumentParser(
        prog="report.py costs", description="Cost units of a protocol_matrix run."
    )
    parser.add_argument("run_dir", type=Path, help="the protocol_matrix run's folder")
    parser.add_argument("--production", type=Path, help="a production run, for per-user units")
    parser.add_argument(
        "--source-csv",
        type=Path,
        nargs="+",
        action="extend",
        default=[],
        metavar="FILE",
        help="a source collector's file (collect/source_collect.py or .ps1); repeatable",
    )
    args = parser.parse_args(argv)
    missing = [str(p) for p in args.source_csv if not p.is_file()]
    if missing:
        parser.error(f"no such source collector file: {', '.join(missing)}")
    text = costs.costs(args.run_dir, args.production, args.source_csv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(text)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "costs":
        costs_main(argv[1:])
        return
    if len(argv) != 1:
        print(__doc__)
        sys.exit(2)
    run_dir = Path(argv[0])
    text = build(run_dir)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    (run_dir / "summary.md").write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
