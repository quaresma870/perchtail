"""Cost units from the measurements of a protocol_matrix run, and, when it
is given, from a production run:

    python loadtest/report/report.py costs loadtest/results/<date>-protocol_matrix \\
        [--production loadtest/results/<date>-production] [--source-csv FILE ...]

Writes costs.csv (one row per unit: unit, protocol, persona, operation,
metric, value, basis) and costs.md next to the matrix run's run.json, and
prints the Markdown. Only numbers and how each was worked out: no
conclusions or recommendations.

- per operation: PerchTail's CPU time, the source's CPU time, bytes on the
  wire and packets, per request, for each protocol, operation, size and
  number at once (no added network delay);
- per MB transferred: PerchTail's CPU time and the bytes that crossed its
  interface from the source, per MB of file, for opens and downloads, by
  protocol;
- per source: a source check and a file-name index pass (and per 10 000
  files), and what they cost per hour at the instance's intervals;
- per operation on the test servers (with --source-csv, the files of
  collect/source_collect.py or .ps1): CPU time and disk bytes of each
  server process over each cell's window;
- per active user (with --production): PerchTail's CPU time, the database's
  and audit log's growth per signed-in user per hour over the whole run,
  and for each kind of person, its requests per hour of session by
  operation, with the matrix's CPU time per request where an operation has
  a counterpart there.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

MB = 1024 * 1024
COSTS_COLUMNS = ["unit", "protocol", "persona", "operation", "metric", "value", "basis"]

# production's operations and the matrix cell that stands for each (the
# smallest size measured for opens and downloads of ordinary logs).
OPERATION_MATRIX = {
    "browse": ("list", "smallest"),
    "open": ("open", "smallest"),
    "open-large": ("open", "largest"),
    "download": ("download", "smallest"),
    "follow": ("follow", None),
}


def _num(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_measurements(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def find_measurements(run_dir: Path) -> list[Path]:
    return sorted(run_dir.glob("*/measurements.csv")) or sorted(run_dir.glob("measurements.csv"))


def per_request(row: dict, key: str) -> float | None:
    value, requests = _num(row.get(key)), _num(row.get("requests"))
    if value is None or not requests:
        return None
    return value / requests


def operation_units(rows: list[dict]) -> list[dict]:
    """One unit per protocol, operation, size (or entries) and parallelism
    of the matrix, without added delay."""
    out = []
    for r in rows:
        if r.get("step") != "matrix" or (_num(r.get("delay_ms")) or 0) != 0:
            continue
        size = r.get("size_bytes") or r.get("entries") or ""
        what = f"{r['operation']} {size}".strip()
        basis = f"{r['requests']} requests, {r['parallelism']} at once"
        for metric, key in (
            ("server_cpu_ms_per_request", "server_cpu_ms"),
            ("source_cpu_ms_per_request", "source_cpu_ms"),
            ("server_net_rx_bytes_per_request", "server_net_rx_bytes"),
            ("server_net_packets_per_request", "server_net_packets"),
        ):
            value = per_request(r, key)
            if value is not None:
                out.append(
                    {
                        "unit": "operation",
                        "protocol": r["protocol"],
                        "persona": "",
                        "operation": f"{what} x{r['parallelism']}",
                        "metric": metric,
                        "value": value,
                        "basis": basis,
                    }
                )
    return out


def mb_units(rows: list[dict]) -> list[dict]:
    """Per MB of file opened or downloaded, by protocol: the sums over the
    matrix's opens and downloads one at a time without added delay."""
    totals: dict[str, dict[str, float]] = {}
    for r in rows:
        if (
            r.get("step") != "matrix"
            or r.get("operation") not in ("open", "download")
            or (_num(r.get("delay_ms")) or 0) != 0
            or (_num(r.get("parallelism")) or 0) != 1
        ):
            continue
        t = totals.setdefault(r["protocol"], {"mb": 0.0, "cpu": 0.0, "rx": 0.0, "src": 0.0})
        t["mb"] += (_num(r.get("bytes")) or 0) / MB
        t["cpu"] += _num(r.get("server_cpu_ms")) or 0
        t["rx"] += _num(r.get("server_net_rx_bytes")) or 0
        t["src"] += _num(r.get("source_cpu_ms")) or 0
    out = []
    for protocol, t in sorted(totals.items()):
        if not t["mb"]:
            continue
        basis = f"{t['mb']:.1f} MB opened or downloaded one at a time"
        for metric, value in (
            ("server_cpu_ms_per_mb", t["cpu"] / t["mb"]),
            ("source_cpu_ms_per_mb", t["src"] / t["mb"]),
            ("wire_bytes_per_file_mb", t["rx"] / t["mb"]),
        ):
            out.append(
                {
                    "unit": "MB transferred",
                    "protocol": protocol,
                    "persona": "",
                    "operation": "open, download",
                    "metric": metric,
                    "value": value,
                    "basis": basis,
                }
            )
    return out


