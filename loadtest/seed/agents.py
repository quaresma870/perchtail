"""Builds the push agent from agent/ and runs one process per running agent
source of the seeded profile, each with its own token and log folder.

    python loadtest/seed/agents.py build
    python loadtest/seed/agents.py start
    python loadtest/seed/agents.py status
    python loadtest/seed/agents.py stop
    python loadtest/seed/agents.py windows-bundle

`build` uses the local Go toolchain when `go` is on PATH, otherwise the
golang image in Docker (LOADTEST_GO_IMAGE), and writes the binary to bin/
in the state directory. `start` reads the sources and tokens `seed.py apply`
saved for the instance, and starts the agents marked running in the profile;
the ones marked stopped keep their source and token but no process, as an
agent that is installed but down. Each agent gets only the variables it
needs (its own settings, and the system's PATH, temp and proxy variables),
and its output goes to agents/<prefix>-agent-NNN.log in the instance's state.
`stop` only stops a saved PID whose executable is still the kit's agent
binary.

The agents dial LOADTEST_AGENT_URL, by default the ws(s):// form of
LOADTEST_BASE_URL plus /agent/connect. Plain ws:// is allowed only for a
host in LOADTEST_ALLOWED_HOSTS (PERCHTAIL_ALLOW_INSECURE is set for them).

The production profile marks some agents as Windows agents. When
LOADTEST_WINDOWS_AGENT_HOSTS names a Windows test server, `start` leaves
them out, and `windows-bundle` writes, in the instance's state, what runs
them there: the Windows build of the agent, a PowerShell script that starts
one process per agent (each with its own token and folder under
LOADTEST_WINDOWS_AGENT_ROOT, C:\\perchtail-loadtest\\agents by default) and one
that stops them. The kit runs nothing on that server itself. Without a
Windows test server they run here like the others, and the summary says so.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (  # noqa: E402
    REPO_DIR,
    KitError,
    base_url,
    check_allowed,
    check_state,
    fail,
    logs_dir,
    read_state,
    state_dir,
    state_root,
    write_state,
)

GO_IMAGE = os.environ.get(
    "LOADTEST_GO_IMAGE",
    "golang:1.27@sha256:ec47ea3725fea0cc88f6de94919aef19f40f12fc1ec0e2d1b3426dbf15ecca04",
)
EXE = ".exe" if os.name == "nt" else ""

# What an agent process inherits besides its own PERCHTAIL_* settings: what
# the OS and Go's runtime need (PATH, temp, home, Windows' system folders),
# and the proxy and CA variables Go reads to dial the server. Nothing else:
# not the admin's or the sources' passwords the kit's own environment holds.
PASS_THROUGH_ENV = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LOCALAPPDATA",
    "APPDATA",
    "LANG",
    "LC_ALL",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)


def binary_path() -> Path:
    return state_root() / "bin" / f"perchtail-agent{EXE}"


def windows_remote() -> bool:
    """True when the Windows agents run on a Windows test server."""
    return bool(os.environ.get("LOADTEST_WINDOWS_AGENT_HOSTS"))


def agent_env(url: str, token: str, root: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in PASS_THROUGH_ENV if key in os.environ}
    env.update(
        PERCHTAIL_SERVER_URL=url,
        PERCHTAIL_AGENT_TOKEN=token,
        PERCHTAIL_BASE_PATH=str(root),
        PERCHTAIL_REMOTE_UPGRADE="false",
    )
    if url.startswith("ws://"):
        env["PERCHTAIL_ALLOW_INSECURE"] = "true"
    return env


def _goarch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        machine, machine
    )


def cmd_build(_args: argparse.Namespace) -> None:
    out = binary_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    agent_dir = REPO_DIR / "agent"
    if shutil.which("go"):
        subprocess.run(["go", "build", "-o", str(out), "."], cwd=agent_dir, check=True)
    else:
        goos = "windows" if os.name == "nt" else "linux"
        subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{agent_dir}:/src:ro",
                "-v",
                f"{out.parent}:/out",
                "-e",
                f"GOOS={goos}",
                "-e",
                f"GOARCH={_goarch()}",
                "-e",
                "CGO_ENABLED=0",
                "-w",
                "/src",
                GO_IMAGE,
                "go",
                "build",
                "-buildvcs=false",
                "-o",
                f"/out/{out.name}",
                ".",
            ],
            check=True,
        )
    print(f"built {out}")


def agent_url() -> str:
    url = os.environ.get("LOADTEST_AGENT_URL")
    if url:
        check_allowed(url.replace("ws://", "http://", 1).replace("wss://", "https://", 1))
        return url
    parts = urllib.parse.urlsplit(base_url())
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urllib.parse.urlunsplit(
        (scheme, parts.netloc, parts.path.rstrip("/") + "/agent/connect", "", "")
    )


def _pids() -> dict[str, int]:
    return read_state("agent-pids.json", {})


def _executable(pid: int) -> str | None:
    """The full path of the program process `pid` runs, or None when no
    such process is running (or it can't be read)."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            if (
                kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value != 259
            ):  # STILL_ACTIVE
                return None
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return None
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    proc_exe = Path(f"/proc/{pid}/exe")
    if Path("/proc/self/exe").exists():
        try:
            return os.readlink(proc_exe)
        except OSError:
            return None
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "comm="], capture_output=True, text=True
    ).stdout.strip()
    return out or None


