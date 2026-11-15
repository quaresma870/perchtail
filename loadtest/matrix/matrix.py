"""Measures what each protocol and each feature costs, one thing at a time,
and writes every measurement to measurements.csv with fixed columns (see
measure.py). It only collects and summarises: `report.py costs` turns the
measurements into cost units.

    python loadtest/matrix/matrix.py --profile protocol_matrix-mini --out DIR
    python loadtest/matrix/matrix.py --profile protocol_matrix --out DIR \
        --steps matrix,fixed,features,operation,storms,reliability --netem --allow-restart

run.py runs it for a phase whose scenario is `protocol_matrix`, after
seeding, with the steps of the profile's `matrix.steps`.

Steps:

- matrix: the same operations on every protocol the instance has (SSH,
  SMB, WinRM, WinRM with JEA, push agents by the OS they report, local
  disk): list folders of 100, 1 000 and 10 000 entries, open and download
  files of 1, 10, 50 and 200 MB, follow a file growing at a fixed rate;
  one at a time, then 5 and 20 at once; repeated with each network delay
  of `matrix.delays_ms` when --netem is given (tc netem on the source
  containers, removed afterwards).
- fixed: with nobody signed in, a source check and a file-name index pass
  on one source of each protocol.
- features: the audit page's cost, sign-in with and without MFA, and the
  settings it looks for (redaction, audited listings) when the instance
  has them.
- operation: a backup; with --restore-test, a restore of it into a
  DATA_DIR of its own that the kit makes in the container and records in
  the instance's state (every path the restore writes is set inside it,
  so the live database is never touched; without the option the restore
  is skipped and the measurements say why); and with
  --migration-from REV an Alembic upgrade from REV on a copy of the
  database; with --allow-restart, the time PerchTail takes to start.
- storms (--allow-restart): PerchTail restarted with the push agents
  connected, and how long until they are all back; 30 sign-ins at once.
- reliability (--reliability): a slow source and a source that goes down,
  on a server of their own (LOADTEST_FAULT_CONTAINER, by default the
  compose service lt-ssh-fault), while the other sources are used; and
  how many parallel sessions one source of each protocol takes before it
  fails, with PerchTail's limits raised for the step and put back after.

Each window is costed from counters read every second: PerchTail's CPU,
memory and network (its container's cgroup and interface, or --pid), the
source containers' (--source-container, repeatable), and the kit's agents.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "seed"))
sys.path.insert(0, str(HERE.parent / "collect"))

import measure  # noqa: E402
from collect import AUDIT_COUNT  # noqa: E402
from common import (  # noqa: E402
    RESTORE_STATE,
    ApiError,
    KitError,
    admin_client,
    check_state,
    fail,
    load_profile,
    prefix,
    read_state,
    remove_state,
    restore_cleanup,
    restore_copy_pattern,
    restore_env,
    write_state,
)

MB = 1024 * 1024
DEFAULTS = {
    "lists": [100, 1000, 10000],
    "sizes_mb": [1, 10, 50, 200],
    "parallelism": [1, 5, 20],
    "delays_ms": [0, 1, 20, 80],
    "follow_seconds": 300,
    "follow_rate_kb_s": 256,
    "repeat": {"list": 5, "open": 2, "download": 2},
    "gap_seconds": 3,
    "login_storm": 30,
    "mfa_logins": 3,
    "capacity": [1, 2, 4, 8, 16, 32, 64],
    "steps": ["matrix", "fixed", "features", "operation"],
}


# The label loadtest/compose.sources.yml gives the kit's test servers. The
# matrix changes a container's network (tc netem) or stops and starts it
# only when it carries this label: never PerchTail's container or anything
# else on the same Docker, whatever LOADTEST_SOURCE_CONTAINERS or
# LOADTEST_FAULT_CONTAINER say.
KIT_LABEL = "perchtail.loadtest"
KIT_LABEL_VALUE = "test-source"


def container_label(container: str) -> str | None:
    """The container's KIT_LABEL, or None when it can't be read."""
    try:
        r = subprocess.run(
            [
                "docker",
                "inspect",
                "--type",
                "container",
                "--format",
                f'{{{{ index .Config.Labels "{KIT_LABEL}" }}}}',
                container,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def require_kit_container(container: str) -> None:
    """Refuses a container that isn't one of the kit's test servers."""
    if not container or container.startswith("-") or container_label(container) != KIT_LABEL_VALUE:
        raise KitError(
            f"{container!r} isn't one of the kit's test servers (they carry the label "
            f"{KIT_LABEL}={KIT_LABEL_VALUE}, set in loadtest/compose.sources.yml): refusing to "
            "change its network or stop it. Check LOADTEST_SOURCE_CONTAINERS and "
            "LOADTEST_FAULT_CONTAINER."
        )


RESTORE_SKIPPED = (
    "restore skipped: it runs only with LOADTEST_RESTORE_TEST=1, into a DATA_DIR of its own "
    "that the kit makes in the container"
)


def config(profile: dict) -> dict:
    own = profile.get("matrix", {})
    out = {**DEFAULTS, **own}
    out["repeat"] = {**DEFAULTS["repeat"], **own.get("repeat", {})}
    return out


def label(source: dict, live: dict) -> str:
    """The protocol a source stands for in the matrix."""
    if source["protocol"] == "winrm" and live.get("winrm_configuration"):
        return "winrm-jea"
    if source["protocol"] == "agent":
        return f"agent-{live.get('agent_os') or 'unknown'}"
    return source["protocol"]


class Matrix:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.profile = load_profile(args.profile)
        self.cfg = config(self.profile)
        self.out = measure.CsvOut(Path(args.out))
        self.admin = admin_client()
        users = read_state("users.json") or {}
        seeded = read_state("seeded.json") or {}
        if not users or not seeded:
            raise KitError("nothing seeded: run seed.py apply --profile " + self.profile["name"])
        # Parallel workers sign in as different users: PerchTail limits the
        # transfers one user runs at once (concurrency_user_transfers), and
        # the matrix measures the protocols, not that limit.
        self.users = sorted(users.items())
        self.username, self.password = self.users[0]
        live = {s["id"]: s for s in self.admin.get("/sources")}
        self.sources: dict[str, dict] = {}
        fault_host = os.environ.get("LOADTEST_FAULT_HOST", "lt-ssh-fault")
        self.fault = None
        for s in seeded["sources"]:
            if s["protocol"] == "agent" and not s.get("running"):
                continue
            current = live.get(s["id"])
            if not current:
                continue
            if current["host"] == fault_host:
                self.fault = {**s, **current}
                continue
            self.sources.setdefault(label(s, current), {**s, **current})
        catalog = read_state("catalog.json") or {}
        self.tree = catalog.get("matrix") or {}
        if not self.tree:
            raise KitError(
                "the catalog has no matrix files: run gen_logs.py generate with this profile"
            )
        pids = [int(p) for p in (read_state("agent-pids.json") or {}).values()]
        self.sampler = measure.Sampler(
            args.container if not args.pid else None,
            args.pid,
            args.source_container or [],
            pids,
            interval=1.0,
        )
        self.delay_ms = 0

    # --- helpers -------------------------------------------------------------

    def session(self, index: int = 0) -> measure.Session:
        username, password = (
            (self.username, self.password) if index == 0 else self.users[index % len(self.users)]
        )
        s = measure.Session()
        result = s.login(username, password)
        if result.status != 200:
            raise KitError(f"can't sign in as {username}: {result.status}")
        return s

    def audit_rows(self) -> int | None:
        if not self.args.container:
            return None
        out = subprocess.run(
            ["docker", "exec", self.args.container, "python3", "-c", AUDIT_COUNT, self.args.db],
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout.strip()
        return int(out) if out.isdigit() else None

    def cell(
        self,
        step: str,
        protocol: str,
        operation: str,
        work,
        parallelism: int = 1,
        size: int | None = None,
        entries: int | None = None,
        notes: str = "",
        signed_in: bool = True,
    ) -> dict:
        """Runs `work(session) -> [Timed]` in `parallelism` workers, each
        signed in on its own (or, with signed_in=False, with no session: work
        that signs in itself or needs none), and writes the window's
        measurements."""
        sessions = [self.session(i) if signed_in else None for i in range(parallelism)]
        audit_before = self.audit_rows()
        self.sampler.settle()
        start = time.time()
        with concurrent.futures.ThreadPoolExecutor(parallelism) as pool:
            results = [r for batch in pool.map(work, sessions) for r in batch]
        end = time.time()
        self.sampler.settle()
        audit_after = self.audit_rows()
        cost = self.sampler.cost(start, end)
        ok = [r for r in results if 200 <= r.status < 300 and not r.error]
        total_bytes = sum(r.bytes for r in ok)
        wall = end - start
        server, sources, agents = cost["server"], cost["sources"], cost["agents"]
        source_cost = agents if protocol.startswith("agent") else sources
        row = {
            "step": step,
            "protocol": protocol,
            "operation": operation,
            "size_bytes": size,
            "entries": entries,
            "parallelism": parallelism,
            "delay_ms": self.delay_ms,
            "requests": len(results),
            "errors": len(results) - len(ok),
            "wall_s": wall,
            "latency_p50_ms": measure.percentile([r.total_ms for r in ok], 50),
            "latency_p95_ms": measure.percentile([r.total_ms for r in ok], 95),
            "ttfb_p50_ms": measure.percentile([r.ttfb_ms for r in ok if r.ttfb_ms], 50),
            "ttfb_p95_ms": measure.percentile([r.ttfb_ms for r in ok if r.ttfb_ms], 95),
            "bytes": total_bytes,
            "mb_per_s": total_bytes / MB / wall if wall > 0 else None,
            "server_cpu_ms": server["cpu_ms"],
            "server_mem_peak_bytes": server["mem_peak"],
            "server_net_rx_bytes": server["rx_bytes"],
            "server_net_tx_bytes": server["tx_bytes"],
            "server_net_packets": server["packets"],
            "source_cpu_ms": source_cost["cpu_ms"],
            "source_mem_peak_bytes": source_cost["mem_peak"],
            "source_net_bytes": None
            if source_cost["rx_bytes"] is None
            else (source_cost["rx_bytes"] or 0) + (source_cost["tx_bytes"] or 0),
            "source_net_packets": source_cost["packets"],
            "audit_rows": None
            if audit_before is None or audit_after is None
            else audit_after - audit_before,
            "start": round(start, 3),
            "end": round(end, 3),
            "notes": notes,
        }
        self.out.row(row)
        cell_name = f"{protocol}/{operation}/{size or entries or ''}/{parallelism}/{self.delay_ms}"
        self.out.request_rows(
            [
                {
                    "step": step,
                    "cell": cell_name,
                    "protocol": protocol,
                    "operation": operation,
                    "start": round(r.start, 3),
                    "ttfb_ms": r.ttfb_ms,
                    "total_ms": r.total_ms,
                    "bytes": r.bytes,
                    "status": r.status,
                    "error": r.error,
                }
                for r in results
            ]
        )
        p95 = row["latency_p95_ms"]
        print(
            f"  {step} {protocol} {operation} {size or entries or ''} x{parallelism} "
            f"delay {self.delay_ms} ms: {len(results)} requests, {row['errors']} errors, "
            f"p95 {p95 and round(p95)} ms, "
            f"server CPU {server['cpu_ms'] and round(server['cpu_ms'])} ms",
            flush=True,
        )
        time.sleep(self.cfg["gap_seconds"])
        return row

    # --- operations ------------------------------------------------------------

    def listing(self, source: dict, folder: str, repeat: int):
        def work(s: measure.Session) -> list:
            return [
                s.request("GET", f"/sources/{source['id']}/browse?path={folder}&refresh=true")
                for _ in range(repeat)
            ]

        return work

    def whole(self, source: dict, path: str, route: str, repeat: int):
        def work(s: measure.Session) -> list:
            out = []
            for _ in range(repeat):
                out.append(s.request("GET", f"/sources/{source['id']}/{route}?path={path}"))
                if route == "open":
                    s.request(
                        "POST", f"/sources/{source['id']}/close", {"path": path, "member": None}
                    )
            return out

        return work

    def follow(self, source: dict, path: str, seconds: float):
        tick = float((self.admin.get("/system-settings") or {}).get("tail_interval_seconds", 2))

        def work(s: measure.Session) -> list:
            opened = s.request("GET", f"/sources/{source['id']}/open?path={path}", keep=False)
            out = [opened]
            offset = opened.bytes
            end = time.time() + seconds
            first = True
            while time.time() < end:
                time.sleep(tick)
                for _round in range(8):
                    url = f"/sources/{source['id']}/follow?path={path}&offset={offset}"
                    if first:
                        url += "&start=true"
                        first = False
                    r = s.request("GET", url)
                    out.append(r)
                    if r.status != 200:
                        break
                    if r.headers.get("X-Follow-Reset") == "1":
                        again = s.request("GET", f"/sources/{source['id']}/open?path={path}")
                        out.append(again)
                        offset = again.bytes
                        break
                    offset += r.bytes
                    if r.headers.get("X-Follow-More") != "1":
                        break
            s.request("POST", f"/sources/{source['id']}/close", {"path": path, "member": None})
            return out

        return work

    # --- steps ---------------------------------------------------------------

    def step_matrix(self) -> None:
        # The agents' footprint with nothing to do, to set against their
        # cost while they work (the agent cells' source columns).
        for protocol in sorted(p for p in self.sources if p.startswith("agent")):

            def idle(_s) -> list:
                time.sleep(30)
                return []

            self.cell(
                "matrix",
                protocol,
                "idle",
                idle,
                notes="30 s, no requests: the agents' own cost",
                signed_in=False,
            )
        delays = self.cfg["delays_ms"] if self.args.netem else [0]
        # PerchTail's concurrency limits out of the way: the matrix measures
        # what each protocol costs, not where those limits refuse (the
        # reliability step measures capacity on its own).
        with self.limits_raised(max(self.cfg["parallelism"])):
            for delay in delays:
                self.set_delay(delay)
                try:
                    for protocol, source in sorted(self.sources.items()):
                        print(
                            f"matrix: {protocol} ({source['name']}), delay {delay} ms", flush=True
                        )
                        self.protocol_cells(protocol, source)
                finally:
                    self.set_delay(0)

    def protocol_cells(self, protocol: str, source: dict) -> None:
        repeat = self.cfg["repeat"]
        for parallelism in self.cfg["parallelism"]:
            for entries in self.cfg["lists"]:
                folder = self.tree["lists"].get(str(entries))
                if folder:
                    self.cell(
                        "matrix",
                        protocol,
                        "list",
                        self.listing(source, folder, repeat["list"]),
                        parallelism,
                        entries=entries,
                    )
            for mb in self.cfg["sizes_mb"]:
                path = self.tree["files"].get(str(mb))
                if not path:
                    continue
                for route in ("open", "download"):
                    self.cell(
                        "matrix",
                        protocol,
                        route,
                        self.whole(source, path, route, repeat[route]),
                        parallelism,
                        size=mb * MB,
                    )
            if self.tree.get("follow") and self.cfg["follow_seconds"]:
                self.cell(
                    "matrix",
                    protocol,
                    "follow",
                    self.follow(source, self.tree["follow"], self.cfg["follow_seconds"]),
                    parallelism,
                    notes=f"{self.cfg['follow_seconds']} s at {self.cfg['follow_rate_kb_s']} KB/s",
                )

    def step_fixed(self) -> None:
        print("fixed: source checks and file-name index passes, nobody signed in", flush=True)
        files = sum(int(n) for n in self.tree["lists"]) + len(self.tree["files"]) + 1
        for protocol, source in sorted(self.sources.items()):

            def check(_s, source=source):
                t0 = time.perf_counter()
                start = time.time()
                error = None
                try:
                    # 200 either way: `ok` says whether the source answered.
                    result = self.admin.post(f"/sources/{source['id']}/check")
                    status = 200 if result.get("ok") else 0
                    if not result.get("ok"):
                        error = str(result.get("detail"))[:200]
                except ApiError as exc:
                    status = exc.status
                elapsed = (time.perf_counter() - t0) * 1000
                return [measure.Timed(status, None, elapsed, 0, error=error, start=start)]

            def index(_s, source=source):
                before = self.admin.get(f"/sources/{source['id']}").get("name_indexed_at")
                t0 = time.perf_counter()
                start = time.time()
                try:
                    self.admin.post(f"/sources/{source['id']}/name-index", expect=(202,))
                except ApiError as exc:
                    return [
                        measure.Timed(exc.status, None, 0, 0, error=str(exc.detail), start=start)
                    ]
                deadline = time.time() + 900
                while time.time() < deadline:
                    now = self.admin.get(f"/sources/{source['id']}").get("name_indexed_at")
                    if now and now != before:
                        return [
                            measure.Timed(
                                200, None, (time.perf_counter() - t0) * 1000, 0, start=start
                            )
                        ]
                    time.sleep(0.5)
                return [
                    measure.Timed(
                        0, None, (time.perf_counter() - t0) * 1000, 0, error="timeout", start=start
                    )
                ]

            self.cell("fixed", protocol, "source-check", check, signed_in=False)
            self.cell(
                "fixed",
                protocol,
                "name-index",
                index,
                entries=files,
                notes="files in the source's tree",
                signed_in=False,
            )

    def step_features(self) -> None:
        print("features: audit page, MFA, and the settings this version has", flush=True)
        settings = self.admin.get("/system-settings")
        source = self.sources.get("ssh") or next(iter(self.sources.values()))
        path = self.tree["files"][str(min(int(k) for k in self.tree["files"]))]
        folder = self.tree["lists"][str(min(int(k) for k in self.tree["lists"]))]

        def load(s: measure.Session, with_audit: bool = False) -> list:
            out = []
            for _ in range(10):
                out.append(
                    s.request("GET", f"/sources/{source['id']}/browse?path={folder}&refresh=true")
                )
                out.append(s.request("GET", f"/sources/{source['id']}/open?path={path}"))
                s.request("POST", f"/sources/{source['id']}/close", {"path": path, "member": None})
                if with_audit:
                    out.append(s.request("GET", "/audit?limit=50&offset=0&sort=time&order=desc"))
            return out

        self.cell("features", "ssh", "baseline-load", load, notes="10 x (list + open)")
        self.admin_cell(
            "features",
            "ssh",
            "with-audit-page",
            lambda s: load(s, True),
            "10 x (list + open + audit page)",
        )
        for feature, pattern in (("redaction", "redact"), ("audited-listings", "audit_list")):
            keys = [k for k in settings if pattern in k]
            if not keys:
                self.out.row(
                    {
                        "step": "features",
                        "operation": feature,
                        "requests": 0,
                        "notes": "no such setting in this version",
                    }
                )
                continue
            key = keys[0]
            original = settings[key]
            try:
                for value in (False, True):
                    self.admin.patch("/system-settings", {key: value})
                    self.cell(
                        "features",
                        "ssh",
                        f"{feature}-{'on' if value else 'off'}",
                        load,
                        notes=f"{key}={value}",
                    )
            finally:
                self.admin.patch("/system-settings", {key: original})
        self.mfa_logins()

    def admin_cell(self, step: str, protocol: str, operation: str, work, notes: str) -> None:
        """A cell run as the admin account (the audit page needs view_audit_log)."""
        admin = read_state("admin.json") or {}
        saved = (self.username, self.password)
        self.username, self.password = admin.get("username"), admin.get("password")
        try:
            self.cell(step, protocol, operation, work, notes=notes)
        finally:
            self.username, self.password = saved

    def mfa_logins(self) -> None:
        """Sign-ins with and without MFA (TOTP), as the same user: MFA is
        turned on for the step and off again. One TOTP code per 30 s step
        (PerchTail refuses a code used once), so the MFA sign-ins are 31 s
        apart."""
        count = int(self.cfg["mfa_logins"])

        def plain(_s) -> list:
            return [measure.Session().login(self.username, self.password) for _ in range(count)]

        self.cell("features", "-", "login", plain, notes="without MFA", signed_in=False)
        s = self.session()
        try:
            enrolled = s.json("POST", "/auth/mfa/enroll", {"password": self.password})
            secret = enrolled["secret"]
            s.json("POST", "/auth/mfa/confirm", {"code": measure.totp(secret)})
        except (ApiError, KeyError) as exc:
            self.out.row(
                {
                    "step": "features",
                    "operation": "login-mfa",
                    "requests": 0,
                    "notes": f"MFA not enabled: {exc}",
                }
            )
            return
        try:

            def with_mfa(_s) -> list:
                out = []
                for _ in range(count):
                    time.sleep(31 - time.time() % 30)  # a fresh 30 s step
                    out.append(
                        measure.Session().login(self.username, self.password, measure.totp(secret))
                    )
                return out

            self.cell(
                "features",
                "-",
                "login-mfa",
                with_mfa,
                notes="TOTP, one sign-in per 30 s step",
                signed_in=False,
            )
        finally:
            time.sleep(31 - time.time() % 30)
            again = measure.Session()
            again.login(self.username, self.password, measure.totp(secret))
            again.json("POST", "/auth/mfa/disable", {"password": self.password})

    def docker(
        self, *cmd: str, env: dict | None = None, timeout: float = 3600
    ) -> subprocess.CompletedProcess:
        # PerchTail's command-line tools refuse to run as root: run them as
        # the image's user (LOADTEST_CONTAINER_USER, perchtail by default).
        full = ["docker", "exec", "-u", os.environ.get("LOADTEST_CONTAINER_USER", "perchtail")]
        for key, value in (env or {}).items():
            full += ["-e", f"{key}={value}"]
        return subprocess.run(
            full + [self.args.container, *cmd], capture_output=True, text=True, timeout=timeout
        )

    def timed(
        self, step: str, operation: str, fn, notes: str = "", size: int | None = None
    ) -> None:
        def work(_s) -> list:
            start = time.time()
            t0 = time.perf_counter()
            status, error = fn()
            return [
                measure.Timed(
                    status, None, (time.perf_counter() - t0) * 1000, 0, error=error, start=start
                )
            ]

        self.cell(step, "-", operation, work, size=size, notes=notes, signed_in=False)

    def step_operation(self) -> None:
        if not self.args.container:
            print("operation: needs --container (docker exec); skipped", flush=True)
            return
        print("operation: backup, restore into the kit's copy, migration", flush=True)
        db_size = self.docker("stat", "-c", "%s", self.args.db).stdout.strip()
        size = int(db_size) if db_size.isdigit() else None
        made: dict = {}

        def backup():
            r = self.docker("python", "-m", "app.backup", "create")
            made["path"] = r.stdout.strip().rsplit(" ", 1)[-1] if r.returncode == 0 else None
            return (200 if r.returncode == 0 else 500), (
                None if r.returncode == 0 else r.stderr[-300:]
            )

        self.timed("operation", "backup", backup, notes="python -m app.backup create", size=size)
        if made.get("path") and not self.args.restore_test:
            print(f"operation: {RESTORE_SKIPPED}", flush=True)
            skipped = {"step": "operation", "operation": "restore", "requests": 0}
            self.out.row({**skipped, "notes": RESTORE_SKIPPED})
        elif made.get("path"):
            self.restore_into_copy(made["path"])
        if self.args.migration_from:
            work_dir = "/tmp/lt-migrate"
            url = f"sqlite:///{work_dir}/perchtail.db"
            # DATA_DIR too: Alembic locks it, and the live one is in use.
            migration_env = {"DATABASE_URL": url, "DATA_DIR": work_dir}
            copy = (
                "import sqlite3, sys; src = sqlite3.connect(sys.argv[1]); "
                "dst = sqlite3.connect(sys.argv[2]); src.backup(dst); dst.close()"
            )
            self.docker("sh", "-c", f"rm -rf {work_dir} && mkdir -p {work_dir}")
            self.docker("python3", "-c", copy, self.args.db, f"{work_dir}/perchtail.db")
            down = self.docker(
                "python",
                "-m",
                "alembic",
                "downgrade",
                self.args.migration_from,
                env=migration_env,
            )
            if down.returncode != 0:
                self.out.row(
                    {
                        "step": "operation",
                        "operation": "migration",
                        "requests": 0,
                        "notes": f"downgrade to {self.args.migration_from} failed: "
                        f"{(down.stderr or down.stdout)[-200:]}",
                    }
                )
            else:

                def upgrade():
                    r = self.docker("python", "-m", "alembic", "upgrade", "head", env=migration_env)
                    return (200 if r.returncode == 0 else 500), (
                        None if r.returncode == 0 else r.stderr[-300:]
                    )

                self.timed(
                    "operation",
                    "migration",
                    upgrade,
                    notes=f"alembic upgrade head from {self.args.migration_from}",
                    size=size,
                )
            self.docker("sh", "-c", f"rm -rf {work_dir}")

    def restore_into_copy(self, backup_path: str) -> None:
        """Restores the backup into a DATA_DIR of its own: made with mktemp
        in the container, recorded in the instance's state before anything
        is written there, and removed afterwards."""
        made = self.docker("mktemp", "-d", f"/tmp/{prefix()}-restore-XXXXXX")
        target = made.stdout.strip()
        if made.returncode != 0 or not restore_copy_pattern().match(target):
            raise KitError(f"couldn't make a restore copy in the container: {made.stderr.strip()}")
        write_state(RESTORE_STATE, {"container": self.args.container, "dir": target})
        try:
            env = restore_env(self.args.container, read_state(RESTORE_STATE))
            backup_size = self.docker("stat", "-c", "%s", backup_path).stdout.strip()

            def restore():
                r = self.docker("python", "-m", "app.backup", "restore", backup_path, env=env)
                return (200 if r.returncode == 0 else 500), (
                    None if r.returncode == 0 else r.stderr[-300:]
                )

            self.timed(
                "operation",
                "restore",
                restore,
                notes=f"into the kit's copy {target}; backup {backup_size} bytes",
                size=int(backup_size) if backup_size.isdigit() else None,
            )
        finally:
            gone = self.docker("sh", "-c", restore_cleanup(target))
            if gone.returncode == 0:
                remove_state(RESTORE_STATE)

    def readyz(self) -> bool:
        token = (read_state("monitoring.json") or {}).get("token") or os.environ.get(
            "LOADTEST_MONITORING_TOKEN"
        )
        s = measure.Session()
        r = s.request(
            "GET",
            "/readyz",
            timeout=5,
            extra_headers={"Authorization": f"Bearer {token}"} if token else None,
        )
        return r.status == 200

    def step_storms(self) -> None:
        if not self.args.allow_restart or not self.args.container:
            print("storms: needs --allow-restart and --container; skipped", flush=True)
            return
        agents = [
            s
            for s in (read_state("seeded.json") or {}).get("sources", [])
            if s["protocol"] == "agent" and s.get("running")
        ]
        print(
            f"storms: restart with {len(agents)} agents connected; "
            f"{self.cfg['login_storm']} sign-ins at once",
            flush=True,
        )
        times: dict = {}

        def restart(_s) -> list:
            start = time.time()
            t0 = time.perf_counter()
            subprocess.run(
                ["docker", "restart", self.args.container], capture_output=True, timeout=300
            )
            while not self.readyz():
                time.sleep(0.5)
            times["ready"] = (time.perf_counter() - t0) * 1000
            names = {a["name"] for a in agents}
            deadline = time.time() + 600
            while time.time() < deadline:
                try:
                    connected = {
                        s["name"]
                        for s in admin_client().get("/sources")
                        if s.get("agent_connected")
                    }
                except (ApiError, KitError, OSError):
                    connected = set()
                if names <= connected:
                    break
                time.sleep(1)
            status = 200 if names <= connected else 0
            return [
                measure.Timed(
                    status, times["ready"], (time.perf_counter() - t0) * 1000, 0, start=start
                )
            ]

        self.cell(
            "storms",
            "agent",
            "restart-reconnect",
            restart,
            entries=len(agents),
            notes="ttfb = until /readyz, total = until every agent is back",
            signed_in=False,
        )
        self.admin = admin_client()
        storm = int(self.cfg["login_storm"])

        def login(_s) -> list:
            return [measure.Session().login(self.username, self.password)]

        self.cell(
            "storms",
            "-",
            "login-storm",
            login,
            parallelism=storm,
            signed_in=False,
            notes=f"{storm} sign-ins at once",
        )

    def step_reliability(self) -> None:
        if not self.args.reliability:
            print("reliability: needs --reliability; skipped", flush=True)
            return
        container = os.environ.get("LOADTEST_FAULT_CONTAINER", "perchtail-lt-ssh-fault-1")
        if not self.fault:
            print("reliability: no source on the fault server; skipped", flush=True)
            return
        # Slowed down, stopped and started: only the kit's own fault server.
        require_kit_container(container)
        others = [s for p, s in self.sources.items() if p != "local"]
        path = self.tree["files"][str(min(int(k) for k in self.tree["files"]))]
        fault = self.fault

        def mixed(seconds: float):
            def work(s: measure.Session) -> list:
                out, end = [], time.time() + seconds
                while time.time() < end:
                    for source in others + [fault]:
                        out.append(
                            s.request(
                                "GET", f"/sources/{source['id']}/open?path={path}", timeout=120
                            )
                        )
                        s.request(
                            "POST", f"/sources/{source['id']}/close", {"path": path, "member": None}
                        )
                return out

            return work

        window = 60
        self.cell(
            "reliability",
            "ssh",
            "baseline",
            mixed(window),
            2,
            notes="other sources and the fault source",
        )
        self.netem(container, 2000)
        try:
            self.cell(
                "reliability",
                "ssh",
                "slow-source",
                mixed(window),
                2,
                notes="2000 ms added on the fault source",
            )
        finally:
            self.netem(container, 0)
        require_kit_container(container)
        subprocess.run(["docker", "stop", container], capture_output=True, timeout=120)
        try:
            self.cell(
                "reliability", "ssh", "source-down", mixed(window), 2, notes=self.status_note(fault)
            )
        finally:
            # The container checked just before it was stopped.
            subprocess.run(["docker", "start", container], capture_output=True, timeout=120)

        def recovery(_s) -> list:
            start, t0 = time.time(), time.perf_counter()
            deadline = time.time() + 300
            while time.time() < deadline:
                try:
                    if self.admin.post(f"/sources/{fault['id']}/check").get("ok"):
                        return [
                            measure.Timed(
                                200, None, (time.perf_counter() - t0) * 1000, 0, start=start
                            )
                        ]
                except ApiError:
                    pass
                time.sleep(2)
            return [
                measure.Timed(
                    0, None, (time.perf_counter() - t0) * 1000, 0, error="not back", start=start
                )
            ]

        self.cell(
            "reliability",
            "ssh",
            "recovery",
            recovery,
            notes="until a source check passes again",
            signed_in=False,
        )
        self.capacity()

    def status_note(self, source: dict) -> str:
        try:
            live = self.admin.get(f"/sources/{source['id']}")
            reason = live.get("status_reason") or live.get("status_message") or ""
            return f"status {live.get('status')}: {reason}"
        except ApiError:
            return ""

    @contextlib.contextmanager
    def limits_raised(self, top: int):
        """PerchTail's three concurrency limits raised above `top` for a
        while, and put back afterwards."""
        keys = ("concurrency_global", "concurrency_per_source", "concurrency_user_transfers")
        current = self.admin.get("/system-settings")
        original = {k: current[k] for k in keys if k in current}
        # The highest values PerchTail accepts (api/system_settings.py).
        ceiling = {
            "concurrency_global": 256,
            "concurrency_per_source": 64,
            "concurrency_user_transfers": 32,
        }
        self.admin.patch(
            "/system-settings", {k: min(ceiling[k], max(top * 2, 64)) for k in original}
        )
        try:
            yield
        finally:
            self.admin.patch("/system-settings", original)

    def capacity(self) -> None:
        """Parallel opens on one source of each protocol, raising the number
        until more than 5 % fail, with PerchTail's limits out of the way."""
        with self.limits_raised(max(self.cfg["capacity"])):
            path = self.tree["files"][str(min(int(k) for k in self.tree["files"]))]
            for protocol, source in sorted(self.sources.items()):
                for level in self.cfg["capacity"]:
                    row = self.cell(
                        "reliability",
                        protocol,
                        "sessions-capacity",
                        self.whole(source, path, "open", 2),
                        level,
                    )
                    if row["requests"] and row["errors"] / row["requests"] > 0.05:
                        break

    # --- network delay ---------------------------------------------------------

    def netem(self, container: str, delay: int) -> None:
        """Adds `delay` ms to everything a container sends (tc netem; the
        test servers' images have iproute2), or removes it with 0. Only in
        one of the kit's test servers (require_kit_container)."""
        require_kit_container(container)
        subprocess.run(
            [
                "docker",
                "exec",
                "--privileged",
                container,
                "tc",
                "qdisc",
                "del",
                "dev",
                "eth0",
                "root",
            ],
            capture_output=True,
        )
        if delay:
            r = subprocess.run(
                [
                    "docker",
                    "exec",
                    "--privileged",
                    container,
                    "tc",
                    "qdisc",
                    "add",
                    "dev",
                    "eth0",
                    "root",
                    "netem",
                    "delay",
                    f"{delay}ms",
                ],
                capture_output=True,
                text=True,
            )
            if r.returncode != 0:
                raise KitError(f"tc netem on {container} failed: {r.stderr.strip()}")

    def set_delay(self, delay: int) -> None:
        """The added delay on every source container; only with --netem, and
        only on the kit's own test servers (all checked before any is
        changed). Without --netem, nothing is run in them."""
        if self.args.netem:
            containers = self.args.source_container or []
            for container in containers:
                require_kit_container(container)
            for container in containers:
                self.netem(container, delay)
        self.delay_ms = delay

    def run(self) -> None:
        steps = self.args.steps.split(",") if self.args.steps else self.cfg["steps"]
        print(
            "protocols: "
            + ", ".join(f"{p} ({s['name']})" for p, s in sorted(self.sources.items())),
            flush=True,
        )
        self.sampler.start()
        try:
            for step in steps:
                getattr(self, f"step_{step}")()
        finally:
            self.sampler.stop()
            (Path(self.args.out) / "counters.json").write_text(
                json.dumps(self.sampler.readings), encoding="utf-8"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--steps", help="comma-separated (default: the profile's matrix.steps)")
    parser.add_argument(
        "--container", default=os.environ.get("LOADTEST_CONTAINER", "perchtail-perchtail-1")
    )
    parser.add_argument(
        "--pid", type=int, help="a PerchTail process on this machine instead of a container"
    )
    parser.add_argument("--db", default="/data/db/perchtail.db")
    parser.add_argument(
        "--source-container", action="append", help="a source server's container (repeatable)"
    )
    parser.add_argument(
        "--netem", action="store_true", help="repeat the matrix with matrix.delays_ms (tc netem)"
    )
    parser.add_argument(
        "--allow-restart", action="store_true", help="restart PerchTail for the storms and start-up"
    )
    parser.add_argument(
        "--reliability", action="store_true", help="the slow and failing source, and capacity"
    )
    parser.add_argument(
        "--migration-from", help="time an Alembic upgrade from this revision on a copy"
    )
    parser.add_argument(
        "--restore-test",
        action="store_true",
        help="time a restore of the backup, into a DATA_DIR the kit makes for it",
    )
    args = parser.parse_args()
    try:
        check_state()
        Matrix(args).run()
    except (KitError, ApiError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