def source_units(rows: list[dict], check_minutes: float, index_minutes: float) -> list[dict]:
    """A source check and an index pass per source and protocol, per 10 000
    files, and per hour at the given intervals (0 turns a job off)."""
    by_protocol: dict[str, dict[str, dict]] = {}
    for r in rows:
        if r.get("step") == "fixed":
            by_protocol.setdefault(r["protocol"], {})[r["operation"]] = r
    out = []
    for protocol, ops in sorted(by_protocol.items()):
        check, index = ops.get("source-check"), ops.get("name-index")
        per_hour = {"cpu": 0.0, "rx": 0.0}
        for op, row, minutes in (
            ("source-check", check, check_minutes),
            ("name-index", index, index_minutes),
        ):
            if not row:
                continue
            cpu, rx = _num(row.get("server_cpu_ms")), _num(row.get("server_net_rx_bytes"))
            wall = _num(row.get("wall_s"))
            for metric, value in (
                ("server_cpu_ms_per_pass", cpu),
                ("server_net_rx_bytes_per_pass", rx),
                ("seconds_per_pass", wall),
            ):
                if value is not None:
                    out.append(
                        {
                            "unit": "source",
                            "protocol": protocol,
                            "persona": "",
                            "operation": op,
                            "metric": metric,
                            "value": value,
                            "basis": "one pass, nobody signed in",
                        }
                    )
            entries = _num(row.get("entries"))
            if op == "name-index" and cpu is not None and entries:
                out.append(
                    {
                        "unit": "source",
                        "protocol": protocol,
                        "persona": "",
                        "operation": op,
                        "metric": "server_cpu_ms_per_10000_files",
                        "value": cpu * 10000 / entries,
                        "basis": f"{entries:.0f} files indexed",
                    }
                )
            if minutes and cpu is not None:
                per_hour["cpu"] += cpu * 60 / minutes
                per_hour["rx"] += (rx or 0) * 60 / minutes
        if check or index:
            basis = f"source check every {check_minutes:g} min, index every {index_minutes:g} min"
            out.append(
                {
                    "unit": "source",
                    "protocol": protocol,
                    "persona": "",
                    "operation": "background",
                    "metric": "server_cpu_ms_per_hour",
                    "value": per_hour["cpu"],
                    "basis": basis,
                }
            )
            out.append(
                {
                    "unit": "source",
                    "protocol": protocol,
                    "persona": "",
                    "operation": "background",
                    "metric": "server_net_rx_bytes_per_hour",
                    "value": per_hour["rx"],
                    "basis": basis,
                }
            )
    return out


def matrix_cost(rows: list[dict], operation: str, which: str | None) -> float | None:
    """The mean CPU time per request across protocols, one at a time and
    without delay, of a matrix operation (at its smallest or largest size)."""
    cells = [
        r
        for r in rows
        if r.get("step") == "matrix"
        and r.get("operation") == operation
        and (_num(r.get("parallelism")) or 0) == 1
        and (_num(r.get("delay_ms")) or 0) == 0
    ]
    if which:
        sizes = [_num(r.get("size_bytes") or r.get("entries")) or 0 for r in cells]
        if sizes:
            pick = min(sizes) if which == "smallest" else max(sizes)
            cells = [r for r, s in zip(cells, sizes, strict=True) if s == pick]
    values = [v for v in (per_request(r, "server_cpu_ms") for r in cells) if v is not None]
    return sum(values) / len(values) if values else None