def _is_kit_agent(pid: int) -> bool:
    """True when `pid` runs the kit's agent binary: a saved PID can belong
    to another program once the agent has exited."""
    exe = _executable(pid)
    if not exe:
        return False
    expected = str(binary_path().resolve())
    try:
        actual = str(Path(exe).resolve())
    except OSError:
        actual = exe
    if os.name == "nt":
        return os.path.normcase(actual) == os.path.normcase(expected)
    return actual == expected


def _alive(pid: int) -> bool:
    return _executable(pid) is not None


def cmd_start(_args: argparse.Namespace) -> None:
    seeded = read_state("seeded.json")
    tokens = read_state("agent-tokens.json", {})
    if not seeded:
        fail("nothing seeded yet: run seed.py apply first")
    binary = binary_path()
    if not binary.exists():
        fail(f"{binary} not found: run agents.py build first")
    url = agent_url()
    log_dir = state_dir() / "agents"
    log_dir.mkdir(exist_ok=True)
    pids = {name: pid for name, pid in _pids().items() if _is_kit_agent(pid)}
    started = 0
    for source in seeded["sources"]:
        if source["protocol"] != "agent" or not source.get("running"):
            continue
        if source.get("windows") and windows_remote():
            continue  # started on the Windows test server (windows-bundle)
        name = source["name"]
        if name in pids:
            continue
        token = tokens.get(name)
        if not token:
            fail(f"no token saved for {name}: run seed.py apply again")
        root = logs_dir() / "agents" / f"agent-{source['agent_index']:03d}"
        root.mkdir(parents=True, exist_ok=True)
        env = agent_env(url, token, root)
        log = (log_dir / f"{name}.log").open("ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        proc = subprocess.Popen(
            [str(binary)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        pids[name] = proc.pid
        started += 1
    write_state("agent-pids.json", pids)
    print(f"agents running: {len(pids)} ({started} started now)")


def cmd_stop(_args: argparse.Namespace) -> None:
    pids = _pids()
    stopping = []
    for name, pid in pids.items():
        if not _alive(pid):
            continue
        if not _is_kit_agent(pid):
            print(
                f"warning: PID {pid} saved for {name} isn't the kit's agent any more; left alone",
                file=sys.stderr,
            )
            continue
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        stopping.append(pid)
    deadline = time.time() + 10
    while time.time() < deadline and any(_is_kit_agent(p) for p in stopping):
        time.sleep(0.5)
    write_state("agent-pids.json", {})
    print(f"stopped {len(stopping)} agents")


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def cmd_windows_bundle(_args: argparse.Namespace) -> None:
    """Writes windows-agents/ in the instance's state: the agent built for
    Windows, start-agents.ps1 and stop-agents.ps1. The start script holds
    the agents' tokens: copy the folder to the Windows test server over a
    channel you trust, and delete it there after the run."""
    seeded = read_state("seeded.json")
    tokens = read_state("agent-tokens.json", {})
    if not seeded:
        fail("nothing seeded yet: run seed.py apply first")
    agents = [s for s in seeded["sources"] if s.get("windows") and s.get("running")]
    if not agents:
        fail("the seeded profile has no Windows agents")
    out = state_dir() / "windows-agents"
    out.mkdir(exist_ok=True)
    agent_dir = REPO_DIR / "agent"
    target = out / "perchtail-agent.exe"
    env = {"GOOS": "windows", "GOARCH": "amd64", "CGO_ENABLED": "0"}
    if shutil.which("go"):
        subprocess.run(
            ["go", "build", "-o", str(target), "."],
            cwd=agent_dir,
            check=True,
            env={**os.environ, **env},
        )
    else:
        cmd = ["docker", "run", "--rm", "-v", f"{agent_dir}:/src:ro", "-v", f"{out}:/out"]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["-w", "/src", GO_IMAGE, "go", "build", "-o", "/out/perchtail-agent.exe", "."]
        subprocess.run(cmd, check=True)
    url = agent_url()
    root = os.environ.get("LOADTEST_WINDOWS_AGENT_ROOT", r"C:\perchtail-loadtest\agents")
    lines = [
        "# Starts the load test's Windows agents (written by loadtest/seed/agents.py).",
        "# Holds the agents' enrollment tokens: delete this folder after the run.",
        '$ErrorActionPreference = "Stop"',
        "$exe = Join-Path $PSScriptRoot 'perchtail-agent.exe'",
        "$pids = @()",
    ]
    for source in agents:
        token = tokens.get(source["name"])
        if not token:
            fail(f"no token saved for {source['name']}: run seed.py apply again")
        folder = f"{root}\\agent-{source['agent_index']:03d}"
        lines += [
            f"New-Item -ItemType Directory -Force {_ps_quote(folder)} | Out-Null",
            f"$env:PERCHTAIL_SERVER_URL = {_ps_quote(url)}",
            f"$env:PERCHTAIL_AGENT_TOKEN = {_ps_quote(token)}",
            f"$env:PERCHTAIL_BASE_PATH = {_ps_quote(folder)}",
            '$env:PERCHTAIL_REMOTE_UPGRADE = "false"',
        ]
        if url.startswith("ws://"):
            lines.append('$env:PERCHTAIL_ALLOW_INSECURE = "true"')
        log = f"{root}\\{source['name']}.log"
        lines.append(
            "$p = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden "
            f"-RedirectStandardOutput {_ps_quote(log)} "
            f"-RedirectStandardError {_ps_quote(log + '.err')}"
        )
        lines.append("$pids += $p.Id")
    lines += [
        "Remove-Item Env:PERCHTAIL_AGENT_TOKEN",
        "$pids | Set-Content (Join-Path $PSScriptRoot 'agent-pids.txt')",
        'Write-Output "started $($pids.Count) agents"',
    ]
    start = out / "start-agents.ps1"
    start.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    try:
        start.chmod(0o600)
    except OSError:
        pass
    (out / "stop-agents.ps1").write_text(
        "# Stops the agents start-agents.ps1 started.\r\n"
        "$file = Join-Path $PSScriptRoot 'agent-pids.txt'\r\n"
        "Get-Content $file | ForEach-Object {\r\n"
        "  Stop-Process -Id $_ -ErrorAction SilentlyContinue\r\n"
        "}\r\n"
        "Remove-Item $file\r\n",
        encoding="utf-8",
    )
    print(
        f"wrote {out}: copy it to the Windows test server, generate the agents' folders there "
        f"(gen_logs.py generate --only-windows-agents --agents-dir {root}) and run "
        "start-agents.ps1; it holds the agents' tokens"
    )


def cmd_status(_args: argparse.Namespace) -> None:
    print(json.dumps({name: _is_kit_agent(pid) for name, pid in _pids().items()}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "start", "stop", "status", "windows-bundle"):
        sub.add_parser(name)
    args = parser.parse_args()
    try:
        if args.command != "build":
            check_state()
        commands = {
            "build": cmd_build,
            "start": cmd_start,
            "stop": cmd_stop,
            "status": cmd_status,
            "windows-bundle": cmd_windows_bundle,
        }
        commands[args.command](args)
    except (KitError, subprocess.CalledProcessError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
