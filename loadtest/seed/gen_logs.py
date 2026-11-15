"""Writes the log files a load test serves, and keeps some of them growing.

    python loadtest/seed/gen_logs.py generate --profile tiny
    python loadtest/seed/gen_logs.py grow --profile tiny [--seconds 600]
    python loadtest/seed/gen_logs.py catalog --profile tiny
    python loadtest/seed/gen_logs.py write --profile tiny --phase growing_files

`generate` fills LOADTEST_LOGS_DIR (loadtest/.state/logs by default):

    shared/                 served by every SSH, SMB and local-disk source
      app-01/ .. app-NN/    plain logs of each size in files.sizes_kb
        rotated/            .gz files
        bundle-N.zip        zip archives of a few logs
      large/large-N.log     files.large_mb each (close to MAX_FETCH_MB)
      live/live-N.log       files that grow (for Follow)
      growing/, heavy/,     the files of the growing_files, growing_heavy and
      daily/                daily_log scenarios (seed/growing.py)
    agents/agent-NNN/       one folder per push agent, same layout without large/,
                            heavy/ and daily/ (growing/ in the first few only)

Files already at the right size are left alone, so running it again is
cheap. A marker file, .perchtail-loadtest, tells `seed.py clean --files`
that shared/ and agents/ are the kit's to delete. `--out DIR` writes the
shared tree somewhere else (the SSH or SMB server of a dedicated test
environment); it needs no instance (no LOADTEST_BASE_URL) and writes no
catalog, which the kit's machine writes. `grow` appends to every live/*.log at
files.growing_bytes_per_second and starts a file over (a rotation) once it
passes files.growing_rotate_mb. `catalog` writes loadtest/.state/catalog.json,
the paths the k6 scenarios use, in the instance's state; it depends only on
the profile. `write` is the writer of a phase's growing files (see
seed/growing.py): it starts them afresh and appends to them until
--stop-file appears, recording what it did in --events.

For the `production` profile, `generate` also writes each source's own
tree (shared/prod/<source>/, or the agent's folder; seed/production.py),
and `write` keeps every source's current.log growing at its rate. With
--only-windows-agents and --agents-dir DIR, both work only on the agents
meant for a Windows test server, under DIR: run them there.
"""

from __future__ import annotations

import argparse
import gzip
import os
import random
import shutil
import sys
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import growing as growing_files  # noqa: E402
import production  # noqa: E402
from common import KitError, check_state, fail, load_profile, logs_dir, write_state  # noqa: E402

LEVELS = ["INFO"] * 14 + ["DEBUG"] * 8 + ["WARN"] * 3 + ["ERROR"]
COMPONENTS = [
    "RouterService",
    "CallControl",
    "SessionManager",
    "MediaProxy",
    "QueueWorker",
    "AuthGateway",
    "ReportJob",
    "HttpServer",
    "DbPool",
    "Scheduler",
]
MESSAGES = [
    "request completed status={status} duration_ms={ms}",
    "dispatching job id={hex} queue=q{n} attempt={a}",
    "session {hex} opened for user=u{n} from 10.{a}.{n}.{a}",
    "session {hex} closed after {ms} ms",
    "cache miss key=item:{n} loading from store",
    "slow query took {ms} ms rows={n}",
    "retrying connection to node-{a} in {a}s",
    "connection reset by peer while reading frame {n}",
    "processed batch size={n} lag_ms={ms}",
    "heartbeat ok peers={a} term={n}",
]
BLOCK_BYTES = 1 << 20
MIB = 1024 * 1024
LOGS_MARKER = ".perchtail-loadtest"


def _line(rng: random.Random, when: datetime) -> str:
    message = rng.choice(MESSAGES).format(
        status=rng.choice([200, 200, 200, 201, 204, 302, 404, 500]),
        ms=rng.randint(1, 4000),
        hex=f"{rng.getrandbits(48):012x}",
        n=rng.randint(1, 99999),
        a=rng.randint(1, 9),
    )
    return (
        f"{when.strftime('%Y-%m-%dT%H:%M:%S')}.{when.microsecond // 1000:03d}Z "
        f"{rng.choice(LEVELS):<5} [worker-{rng.randint(1, 16)}] "
        f"{rng.choice(COMPONENTS)} - {message}\n"
    )


def _blocks(seed: int, count: int = 4) -> list[bytes]:
    """A few 1 MiB blocks of varied log lines, reused to build big files
    fast (writing 2 GB line by line would take minutes)."""
    rng = random.Random(seed)
    when = datetime(2026, 1, 1, tzinfo=UTC)
    blocks = []
    for _ in range(count):
        lines, size = [], 0
        while size < BLOCK_BYTES:
            when += timedelta(milliseconds=rng.randint(1, 900))
            line = _line(rng, when)
            lines.append(line)
            size += len(line)
        blocks.append("".join(lines).encode("utf-8"))
    return blocks