def user_units(production_dir: Path, rows: list[dict]) -> list[dict]:
    """Per active user, from a production run: totals over the run divided
    by its signed-in user-hours, and per kind of person."""
    run = json.loads((production_dir / "run.json").read_text(encoding="utf-8"))
    plan = run.get("production") or {}
    stages = plan.get("stages", [])
    user_hours = (
        sum(
            s["duration"] * (s["target"] + level) / 2
            for s, level in zip(stages, [0] + [x["target"] for x in stages[:-1]], strict=True)
        )
        / 3600
    )
    phase = next((p for p in run["phases"] if "production" in p["scenarios"]), None)
    if not phase or not user_hours:
        return []
    phase_dir = production_dir / phase["name"]
    out = []
    series = phase_dir / "timeseries.csv"
    server = phase_dir / "server.csv"
    basis = f"{user_hours:.1f} signed-in user-hours (the plan's stages)"
    if server.exists():
        with server.open(encoding="utf-8") as handle:
            samples = list(csv.DictReader(handle))
        interval = (
            (_num(samples[1].get("elapsed_s")) or 5) - (_num(samples[0].get("elapsed_s")) or 0)
            if len(samples) > 1
            else 5
        )
        cpu_ms = sum((_num(r.get("cpu_percent")) or 0) * interval * 10 for r in samples)
        out.append(
            {
                "unit": "active user",
                "protocol": "",
                "persona": "all",
                "operation": "",
                "metric": "server_cpu_ms_per_user_hour",
                "value": cpu_ms / user_hours,
                "basis": basis,
            }
        )
        rx = [
            _num(r.get("net_rx_bytes")) for r in samples if _num(r.get("net_rx_bytes")) is not None
        ]
        if len(rx) > 1:
            out.append(
                {
                    "unit": "active user",
                    "protocol": "",
                    "persona": "all",
                    "operation": "",
                    "metric": "server_net_rx_bytes_per_user_hour",
                    "value": (rx[-1] - rx[0]) / user_hours,
                    "basis": basis,
                }
            )
        for metric, key in (
            ("db_bytes_per_user_hour", "db_bytes"),
            ("audit_rows_per_user_hour", "audit_rows"),
        ):
            values = [_num(r.get(key)) for r in samples if _num(r.get(key)) is not None]
            if len(values) > 1:
                out.append(
                    {
                        "unit": "active user",
                        "protocol": "",
                        "persona": "all",
                        "operation": "",
                        "metric": metric,
                        "value": (values[-1] - values[0]) / user_hours,
                        "basis": basis,
                    }
                )
        mem = [_num(r.get("mem_bytes")) for r in samples if _num(r.get("mem_bytes")) is not None]
        peak = max((s["target"] for s in stages), default=0)
        if mem and peak:
            out.append(
                {
                    "unit": "active user",
                    "protocol": "",
                    "persona": "all",
                    "operation": "",
                    "metric": "server_mem_bytes_per_peak_user",
                    "value": (max(mem) - min(mem)) / peak,
                    "basis": f"memory above its lowest, at the peak of {peak} users",
                }
            )
    del series
    summary_path = phase_dir / f"k6-{phase['name']}.json"
    if not summary_path.exists():
        return out
    metrics = json.loads(summary_path.read_text(encoding="utf-8")).get("metrics", {})
    for persona, spec in plan.get("personas", {}).items():
        sessions = (metrics.get(f"sessions{{persona:{persona}}}", {}).get("values", {}) or {}).get(
            "count", 0
        )
        measured = metrics.get(f"session_minutes{{persona:{persona}}}", {}).get("values", {}) or {}
        if measured.get("count"):
            hours = measured["count"] * measured.get("avg", 0) / 60
            how = f"{measured['count']} sessions, {hours:.1f} h measured"
        else:
            lo, hi = spec.get("session_minutes", [0, 0])
            hours = sessions * (lo + hi) / 2 / 60
            how = f"{sessions} sessions, about {hours:.1f} h (the middle of session_minutes)"
        if not hours:
            continue
        cpu_estimate = 0.0
        for key, metric in metrics.items():
            prefix = f"http_req_duration{{persona:{persona},gop:"
            if not key.startswith(prefix):
                continue
            op = key[len(prefix) : -1]
            count = (metric.get("values") or {}).get("count", 0)
            if not count:
                continue
            rate = count / hours
            pbasis = how
            out.append(
                {
                    "unit": "active user",
                    "protocol": "",
                    "persona": persona,
                    "operation": op,
                    "metric": "requests_per_session_hour",
                    "value": rate,
                    "basis": pbasis,
                }
            )
            mapped = OPERATION_MATRIX.get(op)
            cost = matrix_cost(rows, *mapped) if mapped else None
            if cost is not None:
                cpu_estimate += rate * cost
                out.append(
                    {
                        "unit": "active user",
                        "protocol": "",
                        "persona": persona,
                        "operation": op,
                        "metric": "server_cpu_ms_per_session_hour",
                        "value": rate * cost,
                        "basis": f"matrix {mapped[0]} ({mapped[1] or 'as measured'}), "
                        "mean over protocols",
                    }
                )
        out.append(
            {
                "unit": "active user",
                "protocol": "",
                "persona": persona,
                "operation": "mapped operations",
                "metric": "server_cpu_ms_per_session_hour",
                "value": cpu_estimate,
                "basis": "operations with a matrix counterpart only",
            }
        )
    return out


