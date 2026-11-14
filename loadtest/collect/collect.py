"""Records the server's resource use every few seconds while a load test
runs.

    python loadtest/collect/collect.py --out DIR --container perchtail-perchtail-1
    python loadtest/collect/collect.py --out DIR --pid 1234 \
        --scratch-dir /data/scratch --db /data/db/perchtail.db

Each sample (every --interval seconds, 5 by default) has:

- CPU and memory of the server process: `docker stats` for a container,
  /proc/<pid> for a process on this machine (Linux);
- bytes used under the scratch directory, and the size of the SQLite
  database and its WAL, measured on disk (`docker exec` into the container,
  or the local paths);
- PerchTail's own report, GET /monitoring/health, read with the
  monitoring token (LOADTEST_MONITORING_TOKEN, or the one seed.py saved),
  never with a user's credentials.

Writes DIR/server.csv (one row per sample), DIR/health.jsonl (each health
report as returned) and DIR/hardware.json (this machine, and the Docker
VM's CPUs and memory). Stops after --seconds, or when DIR/collect.stop
appears (how loadtest/run.py stops it on every OS).
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "seed"))

from common import KitError, base_url, check_state, fail, read_state  # noqa: E402

FIELDS = [
    "timestamp",
    "elapsed_s",
    "cpu_percent",
    "mem_bytes",
    "mem_limit_bytes",
    "pids",
    "scratch_bytes",
    "db_bytes",
    "wal_bytes",
    "health_status",
    "health_db_latency_ms",
    "health_scratch_used_bytes",
    "health_db_size_bytes",
    "health_db_wal_bytes",
    "slots_in_use",
    "slots_max",
    "transfers_in_use",
    "limit_rejections_total",
    "agents_connected",
    "name_index_oldest_age_s",
    "source_checks_failing",
]
_UNITS = {
    "b": 1,
    "kb": 1e3,
    "kib": 1024,
    "mb": 1e6,
    "mib": 1024**2,
    "gb": 1e9,
    "gib": 1024**3,
    "tb": 1e12,
    "tib": 1024**4,
}


def _bytes(text: str) -> int | None:
    match = re.match(r"\s*([\d.]+)\s*([a-zA-Z]+)", text or "")
    if not match:
        return None
    return int(float(match.group(1)) * _UNITS.get(match.group(2).lower(), 1))


def monitoring_token() -> str:
    token = os.environ.get("LOADTEST_MONITORING_TOKEN") or (
        read_state("monitoring.json") or {}
    ).get("token")
    if not token:
        raise KitError("no monitoring token: set LOADTEST_MONITORING_TOKEN or run seed.py apply")
    return token


# --- process samplers ---------------------------------------------------------


class DockerSampler:
    def __init__(self, container: str, scratch_dir: str, db_path: str):
        self.container, self.scratch_dir, self.db_path = container, scratch_dir, db_path

    def process(self) -> dict:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{json .}}", self.container],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        if not out:
            return {}
        stats = json.loads(out.splitlines()[0])
        used, _, limit = stats.get("MemUsage", "").partition("/")
        return {
            "cpu_percent": float(stats.get("CPUPerc", "0").rstrip("%") or 0),
            "mem_bytes": _bytes(used),
            "mem_limit_bytes": _bytes(limit),
            "pids": int(stats.get("PIDs") or 0),
        }

    def disk(self) -> dict:
        scratch, db, wal = (
            shlex.quote(p) for p in (self.scratch_dir, self.db_path, self.db_path + "-wal")
        )
        script = (
            f"du -sb {scratch} 2>/dev/null | cut -f1; "
            f"stat -c %s {db} 2>/dev/null || echo; "
            f"stat -c %s {wal} 2>/dev/null || echo"
        )
        out = subprocess.run(
            ["docker", "exec", self.container, "sh", "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.splitlines()
        values = [int(v) if v.strip().isdigit() else None for v in (out + ["", "", ""])[:3]]
        return dict(zip(("scratch_bytes", "db_bytes", "wal_bytes"), values, strict=True))


class ProcSampler:
    """A PerchTail process on this (Linux) machine, read from /proc."""

    def __init__(self, pid: int, scratch_dir: str, db_path: str):
        self.pid, self.scratch_dir, self.db_path = pid, Path(scratch_dir), Path(db_path)
        self.ticks = os.sysconf("SC_CLK_TCK")
        self.last: tuple[float, int] | None = None

    def process(self) -> dict:
        fields = Path(f"/proc/{self.pid}/stat").read_text().rsplit(")", 1)[1].split()
        cpu_ticks = int(fields[11]) + int(fields[12])  # utime + stime
        now = time.monotonic()
        percent = None
        if self.last:
            percent = (cpu_ticks - self.last[1]) / self.ticks / (now - self.last[0]) * 100
        self.last = (now, cpu_ticks)
        status = Path(f"/proc/{self.pid}/status").read_text()
        rss = re.search(r"VmRSS:\s+(\d+) kB", status)
        threads = re.search(r"Threads:\s+(\d+)", status)
        return {
            "cpu_percent": round(percent, 2) if percent is not None else None,
            "mem_bytes": int(rss.group(1)) * 1024 if rss else None,
            "pids": int(threads.group(1)) if threads else None,
        }

    def disk(self) -> dict:
        total = 0
        for root, _dirs, files in os.walk(self.scratch_dir):
            for name in files:
                try:
                    total += os.lstat(os.path.join(root, name)).st_size
                except OSError:
                    pass
        wal = Path(str(self.db_path) + "-wal")
        return {
            "scratch_bytes": total,
            "db_bytes": self.db_path.stat().st_size if self.db_path.exists() else None,
            "wal_bytes": wal.stat().st_size if wal.exists() else None,
        }


# --- health -------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib sends the Authorization header on to a redirect's target;
    refusing every redirect keeps the monitoring token on the instance."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def read_health(url: str, token: str) -> dict | None:
    """The health report, or None when the instance doesn't answer with
    one. A redirect raises KitError: it is never followed."""
    req = urllib.request.Request(
        f"{url}/monitoring/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with _OPENER.open(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            location = exc.headers.get("Location") if exc.headers else None
            target = f" to {location}" if location else ""
            raise KitError(
                f"{url}/monitoring/health answered with a redirect (HTTP {exc.code}{target}); "
                "the collector doesn't follow redirects, so the monitoring token isn't sent "
                "elsewhere: set LOADTEST_BASE_URL to the address PerchTail answers on"
            ) from None
        return None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


def health_fields(health: dict | None) -> dict:
    if not health:
        return {"health_status": "unreachable"}
    db, scratch, limits = (
        health.get("database", {}),
        health.get("scratch", {}),
        health.get("limits", {}),
    )
    return {
        "health_status": health.get("status"),
        "health_db_latency_ms": db.get("latency_ms"),
        "health_scratch_used_bytes": scratch.get("used_bytes"),
        "health_db_size_bytes": db.get("size_bytes"),
        "health_db_wal_bytes": db.get("wal_bytes"),
        "slots_in_use": limits.get("global_in_use"),
        "slots_max": limits.get("global_max"),
        "transfers_in_use": limits.get("transfers_in_use"),
        "limit_rejections_total": sum((limits.get("rejected") or {}).values()),
        "agents_connected": (health.get("agent") or {}).get("connected"),
        "name_index_oldest_age_s": (health.get("name_index") or {}).get("oldest_age_seconds"),
        "source_checks_failing": (health.get("source_checks") or {}).get("failing"),
    }


# --- hardware -----------------------------------------------------------------


def _total_ram() -> int | None:
    if sys.platform.startswith("linux"):
        match = re.search(r"MemTotal:\s+(\d+) kB", Path("/proc/meminfo").read_text())
        return int(match.group(1)) * 1024 if match else None
    if os.name == "nt":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in (
                    "total",
                    "avail",
                    "totalPage",
                    "availPage",
                    "totalVirtual",
                    "availVirtual",
                    "ext",
                )
            ]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return int(status.total)
    return None


def _cpu_model() -> str:
    if sys.platform.startswith("linux"):
        match = re.search(r"model name\s*:\s*(.+)", Path("/proc/cpuinfo").read_text())
        if match:
            return match.group(1).strip()
    if os.name == "nt":
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
            )
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or platform.machine()


def hardware(container: str | None, out_dir: Path) -> dict:
    info = {
        "os": platform.platform(),
        "cpu_model": _cpu_model(),
        "logical_cpus": os.cpu_count(),
        "ram_bytes": _total_ram(),
        "results_disk_free_bytes": shutil.disk_usage(out_dir).free,
    }
    if container:
        docker = subprocess.run(
            ["docker", "info", "--format", "{{json .}}"], capture_output=True, text=True, timeout=30
        ).stdout
        try:
            d = json.loads(docker)
            info["docker"] = {
                "cpus": d.get("NCPU"),
                "mem_bytes": d.get("MemTotal"),
                "os": d.get("OperatingSystem"),
                "kernel": d.get("KernelVersion"),
                "storage_driver": d.get("Driver"),
            }
        except ValueError:
            pass
    return info


# --- main loop ----------------------------------------------------------------


def run(args: argparse.Namespace) -> None:
    url = base_url()
    token = monitoring_token()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stop_file = out_dir / "collect.stop"
    if stop_file.exists():
        stop_file.unlink()
    if args.container:
        sampler = DockerSampler(args.container, args.scratch_dir, args.db)
    elif args.pid:
        sampler = ProcSampler(args.pid, args.scratch_dir, args.db)
    else:
        raise KitError("give --container or --pid")

    hw = hardware(args.container, out_dir)
    health = read_health(url, token)
    if health:
        hw["perchtail_version"] = health.get("version")
        hw["perchtail_disks"] = health.get("disks")
    (out_dir / "hardware.json").write_text(json.dumps(hw, indent=2), encoding="utf-8")

    started = time.monotonic()
    deadline = started + args.seconds if args.seconds else None
    new_file = not (out_dir / "server.csv").exists()
    with (
        (out_dir / "server.csv").open("a", newline="", encoding="utf-8") as csv_handle,
        (out_dir / "health.jsonl").open("a", encoding="utf-8") as health_handle,
    ):
        writer = csv.DictWriter(csv_handle, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        while not stop_file.exists() and (deadline is None or time.monotonic() < deadline):
            tick = time.monotonic()
            row: dict = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "elapsed_s": round(tick - started, 1),
            }
            results: dict = {}

            def sample(name, fn, results=results):
                try:
                    results[name] = fn()
                except Exception as exc:  # a failed sample is a gap, not the end of the run
                    results[name] = {}
                    print(f"collect: {name} sample failed: {exc}", file=sys.stderr)

            threads = [
                threading.Thread(target=sample, args=("process", sampler.process)),
                threading.Thread(target=sample, args=("disk", sampler.disk)),
                threading.Thread(target=sample, args=("health", lambda: read_health(url, token))),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            row.update(results.get("process") or {})
            row.update(results.get("disk") or {})
            row.update(health_fields(results.get("health")))
            writer.writerow(row)
            csv_handle.flush()
            if results.get("health"):
                health_handle.write(
                    json.dumps({"timestamp": row["timestamp"], **results["health"]}) + "\n"
                )
                health_handle.flush()
            time.sleep(max(0.0, args.interval - (time.monotonic() - tick)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", required=True, help="results directory for this phase")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument(
        "--seconds", type=float, default=0, help="stop after this long (0: until collect.stop)"
    )
    parser.add_argument("--container", help="the PerchTail container (docker stats / docker exec)")
    parser.add_argument("--pid", type=int, help="the PerchTail process on this machine (/proc)")
    parser.add_argument("--scratch-dir", default="/data/scratch")
    parser.add_argument("--db", default="/data/db/perchtail.db")
    args = parser.parse_args()
    try:
        check_state()
        run(args)
    except KitError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