def _write_sized(path: Path, size: int, blocks: list[bytes]) -> bool:
    """Writes `size` bytes of log lines; False when it was already there."""
    if path.exists() and path.stat().st_size == size:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        written, i = 0, 0
        while written < size:
            chunk = blocks[i % len(blocks)][: size - written]
            handle.write(chunk)
            written += len(chunk)
            i += 1
    tmp.replace(path)
    return True


# --- layout -----------------------------------------------------------------


def layout(profile: dict, tree: str) -> dict:
    """The files of one tree ("shared" or "agent"), relative to its root,
    as the catalog lists them."""
    files = profile["files"]
    sizes = (
        files["sizes_kb"] if tree == "shared" else files.get("agent_sizes_kb", files["sizes_kb"])
    )
    folders = int(files.get("app_folders", 1)) if tree == "shared" else 1
    per_size = int(files.get("per_size", 1))
    out: dict = {"folders": [], "sized": [], "gz": [], "zip": [], "large": [], "growing": []}
    for f in range(1, folders + 1):
        folder = f"app-{f:02d}"
        out["folders"].append(folder)
        for kb in sizes:
            for n in range(1, per_size + 1):
                out["sized"].append({"path": f"{folder}/service-{kb}k-{n}.log", "size": kb * 1024})
        gz_count = int(files.get("gz", 0))
        if gz_count:
            out["folders"].append(f"{folder}/rotated")
        for n in range(1, gz_count + 1):
            out["gz"].append(
                {
                    "path": f"{folder}/rotated/service.log.{n}.gz",
                    "plain_size": sizes[min(1, len(sizes) - 1)] * 1024,
                }
            )
        for n in range(1, int(files.get("zip", 0)) + 1):
            members = [f"member-{m}.log" for m in range(1, 4)]
            out["zip"].append({"path": f"{folder}/bundle-{n}.zip", "members": members})
    if tree == "shared":
        large = int(files.get("large_count", 0))
        if large:
            out["folders"].append("large")
        for n in range(1, large + 1):
            out["large"].append(
                {"path": f"large/large-{n}.log", "size": int(files["large_mb"]) * 1024 * 1024}
            )
    growing = (
        int(files.get("growing", 0)) if tree == "shared" else min(1, int(files.get("growing", 0)))
    )
    if growing:
        out["folders"].append("live")
    for n in range(1, growing + 1):
        out["growing"].append({"path": f"live/live-{n}.log"})
    grow = growing_files.layout(profile, tree)
    for key, folder in (("growing_files", "growing"), ("heavy", "heavy"), ("daily", "daily")):
        if grow[key]:
            out["folders"].append(folder)
    out.update(grow)
    out["growing_agents"] = growing_agent_count(profile)
    return out


def agent_roots(profile: dict) -> list[Path]:
    agents = profile.get("agents", {})
    total = int(agents.get("running", 0)) + int(agents.get("stopped", 0))
    return [logs_dir() / "agents" / f"agent-{i:03d}" for i in range(1, total + 1)]


def _start_growing(root: Path, spec: dict, profile: dict, with_growing_files: bool) -> int:
    """Creates the growing files missing at their starting size; the writer
    starts them afresh at each phase anyway."""
    starts = {
        "growing_files": int(float(growing_files.setting(profile, "growing_size_mb")) * MIB),
        "heavy": int(float(growing_files.setting(profile, "heavy_start_mb")) * MIB),
        "daily": 0,
    }
    written = 0
    for key, start in starts.items():
        if key == "growing_files" and not with_growing_files:
            continue
        for item in spec[key]:
            path = root / item["path"]
            if not path.exists():
                growing_files.GrowingFile(path, item["name"]).restart(start)
                written += 1
    return written


def production_sources(profile: dict, only_windows: bool = False) -> list[dict]:
    """The sources of a production profile (from seed.py's plan, without
    the API), or none for another profile."""
    if "production" not in profile:
        return []
    from seed import planned_sources  # noqa: PLC0415

    sources = planned_sources(profile)
    if only_windows:
        sources = [s for s in sources if s.get("windows")]
    return sources


def production_trees(profile: dict) -> dict[str, dict]:
    sources = production_sources(profile)
    rates = production.rate_classes(profile, [s["name"] for s in sources])
    return {
        s["name"]: production.tree(profile, s["name"], rates[s["name"]], i)
        for i, s in enumerate(sources)
    }


