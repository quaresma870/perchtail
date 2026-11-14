"""Runs a load-test profile end to end, without intervention:

    python loadtest/run.py --profile tiny

1. seeds the instance (seed.py apply), writes the log files (gen_logs.py
   generate), builds and starts the push agents (agents.py), and waits for
   the file-name index (seed.py index);
2. keeps the growing files growing (gen_logs.py grow) for the whole run;
3. runs the profile's phases one after another: applies the phase's System
   settings, starts the collector, runs k6 on k6/profile.js for that phase,
   stops the collector;
4. puts the profile's settings back, stops the agents and the growing
   files, and writes the summary (report/report.py).

Results go to loadtest/results/<date>-<profile>/ (ignored by git), one
folder per phase. `--clean` removes everything seeded afterwards. The state
used is the instance's own (loadtest/.state/instances/<host>-<port>/); state
written for another URL is refused, `--skip-seed` included.

k6 runs from the PATH when its version is the pinned one (K6_VERSION),
otherwise in Docker (K6_IMAGE); `--k6 native|docker` forces either. In
Docker, a localhost URL is reached as host.docker.internal.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "seed"))

from common import (  # noqa: E402
    REPO_DIR,
    SETTINGS_STATE,
    ApiError,
    KitError,
    admin_client,
    allowed_hosts,
    base_url,
    change_settings,
    check_state,
    fail,
    load_profile,
    normalized_url,
    prefix,
    read_state,
    results_root,
    state_dir,
    state_root,
)

K6_VERSION = "1.4.2"
K6_DIGEST = "sha256:3656673de3f30424e8ebcfa46acd9558d83b6a43612d0f668ffeac953950c6c7"
K6_IMAGE = f"grafana/k6:{K6_VERSION}@{K6_DIGEST}"
PY = sys.executable
PAUSE_BETWEEN_PHASES = 15


def step(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def run_py(*args: str) -> None:
    subprocess.run([PY, *args], check=True)


def git_commit() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_DIR, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


# --- k6 -------------------------------------------------------------------------


def k6_mode(requested: str) -> str:
    if requested != "auto":
        return requested
    if shutil.which("k6"):
        out = subprocess.run(["k6", "version"], capture_output=True, text=True).stdout
        found = re.search(r"\bk6 v(\d+\.\d+\.\d+)\b", out)
        if found and found.group(1) == K6_VERSION:
            return "native"
        step(f"k6 on PATH isn't v{K6_VERSION}; using {K6_IMAGE.split('@')[0]} in Docker")
    return "docker"


def k6_command(
    mode: str, url: str, profile_path: Path, phase: str, out_dir: Path
) -> tuple[list[str], dict]:
    env = {
        "LOADTEST_BASE_URL": url,
        "LOADTEST_ALLOWED_HOSTS": ",".join(sorted(allowed_hosts())),
        "LOADTEST_PHASE": phase,
        "LOADTEST_STATE_URL": normalized_url(url),
    }
    if mode == "native":
        env.update(
            LOADTEST_PROFILE=str(profile_path),
            LOADTEST_STATE_DIR=str(state_dir()),
            LOADTEST_RESULTS_DIR=str(out_dir),
        )
        return ["k6", "run", "--quiet", str(HERE / "k6" / "profile.js")], env

    parts = urllib.parse.urlsplit(url)
    if parts.hostname in ("localhost", "127.0.0.1", "::1"):
        # The instance is on this machine: from inside the k6 container
        # that's host.docker.internal, a name for the same allowed host.
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        env["LOADTEST_BASE_URL"] = urllib.parse.urlunsplit(parts._replace(netloc=netloc))
        env["LOADTEST_ALLOWED_HOSTS"] += ",host.docker.internal"
    try:
        profile_in_container = "/loadtest/" + profile_path.resolve().relative_to(HERE).as_posix()
        extra_mount: list[str] = []
    except ValueError:
        profile_in_container = "/profile/" + profile_path.name
        extra_mount = ["-v", f"{profile_path.resolve().parent}:/profile:ro"]
    env.update(
        LOADTEST_PROFILE=profile_in_container,
        LOADTEST_STATE_DIR="/state",
        LOADTEST_RESULTS_DIR="/results",
    )
    cmd = [
        "docker",
        "run",
        "--rm",
        "--add-host",
        "host.docker.internal:host-gateway",
        "-v",
        f"{HERE}:/loadtest:ro",
        "-v",
        f"{state_dir()}:/state:ro",
        "-v",
        f"{out_dir}:/results",
        *extra_mount,
    ]
    for key in env:
        cmd += ["-e", key]
    cmd += [K6_IMAGE, "run", "--quiet", "/loadtest/k6/profile.js"]
    return cmd, env


# --- phases -------------------------------------------------------------------


def phase_settings(profile: dict, phase: dict) -> dict:
    """The profile's settings, the phase's own, and for any key another
    phase changes but this one doesn't set, the instance's original value:
    a phase never inherits the one before it."""
    settings = {**profile.get("settings", {}), **phase.get("settings", {})}
    original = read_state(SETTINGS_STATE, {})
    for other in profile["phases"]:
        for key in other.get("settings", {}):
            if key not in settings and key in original:
                settings[key] = original[key]
    return settings


def apply_settings(settings: dict) -> None:
    change_settings(admin_client(), settings)


def start_collector(args: argparse.Namespace, out_dir: Path) -> subprocess.Popen:
    cmd = [
        PY,
        str(HERE / "collect" / "collect.py"),
        "--out",
        str(out_dir),
        "--interval",
        str(args.interval),
    ]
    if args.pid:
        cmd += ["--pid", str(args.pid), "--scratch-dir", args.scratch_dir, "--db", args.db]
    else:
        cmd += ["--container", args.container]
    log = (out_dir / "collect.log").open("ab")
    return subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)


def stop_collector(proc: subprocess.Popen, out_dir: Path) -> None:
    (out_dir / "collect.stop").touch()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()


def run_phase(args, profile: dict, phase: dict, run_dir: Path, mode: str) -> dict:
    out_dir = run_dir / phase["name"]
    out_dir.mkdir(parents=True, exist_ok=True)
    settings = phase_settings(profile, phase)
    apply_settings(settings)
    step(f"phase {phase['name']}: scenarios {', '.join(phase['scenarios'])}")
    collector = start_collector(args, out_dir)
    time.sleep(args.interval)  # one sample before the load starts
    cmd, env = k6_command(mode, base_url(), Path(profile["_path"]), phase["name"], out_dir)
    started = time.time()
    with (out_dir / "k6.log").open("w", encoding="utf-8") as log:
        proc = subprocess.run(
            cmd,
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log.write(proc.stdout)
    print(
        proc.stdout.strip().split("\n\nphase", 1)[-1]
        if "phase " in proc.stdout
        else proc.stdout[-2000:]
    )
    time.sleep(args.interval)  # and one after
    stop_collector(collector, out_dir)
    result = {
        "name": phase["name"],
        "scenarios": phase["scenarios"],
        "settings": settings,
        "k6_exit_code": proc.returncode,
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "seconds": round(time.time() - started, 1),
    }
    if proc.returncode not in (0, 99):
        step(f"phase {phase['name']}: k6 exited with {proc.returncode} (see {out_dir / 'k6.log'})")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--profile", required=True)
    parser.add_argument("--phases", help="comma-separated subset of the profile's phases")
    parser.add_argument(
        "--container", default=os.environ.get("LOADTEST_CONTAINER", "perchtail-perchtail-1")
    )
    parser.add_argument(
        "--pid", type=int, help="measure a PerchTail process on this machine instead"
    )
    parser.add_argument("--scratch-dir", default="/data/scratch", help="with --pid: SCRATCH_DIR")
    parser.add_argument("--db", default="/data/db/perchtail.db", help="with --pid: the SQLite file")
    parser.add_argument("--interval", type=float, default=5.0, help="collector interval in seconds")
    parser.add_argument("--k6", choices=["auto", "native", "docker"], default="auto")
    parser.add_argument("--skip-seed", action="store_true", help="the instance is already seeded")
    parser.add_argument("--clean", action="store_true", help="remove what was seeded afterwards")
    args = parser.parse_args()

    try:
        url = base_url()
        check_state()
        profile = load_profile(args.profile)
        phases = profile["phases"]
        if args.phases:
            wanted = args.phases.split(",")
            phases = [p for p in phases if p["name"] in wanted]
            if len(phases) != len(wanted):
                raise KitError(f"unknown phase in {args.phases}")
        run_dir = results_root() / f"{time.strftime('%Y%m%d-%H%M%S')}-{profile['name']}"
        run_dir.mkdir(parents=True)
        run_started = time.time()
        step(
            f"profile {profile['name']} against {urllib.parse.urlsplit(url).netloc}; "
            f"results in {run_dir}"
        )

        seed = str(HERE / "seed" / "seed.py")
        gen = str(HERE / "seed" / "gen_logs.py")
        agents = str(HERE / "seed" / "agents.py")
        seed_started = time.time()
        if not args.skip_seed:
            step("seeding")
            run_py(seed, "apply", "--profile", args.profile)
            run_py(gen, "generate", "--profile", args.profile)
        else:
            seeded = read_state("seeded.json")
            if (
                not seeded
                or seeded["profile"] != profile["name"]
                or seeded.get("prefix", "lt") != prefix()
            ):
                raise KitError(
                    f"--skip-seed: this instance isn't seeded for profile {profile['name']} "
                    f"with the prefix {prefix()!r}"
                )
            run_py(gen, "catalog", "--profile", args.profile)
        if profile.get("agents", {}).get("running"):
            if not (state_root() / "bin").exists() or not any((state_root() / "bin").iterdir()):
                run_py(agents, "build")
            run_py(agents, "start")
        if profile.get("settings", {}).get("name_index_enabled", True):
            run_py(seed, "index", "--timeout", "300")
        seed_seconds = round(time.time() - seed_started, 1)

        grower = subprocess.Popen(
            [PY, gen, "grow", "--profile", args.profile],
            stdout=(run_dir / "grow.log").open("ab"),
            stderr=subprocess.STDOUT,
        )
        mode = k6_mode(args.k6)
        results = []
        try:
            for i, phase in enumerate(phases):
                if i:
                    time.sleep(PAUSE_BETWEEN_PHASES)
                results.append(run_phase(args, profile, phase, run_dir, mode))
        finally:
            grower.terminate()
            apply_settings(phase_settings(profile, {}))
            if profile.get("agents", {}).get("running"):
                run_py(agents, "stop")

        run_info = {
            "profile": profile["name"],
            "profile_file": profile["_path"],
            "target": urllib.parse.urlsplit(url).netloc,
            "state": str(state_dir()),
            "git_commit": git_commit(),
            "k6": {"mode": mode, "version": K6_VERSION},
            "seed_seconds": seed_seconds,
            "total_seconds": round(time.time() - run_started, 1),
            "phases": results,
        }
        (run_dir / "run.json").write_text(json.dumps(run_info, indent=2), encoding="utf-8")
        run_py(str(HERE / "report" / "report.py"), str(run_dir))
        if args.clean:
            step("cleaning")
            run_py(seed, "clean")
        step(f"done in {run_info['total_seconds']:.0f}s; summary in {run_dir / 'summary.md'}")
    except (KitError, ApiError) as exc:
        fail(str(exc))
    except subprocess.CalledProcessError as exc:
        fail(f"{' '.join(map(str, exc.cmd[:3]))} failed with exit code {exc.returncode}")


if __name__ == "__main__":
    main()
