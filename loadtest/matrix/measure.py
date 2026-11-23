"""The pieces every measurement of the cost matrix shares: the fixed columns
of measurements.csv, an HTTP client that times each request (to the first
byte and to the last), the counters read before and after a window
(PerchTail's CPU, memory and network, and the source servers'), and how a
window is costed from them.

Counters are cumulative (CPU microseconds, bytes and packets on an
interface), read from the cgroup and the network interface of a container
(`docker exec`), or for a process on this machine from the OS. A
window's cost is the difference between the readings at its ends,
interpolated when the readings don't fall exactly on them.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import http.client
import json
import os
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "seed"))

from common import ApiError, base_url, check_allowed  # noqa: E402

# measurements.csv: one row per measured cell, the same columns for every
# step, so the files of different runs and steps can be put together.
COLUMNS = [
    "step",
    "protocol",
    "operation",
    "size_bytes",
    "entries",
    "parallelism",
    "delay_ms",
    "requests",
    "errors",
    "wall_s",
    "latency_p50_ms",
    "latency_p95_ms",
    "ttfb_p50_ms",
    "ttfb_p95_ms",
    "bytes",
    "mb_per_s",
    "server_cpu_ms",
    "server_mem_peak_bytes",
    "server_net_rx_bytes",
    "server_net_tx_bytes",
    "server_net_packets",
    "source_cpu_ms",
    "source_mem_peak_bytes",
    "source_net_bytes",
    "source_net_packets",
    "audit_rows",
    "start",
    "end",
    "notes",
]
REQUEST_COLUMNS = [
    "step",
    "cell",
    "protocol",
    "operation",
    "start",
    "ttfb_ms",
    "total_ms",
    "bytes",
    "status",
    "error",
]


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))
    return ordered[index]


class CsvOut:
    """measurements.csv and requests.csv of a run, appended as cells end."""

    def __init__(self, out_dir: Path):
        out_dir.mkdir(parents=True, exist_ok=True)
        self.measurements = out_dir / "measurements.csv"
        self.requests = out_dir / "requests.csv"
        for path, columns in ((self.measurements, COLUMNS), (self.requests, REQUEST_COLUMNS)):
            if not path.exists():
                with path.open("w", newline="", encoding="utf-8") as handle:
                    csv.writer(handle).writerow(columns)
        self.lock = threading.Lock()

    def row(self, values: dict) -> None:
        unknown = set(values) - set(COLUMNS)
        if unknown:
            raise ValueError(f"not a measurements.csv column: {sorted(unknown)}")
        with self.lock, self.measurements.open("a", newline="", encoding="utf-8") as handle:
            csv.writer(handle).writerow([_cell(values.get(c)) for c in COLUMNS])

    def request_rows(self, rows: list[dict]) -> None:
        with self.lock, self.requests.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            for r in rows:
                writer.writerow([_cell(r.get(c)) for c in REQUEST_COLUMNS])


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


# --- HTTP ---------------------------------------------------------------------


@dataclass
class Timed:
    status: int
    ttfb_ms: float | None
    total_ms: float
    bytes: int
    headers: dict = field(default_factory=dict)
    body: bytes | None = None
    error: str | None = None
    start: float = 0.0


class Session:
    """A signed-in user, like the web UI: the session cookie, and the
    page's Origin on requests that change something."""

    def __init__(self, url: str | None = None):
        self.url = (url or base_url()).rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        check_allowed(self.url)
        self.parts = parts
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.cookie = ""

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        cls = (
            http.client.HTTPSConnection
            if self.parts.scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(self.parts.hostname, self.parts.port, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        keep: bool = False,
        timeout: float = 600,
        extra_headers: dict | None = None,
    ) -> Timed:
        """One request, read to the end: time to the first byte of the body
        (headers included) and to the last. `keep` keeps the body."""
        headers = {"Accept": "application/json", **(extra_headers or {})}
        if self.cookie:
            headers["Cookie"] = self.cookie
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers["Origin"] = self.origin
        start = time.time()
        t0 = time.perf_counter()
        conn = self._connection(timeout)
        try:
            conn.request(method, self.parts.path + path, payload, headers)
            resp = conn.getresponse()
            first = resp.read(1)
            ttfb = (time.perf_counter() - t0) * 1000
            chunks = [first] if keep else None
            size = len(first)
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if keep:
                    chunks.append(chunk)
            total = (time.perf_counter() - t0) * 1000
            result = Timed(resp.status, ttfb, total, size, dict(resp.getheaders()), start=start)
            if keep:
                result.body = b"".join(chunks)
            declared = result.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) != size:
                result.error = f"{size} of {declared} bytes"
            return result
        except (OSError, http.client.HTTPException) as exc:
            return Timed(0, None, (time.perf_counter() - t0) * 1000, 0, error=str(exc), start=start)
        finally:
            conn.close()

    def login(self, username: str, password: str, mfa_code: str | None = None) -> Timed:
        body = {"username": username, "password": password}
        if mfa_code:
            body["mfa_code"] = mfa_code
        result = self.request("POST", "/auth/login", body, keep=True)
        cookie = result.headers.get("set-cookie") or result.headers.get("Set-Cookie") or ""
        if result.status == 200 and cookie:
            self.cookie = cookie.split(";", 1)[0]
        return result

    def json(self, method: str, path: str, body: dict | None = None) -> dict | list:
        result = self.request(method, path, body, keep=True)
        if not 200 <= result.status < 300:
            raise ApiError(
                method, path, result.status, (result.body or b"")[:200].decode(errors="replace")
            )
        return json.loads(result.body) if result.body else {}