def _generate_production(args: argparse.Namespace, profile: dict, shared_root: Path) -> int:
    sources = production_sources(profile, args.only_windows_agents)
    trees = production_trees(profile)
    agents_dir = Path(args.agents_dir) if args.agents_dir else logs_dir() / "agents"
    written = 0
    for source in sources:
        if args.only_windows_agents and source["protocol"] != "agent":
            continue
        if args.out and not args.only_windows_agents and source["protocol"] == "agent":
            continue  # a test server's trees: the agents' are on the load machine
        root = production.source_root(source, shared_root, agents_dir)
        written += production.write_tree(root, trees[source["name"]], source["name"])
    return written


def _check_disk(profile: dict) -> None:
    needed = (
        growing_files.disk_needed(profile)
        + production.disk_needed(profile, production_sources(profile))
        if "production" in profile
        else growing_files.disk_needed(profile)
    )
    if not needed:
        return
    root = logs_dir()
    root.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(root).free
    print(f"the growing files need up to {needed / 1024**3:.1f} GiB in {root}")
    if free < needed:
        raise KitError(
            f"{root} has {free / 1024**3:.1f} GiB free; the growing files of this profile need "
            f"up to {needed / 1024**3:.1f} GiB (set LOADTEST_LOGS_DIR to a disk with room)"
        )