def read_source_csv(path: Path) -> dict[str, list[dict]]:
    """A source_collect.py / .ps1 file: cumulative readings per process name."""
    series: dict[str, list[dict]] = {}
    with path.open(encoding="utf-8") as handle:
        for r in csv.DictReader(handle):
            t, cpu = _num(r.get("t")), _num(r.get("cpu_ms"))
            if t is None or cpu is None:
                continue
            series.setdefault(r["name"], []).append(
                {
                    "t": t,
                    "cpu_ms": cpu,
                    "read": _num(r.get("read_bytes")) or 0,
                    "write": _num(r.get("write_bytes")) or 0,
                }
            )
    for readings in series.values():
        readings.sort(key=lambda x: x["t"])
    return series


def _at(readings: list[dict], t: float, key: str) -> float | None:
    """A cumulative reading at time `t`, interpolated."""
    if not readings:
        return None
    if t <= readings[0]["t"]:
        return readings[0][key]
    for a, b in zip(readings, readings[1:], strict=False):
        if a["t"] <= t <= b["t"]:
            if b["t"] == a["t"]:
                return a[key]
            return a[key] + (b[key] - a[key]) * (t - a["t"]) / (b["t"] - a["t"])
    return readings[-1][key]


def source_csv_units(rows: list[dict], label: str, series: dict[str, list[dict]]) -> list[dict]:
    """What each measured cell cost on a test server, from the readings a
    source collector took there over the cell's window (start, end)."""
    out = []
    for r in rows:
        start, end, requests = _num(r.get("start")), _num(r.get("end")), _num(r.get("requests"))
        if start is None or end is None or not requests:
            continue
        for name, readings in sorted(series.items()):
            inside = [x for x in readings if start - 2 <= x["t"] <= end + 2]
            if len(inside) < 2:
                continue
            cpu = (_at(readings, end, "cpu_ms") or 0) - (_at(readings, start, "cpu_ms") or 0)
            io = sum(
                (_at(readings, end, k) or 0) - (_at(readings, start, k) or 0)
                for k in ("read", "write")
            )
            size = r.get("size_bytes") or r.get("entries") or ""
            what = f"{r['operation']} {size} x{r.get('parallelism') or 1}".replace("  ", " ")
            basis = f"{label}, {name}, {r['requests']} requests, delay {r.get('delay_ms') or 0} ms"
            for metric, value in (
                ("source_host_cpu_ms_per_request", max(0.0, cpu) / requests),
                ("source_host_disk_bytes_per_request", max(0.0, io) / requests),
            ):
                out.append(
                    {
                        "unit": "operation",
                        "protocol": r["protocol"],
                        "persona": "",
                        "operation": what,
                        "metric": metric,
                        "value": value,
                        "basis": basis,
                    }
                )
    return out


def write_costs(out_dir: Path, units: list[dict]) -> str:
    with (out_dir / "costs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COSTS_COLUMNS)
        writer.writeheader()
        for u in units:
            writer.writerow({**u, "value": f"{u['value']:.3f}"})
    lines = [
        "## Cost units",
        "",
        "| Unit | Protocol | Kind | Operation | Metric | Value | Basis |",
        "|---|---|---|---|---|---:|---|",
    ]
    for u in units:
        lines.append(
            f"| {u['unit']} | {u['protocol'] or '–'} | {u['persona'] or '–'} | "
            f"{u['operation'] or '–'} | "
            f"{u['metric']} | {u['value']:,.3f} | {u['basis']} |"
        )
    text = "\n".join(lines) + "\n"
    (out_dir / "costs.md").write_text(text, encoding="utf-8")
    return text


def costs(
    matrix_dir: Path, production_dir: Path | None = None, source_csvs: list[Path] | None = None
) -> str:
    rows = [r for path in find_measurements(matrix_dir) for r in read_measurements(path)]
    settings: dict = {}
    run_file = matrix_dir / "run.json"
    if run_file.exists():
        run = json.loads(run_file.read_text(encoding="utf-8"))
        for phase in run.get("phases", []):
            settings.update(phase.get("settings", {}))
    units = operation_units(rows) + mb_units(rows)
    units += source_units(
        rows,
        float(settings.get("source_check_minutes", 10)),
        float(settings.get("name_index_minutes", 15)),
    )
    for path in source_csvs or []:
        units += source_csv_units(rows, path.name, read_source_csv(path))
    if production_dir:
        units += user_units(production_dir, rows)
    return write_costs(matrix_dir, units)