def totp(secret: str, at: float | None = None, step: int = 30, digits: int = 6) -> str:
    """The TOTP code (RFC 6238, SHA-1) of a base32 secret at a time."""
    key = __import__("base64").b32decode(secret.upper() + "=" * (-len(secret) % 8))
    counter = int((at if at is not None else time.time()) // step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"


# --- counters -------------------------------------------------------------------

# Read in a container: cgroup v2 CPU and memory, falling back to v1, and the
# first interface's byte and packet counters.
COUNTERS_SCRIPT = (
    "cat /sys/fs/cgroup/cpu.stat 2>/dev/null | sed -n 's/^usage_usec /cpu_usec /p' || true; "
    "[ -f /sys/fs/cgroup/cpuacct/cpuacct.usage ] && "
    "echo cpu_usec $(( $(cat /sys/fs/cgroup/cpuacct/cpuacct.usage) / 1000 )); "
    "echo mem $(cat /sys/fs/cgroup/memory.current 2>/dev/null "
    "|| cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || echo 0); "
    "for k in rx_bytes tx_bytes rx_packets tx_packets; do "
    "echo $k $(cat /sys/class/net/eth0/statistics/$k 2>/dev/null || echo 0); done"
)


def container_counters(container: str) -> dict[str, float]:
    """CPU (µs), memory (bytes) and eth0's counters of a container."""
    out = subprocess.run(
        ["docker", "exec", container, "sh", "-c", COUNTERS_SCRIPT],
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    values: dict[str, float] = {"t": time.time()}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("-").isdigit():
            values[parts[0]] = float(parts[1])
    return values


def process_counters(pids: list[int]) -> dict[str, float]:
    """CPU (µs) and resident memory (bytes) of processes on this machine
    (the kit's agents): /proc on Linux, GetProcessTimes on Windows."""
    cpu = mem = 0.0
    for pid in pids:
        try:
            if os.name == "nt":
                c, m = _windows_process(pid)
            else:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                c = (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK") * 1e6
                m = int(fields[21]) * os.sysconf("SC_PAGE_SIZE")
        except (OSError, ValueError, IndexError):
            continue
        cpu += c
        mem += m
    return {"t": time.time(), "cpu_usec": cpu, "mem": mem}


def _windows_process(pid: int) -> tuple[float, float]:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # LIMITED_QUERY | VM_READ
    if not handle:
        raise OSError(f"can't open process {pid}")
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
            raise OSError("GetProcessTimes failed")

        def ticks(ft: wintypes.FILETIME) -> int:
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime

        cpu_us = (ticks(times[2]) + ticks(times[3])) / 10  # 100 ns units

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
        return cpu_us, float(counters.WorkingSetSize)
    finally:
        kernel32.CloseHandle(handle)


def at(readings: list[dict], t: float, key: str) -> float | None:
    """A cumulative counter's value at time `t`, interpolated between the
    readings around it (readings sorted by their time, `t`)."""
    points = [(r["t"], r[key]) for r in readings if key in r]
    if not points:
        return None
    if t <= points[0][0]:
        return points[0][1]
    for (t1, v1), (t2, v2) in zip(points, points[1:], strict=False):
        if t1 <= t <= t2:
            return v1 if t2 == t1 else v1 + (v2 - v1) * (t - t1) / (t2 - t1)
    return points[-1][1]


def window_cost(readings: list[dict], start: float, end: float) -> dict:
    """What a window [start, end) cost, from cumulative readings: CPU in
    ms, bytes and packets in and out, and the highest memory reading in it."""

    def delta(key: str) -> float | None:
        a, b = at(readings, start, key), at(readings, end, key)
        return None if a is None or b is None else max(0.0, b - a)

    cpu = delta("cpu_usec")
    inside = [r["mem"] for r in readings if start <= r["t"] <= end and "mem" in r]
    packets = [delta("rx_packets"), delta("tx_packets")]
    return {
        "cpu_ms": None if cpu is None else cpu / 1000,
        "mem_peak": max(inside) if inside else None,
        "rx_bytes": delta("rx_bytes"),
        "tx_bytes": delta("tx_bytes"),
        "packets": None if None in packets else sum(packets),
    }


class Sampler:
    """Reads the counters of the PerchTail container (or process) and of
    the source servers every `interval` seconds in the background, so any
    window can be costed afterwards."""

    def __init__(
        self,
        server: str | None,
        server_pid: int | None,
        sources: list[str],
        agent_pids: list[int],
        interval: float = 1.0,
    ):
        self.server, self.server_pid = server, server_pid
        self.sources, self.agent_pids = sources, agent_pids
        self.interval = interval
        self.readings: dict[str, list[dict]] = {"server": [], "sources": [], "agents": []}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _read(self) -> None:
        if self.server:
            self.readings["server"].append(container_counters(self.server))
        elif self.server_pid:
            self.readings["server"].append(process_counters([self.server_pid]))
        if self.sources:
            total: dict[str, float] = {"t": time.time()}
            for name in self.sources:
                for key, value in container_counters(name).items():
                    if key != "t":
                        total[key] = total.get(key, 0.0) + value
            self.readings["sources"].append(total)
        if self.agent_pids:
            self.readings["agents"].append(process_counters(self.agent_pids))

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._read()
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"sampler: {exc}", file=sys.stderr)
            self._stop.wait(max(0.0, self.interval - (time.time() - started)))

    def start(self) -> Sampler:
        self._read()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=30)
        self._read()

    def settle(self) -> None:
        """Waits for one more reading after now, so a window that just ended
        has a reading past its end."""
        time.sleep(self.interval * 1.2)

    def cost(self, start: float, end: float) -> dict:
        out = {"server": window_cost(self.readings["server"], start, end)}
        out["sources"] = window_cost(self.readings["sources"], start, end)
        out["agents"] = window_cost(self.readings["agents"], start, end)
        return out