def _generate_tree(root: Path, spec: dict, profile: dict, blocks: list[bytes]) -> int:
    written = 0
    for item in spec["sized"] + spec["large"]:
        written += _write_sized(root / item["path"], item["size"], blocks)
    for item in spec["gz"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            plain = b"".join(blocks)[: item["plain_size"]]
            with gzip.open(path, "wb", compresslevel=6) as handle:
                handle.write(plain)
            written += 1
    small = profile["files"]["sizes_kb"][0] * 1024
    for item in spec["zip"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for m, member in enumerate(item["members"]):
                    archive.writestr(member, blocks[m % len(blocks)][: small * (m + 1)])
            written += 1
    for item in spec["growing"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blocks[0][: 64 * 1024])
            written += 1
    return written


def cmd_generate(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    blocks = _blocks(seed=277)
    shared_root = Path(args.out) if args.out else logs_dir() / "shared"
    started = time.time()
    if not args.out:
        marker = logs_dir() / LOGS_MARKER
        if not marker.exists():
            marker.write_text(
                "Written by loadtest/seed/gen_logs.py: `seed.py clean --files` may delete "
                "shared/ and agents/ here.\n",
                encoding="utf-8",
            )
    _check_disk(profile)
    if args.only_windows_agents:
        count = _generate_production(args, profile, shared_root)
        print(f"generated {count} files for the Windows agents under {args.agents_dir}")
        return
    shared_spec = layout(profile, "shared")
    count = _generate_tree(shared_root, shared_spec, profile, blocks)
    count += _start_growing(shared_root, shared_spec, profile, True)
    if not args.out:
        agent_spec = layout(profile, "agent")
        for i, root in enumerate(agent_roots(profile)):
            count += _generate_tree(root, agent_spec, profile, blocks)
            count += _start_growing(root, agent_spec, profile, i < growing_agent_count(profile))
    count += _generate_production(args, profile, shared_root)
    if "matrix" in profile:
        count += growing_files.write_matrix(shared_root, profile)
        if not args.out:
            for root in matrix_agent_roots(profile):
                count += growing_files.write_matrix(root, profile)
    if not args.out:
        cmd_catalog(args, quiet=True)
    print(
        f"generated {count} files in {time.time() - started:.1f}s "
        f"under {shared_root.parent if not args.out else shared_root}"
    )


def cmd_catalog(args: argparse.Namespace, quiet: bool = False) -> None:
    profile = load_profile(args.profile)
    catalog = {
        "profile": profile["name"],
        "shared": layout(profile, "shared"),
        "agent": layout(profile, "agent"),
    }
    if "production" in profile:
        catalog["sources"] = production_trees(profile)
    if "matrix" in profile:
        catalog["matrix"] = growing_files.matrix_layout(profile)
    path = write_state("catalog.json", catalog)
    if not quiet:
        print(f"wrote {path}")


def matrix_agent_roots(profile: dict) -> list[Path]:
    """The agents protocol_matrix measures, which get its files: the first
    agent, and the Windows agents (the rest only connect, for the storm)."""
    from seed import planned_sources  # noqa: PLC0415

    return [
        logs_dir() / "agents" / f"agent-{s['agent_index']:03d}"
        for s in planned_sources(profile)
        if s["protocol"] == "agent"
        and s.get("running")
        and (s["agent_index"] == 1 or s.get("windows"))
    ]


def growing_agent_count(profile: dict) -> int:
    """How many agent trees have growing/: the first running agents."""
    running = int(profile.get("agents", {}).get("running", 0))
    return min(running, int(growing_files.setting(profile, "growing_agents")))


def cmd_write(args: argparse.Namespace) -> None:
    """The writer of one phase's growing files (run.py starts and stops it)."""
    profile = load_profile(args.profile)
    phase = next((p for p in profile["phases"] if p["name"] == args.phase), None)
    if phase is None:
        raise KitError(f"phase {args.phase} isn't in profile {profile['name']}")
    shared_root = Path(args.out) if args.out else logs_dir() / "shared"
    roots = {"shared": shared_root}
    if not args.out:
        for root in agent_roots(profile)[: growing_agent_count(profile)]:
            roots[root.name] = root
        if "protocol_matrix" in phase["scenarios"]:
            for root in matrix_agent_roots(profile):
                roots[root.name] = root
    entries = (
        [] if args.only_windows_agents else growing_files.plan(profile, phase["scenarios"], roots)
    )
    if "production" in phase["scenarios"]:
        agents_dir = Path(args.agents_dir) if args.agents_dir else logs_dir() / "agents"
        sources = production_sources(profile, args.only_windows_agents)
        if args.only_windows_agents:
            sources = [s for s in sources if s["protocol"] == "agent"]
        elif args.out:
            sources = [s for s in sources if s["protocol"] != "agent"]
        elif os.environ.get("LOADTEST_WINDOWS_AGENT_HOSTS"):
            # Written on the Windows test server (write --only-windows-agents).
            sources = [s for s in sources if not s.get("windows")]
        entries += production.writer_entries(profile, sources, shared_root, agents_dir)
    if not entries:
        print(f"phase {args.phase} has no growing files")
        return
    stop = Path(args.stop_file) if args.stop_file else None
    if stop and stop.exists():
        stop.unlink()
    deadline = time.time() + args.seconds if args.seconds else None
    events = growing_files.EventLog(Path(args.events) if args.events else None)
    print(f"writing {len(entries)} growing files for phase {args.phase}", flush=True)
    try:
        growing_files.run_writer(
            entries,
            events,
            lambda: (
                (stop is not None and stop.exists())
                or (deadline is not None and time.time() >= deadline)
            ),
            profile,
        )
    except KeyboardInterrupt:
        events.write("stop", interrupted=True)


def cmd_grow(args: argparse.Namespace) -> None:
    """Appends to every growing file until stopped (or --seconds)."""
    profile = load_profile(args.profile)
    files = profile["files"]
    rate = int(files.get("growing_bytes_per_second", 2048))
    rotate = int(float(files.get("growing_rotate_mb", 0)) * 1024 * 1024)
    shared_root = Path(args.out) if args.out else logs_dir() / "shared"
    targets = [shared_root / g["path"] for g in layout(profile, "shared")["growing"]]
    if not args.out:
        for root in agent_roots(profile):
            targets += [root / g["path"] for g in layout(profile, "agent")["growing"]]
    if not targets:
        print("no growing files in this profile")
        return
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    rng = random.Random()
    tick = 0.5
    per_tick = max(1, int(rate * tick))
    deadline = time.time() + args.seconds if args.seconds else None
    print(f"growing {len(targets)} files at {rate} B/s each", flush=True)
    try:
        while deadline is None or time.time() < deadline:
            now = datetime.now(UTC)
            for path in targets:
                if rotate and path.stat().st_size > rotate:
                    path.write_bytes(b"")
                chunk, size = [], 0
                while size < per_tick:
                    line = _line(rng, now)
                    chunk.append(line)
                    size += len(line)
                with path.open("ab") as handle:
                    handle.write("".join(chunk).encode("utf-8"))
            time.sleep(tick)
    except KeyboardInterrupt:
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "grow", "catalog", "write"):
        p = sub.add_parser(name)
        p.add_argument("--profile", required=True)
        if name != "catalog":
            p.add_argument(
                "--out",
                help="write the shared tree here instead (e.g. on a test server's share)",
            )
        if name in ("grow", "write"):
            p.add_argument(
                "--seconds", type=int, default=0, help="stop after this long (0: until stopped)"
            )
        if name in ("generate", "write"):
            p.add_argument(
                "--only-windows-agents",
                action="store_true",
                help="production: only the agents meant for a Windows test server",
            )
            p.add_argument(
                "--agents-dir", help="where the agents' folders are (default: logs/agents)"
            )
        if name == "write":
            p.add_argument("--phase", required=True)
            p.add_argument("--events", help="record what was written here (JSON lines)")
            p.add_argument("--stop-file", help="stop when this file appears")
    args = parser.parse_args()
    try:
        if args.command not in ("grow", "write") and not (args.command == "generate" and args.out):
            check_state()
        commands = {
            "generate": cmd_generate,
            "grow": cmd_grow,
            "catalog": cmd_catalog,
            "write": cmd_write,
        }
        commands[args.command](args)
    except KitError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
