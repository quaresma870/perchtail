"""The plan of the `production` profile: PerchTail as an installation in
use, with many sources of every protocol, each with its own tree of logs
and its own write rate, and people of different kinds signing in and out
through a working day.

Everything here is deterministic for a given profile (seeded random
numbers), so a run can be repeated and the unit tests can check it:

- `source_plan`: how many sources of each protocol, and which agents are
  meant to run on a Windows test server;
- `rate_classes`: each source's write rate, from the profile's classes
  (by default 70 % calm, 25 % with movement, 5 % intense);
- `weighted_counts` and `user_personas`: the users of each kind of person;
- `concurrency_stages` and `incident_window`: how many people are signed
  in over time, and when the incident happens;
- `tree`: the files of one source.

seed.py writes the result to `production-plan.json` in the instance's
state, which the k6 scenario `production` reads.
"""

from __future__ import annotations

import gzip
import random
import zipfile
from pathlib import Path

import growing

MB = growing.MB

DEFAULT_CUSTOMERS = [
    "brightwater-retail",
    "kestrel-freight",
    "harbourline-bank",
    "orchard-health",
    "nordvale-energy",
]
DEFAULT_FOLDERS = ["production", "staging", "shared-services"]

DEFAULT_CLASSES = {
    "calm": {"share": 0.70, "kb_s": [2, 8], "rotate_mb": 64},
    "moving": {"share": 0.25, "kb_s": [100, 300], "rotate_mb": 64},
    "intense": {"share": 0.05, "daily_size_gb": 3},
}

# Kinds of people, with their share of sessions, how long a session lasts
# and how long they pause. Every kind has its own role (see ROLES).
DEFAULT_PERSONAS = {
    "support": {
        "weight": 40,
        "session_minutes": [20, 60],
        "think_seconds": [10, 40],
        "read_seconds": [20, 90],
        "files_per_visit": [1, 3],
    },
    "oncall": {
        "weight": 25,
        "session_minutes": [40, 120],
        "follow_files": [2, 5],
        "think_seconds": [10, 30],
    },
    "investigation": {
        "weight": 20,
        "session_minutes": [20, 60],
        "think_seconds": [20, 60],
        "read_seconds": [20, 60],
    },
    "admin": {"weight": 10, "session_minutes": [10, 30], "think_seconds": [15, 45]},
    "occasional": {"weight": 5, "session_minutes": [1, 4], "read_seconds": [20, 60]},
}

# What each kind of person may do. Grants are on customers by number
# (1-based) and, for oncall, on each customer's first folder only: not
# everybody sees everything.
ROLES = {
    "support": {"global": [], "customers": [1, 2, 3], "capabilities": ["view"]},
    "oncall": {"global": [], "first_folders": True, "capabilities": ["view"]},
    "investigation": {"global": [], "customers": "all", "capabilities": ["view", "download"]},
    "admin": {
        # create_source too: editing a source needs it with manage_source.
        "global": [
            "manage_users",
            "manage_roles",
            "manage_system_settings",
            "view_audit_log",
            "create_source",
        ],
        "customers": "all",
        "capabilities": ["view", "download", "manage_rules", "manage_source"],
    },
    "occasional": {"global": [], "customers": [4, 5], "capabilities": ["view"]},
}


def settings(profile: dict) -> dict:
    return profile.get("production", {})


def personas(profile: dict) -> dict:
    """The profile's kinds of people, each merged over the defaults."""
    own = settings(profile).get("personas", {})
    names = own.keys() if own else DEFAULT_PERSONAS.keys()
    return {n: {**DEFAULT_PERSONAS.get(n, {}), **own.get(n, {})} for n in names}


def weighted_counts(weights: dict[str, float], total: int) -> dict[str, int]:
    """Splits `total` by weight, rounding by largest remainder: the counts
    add up to `total`, and each is within one of its exact share."""
    whole = sum(weights.values())
    exact = {k: total * w / whole for k, w in weights.items()}
    counts = {k: int(v) for k, v in exact.items()}
    left = total - sum(counts.values())
    # Largest remainder first; on a tie, the smaller share (so 5 % of 170
    # is 9 sources rather than 8).
    for k in sorted(exact, key=lambda k: (-(exact[k] - counts[k]), weights[k]))[:left]:
        counts[k] += 1
    return counts


def spread(counts: dict[str, int]) -> list[str]:
    """The keys of `counts`, each repeated its count, interleaved so that
    every stretch of the list has about the right mix."""
    total = sum(counts.values())
    placed = {k: 0 for k in counts}
    out = []
    for i in range(total):
        # The key furthest behind its share at this point.
        key = max(counts, key=lambda k: (counts[k] * (i + 1) / total - placed[k], counts[k]))
        placed[key] += 1
        out.append(key)
    return out


def user_personas(profile: dict, usernames: list[str]) -> dict[str, str]:
    weights = {n: p["weight"] for n, p in personas(profile).items()}
    order = spread(weighted_counts(weights, len(usernames)))
    return dict(zip(usernames, order, strict=True))


def source_plan(profile: dict, windows_hosts: bool) -> dict:
    """The sources by protocol. `agents.windows` of the running agents are
    Windows agents: run on a Windows test server when there is one
    (`windows_hosts`), otherwise by the kit on the load machine, which the
    summary says."""
    agents = profile.get("agents", {})
    windows = int(agents.get("windows", 0))
    return {
        "ssh": int(profile["sources"].get("ssh", 0)),
        "smb": int(profile["sources"].get("smb", 0)),
        "local": int(profile["sources"].get("local", 0)),
        "winrm": int(profile["sources"].get("winrm", 0)),
        "agent": int(agents.get("running", 0)) + int(agents.get("stopped", 0)),
        "windows_agents": windows,
        "windows_agents_on": "windows test server" if windows_hosts else "load machine",
    }


def classes(profile: dict) -> dict:
    own = settings(profile).get("rate_classes", {})
    names = own.keys() if own else DEFAULT_CLASSES.keys()
    return {n: {**DEFAULT_CLASSES.get(n, {}), **own.get(n, {})} for n in names}


def rate_classes(profile: dict, names: list[str], seed: int = 277) -> dict[str, dict]:
    """Each source's class and rate: {class, rate (bytes/s, constant
    classes), daily_size (bytes, intense), rotate_bytes}. The classes are
    spread over the sources in a seeded order, so every protocol gets its
    share of each."""
    cls = classes(profile)
    counts = weighted_counts({n: c["share"] for n, c in cls.items()}, len(names))
    # A small profile still gets one source of every class (taken from the
    # biggest), so production-mini has an intense log too.
    for kind, c in cls.items():
        if counts[kind] == 0 and c["share"] > 0 and len(names) >= len(cls):
            biggest = max(counts, key=lambda k: counts[k])
            counts[biggest] -= 1
            counts[kind] = 1
    order = spread(counts)
    rng = random.Random(seed)
    shuffled = list(names)
    rng.shuffle(shuffled)
    out = {}
    for name, kind in zip(shuffled, order, strict=True):
        c = cls[kind]
        entry: dict = {"class": kind}
        if "daily_size_gb" in c:
            entry["daily_size"] = int(float(c["daily_size_gb"]) * 1024 * MB)
        else:
            lo, hi = c["kb_s"]
            entry["rate"] = int(rng.uniform(lo, hi) * 1024)
            entry["rotate_bytes"] = int(float(c.get("rotate_mb", 64)) * MB)
        out[name] = entry
    return {n: out[n] for n in names}


def concurrency_stages(profile: dict) -> list[dict]:
    """How many people are signed in over time, as k6 `ramping-vus`
    stages {duration (seconds), target}: people arrive through the morning
    (the rise to the peak in three parts, faster in the middle), the peak
    holds, then people leave towards `end_users`."""
    p = settings(profile)
    peak = int(p.get("peak_users", 30))
    start = int(p.get("start_users", max(1, peak // 10)))
    end = int(p.get("end_users", max(1, peak // 4)))
    up = float(p.get("ramp_up_minutes", 30)) * 60
    hold = float(p.get("peak_minutes", 60)) * 60
    down = float(p.get("ramp_down_minutes", 30)) * 60
    rise = peak - start
    stages = [{"duration": 60, "target": start}]
    for share, time_share in ((0.25, 0.4), (0.5, 0.3), (0.25, 0.3)):
        stages.append({"duration": round(up * time_share), "target": 0})
        stages[-1]["share"] = share
    reached = start
    for stage in stages[1:]:
        reached += rise * stage.pop("share")
        stage["target"] = round(reached)
    stages[-1]["target"] = peak
    stages.append({"duration": round(hold), "target": peak})
    stages.append({"duration": round(down), "target": end})
    return [s for s in stages if s["duration"] > 0]


def total_seconds(stages: list[dict]) -> int:
    return sum(s["duration"] for s in stages)


def peak_window(stages: list[dict]) -> tuple[int, int]:
    """When the peak holds: (start, end) in seconds from the start, from
    when the number signed in has reached its peak to when it starts to
    fall."""
    peak = max(s["target"] for s in stages)
    t, level, first, last = 0, 0, None, None
    for s in stages:
        if s["target"] == peak and level == peak:
            first = t if first is None else first
            last = t + s["duration"]
        t += s["duration"]
        level = s["target"]
    return int(first or 0), int(last or 0)


def incident_window(profile: dict, stages: list[dict]) -> dict:
    """The incident: `users` people open and follow the same big growing
    file for `minutes`, and `zip_users` of them download its folder as a
    ZIP. By default it starts in the middle of the peak."""
    p = settings(profile).get("incident", {})
    duration = int(float(p.get("minutes", 10)) * 60)
    if "at_minutes" in p:
        start = int(float(p["at_minutes"]) * 60)
    else:
        first, last = peak_window(stages)
        start = max(0, (first + last) // 2 - duration // 2)
    total = total_seconds(stages)
    if start + duration > total:
        start = max(0, total - duration)
    return {
        "start": start,
        "duration": min(duration, total),
        "users": int(p.get("users", 20)),
        "zip_users": int(p.get("zip_users", 4)),
    }


def tree(profile: dict, name: str, entry: dict, index: int) -> dict:
    """The files of one source, relative to its root: `app-N/` folders with
    a growing `current.log` (only in app-1), rotated logs, a .gz and a .zip
    of rotated logs; every `large_every`-th source has a big file too.
    Every file is written with growing.py's checkable lines."""
    p = settings(profile).get("tree", {})
    folders = int(p.get("app_folders", 3))
    rotated = int(p.get("rotated", 3))
    sizes = p.get("rotated_kb", [64, 512, 2048])
    out: dict = {"current": [], "static": [], "gz": [], "zip": [], "large": [], "folders": []}
    for f in range(1, folders + 1):
        folder = f"app-{f}"
        out["folders"].append(folder)
        if f == 1:
            out["current"].append({"path": f"{folder}/current.log", "name": f"{name}-current"})
        for r in range(1, rotated + 1):
            size = int(sizes[(f + r) % len(sizes)]) * 1024
            out["static"].append({"path": f"{folder}/service.log.{r}", "size": size})
        out["gz"].append({"path": f"{folder}/service.log.{rotated + 1}.gz", "size": 256 * 1024})
        out["zip"].append(
            {
                "path": f"{folder}/archive.zip",
                "members": ["day-1.log", "day-2.log"],
                "size": 128 * 1024,
            }
        )
    every = int(p.get("large_every", 10))
    if every and index % every == 0:
        out["large"].append(
            {"path": "large/big.log", "size": int(float(p.get("large_mb", 200)) * MB)}
        )
        out["folders"].append("large")
    return out


def day_seconds(profile: dict) -> float:
    """The length of the intense sources' "day" (their daily_log pattern):
    `day_minutes`, by default the length of the run."""
    minutes = settings(profile).get("day_minutes")
    return float(minutes) * 60 if minutes else float(total_seconds(concurrency_stages(profile)))


def source_root(source: dict, shared: Path, agents_dir: Path) -> Path:
    """Where a source's tree is: its own folder under shared/prod/ for the
    servers, the agent's own folder for an agent."""
    if source["protocol"] == "agent":
        return agents_dir / f"agent-{source['agent_index']:03d}"
    return shared / "prod" / source["name"]


def start_bytes(entry: dict) -> int:
    """How big current.log is when a run starts."""
    return {"calm": 64 * 1024, "moving": MB}.get(entry["class"], 0)


def write_tree(root: Path, spec: dict, name: str) -> int:
    """Writes a source's files that are missing (current.log too, at its
    starting size; the writer starts it afresh at each run)."""
    written = 0

    def checkable(size: int, label: str) -> bytes:
        count = max(1, size // growing.LINE_BYTES)
        return growing.lines(1, 1, count, 1763150000000, 0, label)

    for item in spec["static"] + spec["large"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                done, count = 0, item["size"] // growing.LINE_BYTES
                while done < count:
                    n = min(65536, count - done)
                    handle.write(growing.lines(1, done + 1, n, 1763150000000, 0, name))
                    done += n
            written += 1
    for item in spec["gz"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with gzip.open(path, "wb", compresslevel=6) as handle:
                handle.write(checkable(item["size"], name))
            written += 1
    for item in spec["zip"]:
        path = root / item["path"]
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for m, member in enumerate(item["members"]):
                    archive.writestr(member, checkable(item["size"] * (m + 1), name))
            written += 1
    for item in spec["current"]:
        path = root / item["path"]
        if not path.exists():
            growing.GrowingFile(path, item["name"]).restart(0)
            written += 1
    return written


def writer_entries(
    profile: dict,
    sources: list[dict],
    shared: Path,
    agents_dir: Path,
    protocols: set[str] | None = None,
) -> list[dict]:
    """The writer's files for a production phase: each source's current.log
    at its class's rate. Calm and moving logs rotate by size; an intense
    log follows the daily_log pattern over `day_seconds` and rotates at the
    end of each day. With `protocols`, only the sources of those protocols
    (their rates still come from all of `sources`)."""
    names = [s["name"] for s in sources]
    rates = rate_classes(profile, names)
    day = day_seconds(profile)
    out = []
    for source in sources:
        if protocols is not None and source["protocol"] not in protocols:
            continue
        if source["protocol"] == "agent" and not source.get("running", True):
            continue
        entry = rates[source["name"]]
        root = source_root(source, shared, agents_dir)
        path = root / "app-1" / "current.log"
        label = f"{source['name']}-current"
        if "daily_size" in entry:
            segments = growing.daily_schedule(entry["daily_size"], day)
            f = growing.GrowingFile(path, label, rotate_s=day)
            rate = lambda elapsed, segments=segments, day=day: growing.rate_at(  # noqa: E731
                segments, elapsed % day
            )
        else:
            f = growing.GrowingFile(path, label, rotate_bytes=entry["rotate_bytes"])
            rate = lambda _elapsed, r=entry["rate"]: r  # noqa: E731
        out.append(
            {
                "file": f,
                "kind": f"production-{entry['class']}",
                "tree": source["name"],
                "rate": rate,
                "start_bytes": start_bytes(entry),
            }
        )
    return out


def disk_needed(profile: dict, sources: list[dict], protocols: set[str] | None = None) -> int:
    """Bytes the production trees can take: the static files, and each
    current.log at its largest (twice: the rotated copy). With `protocols`,
    only the trees of the sources of those protocols (their rates and big
    files still placed as among all of `sources`)."""
    names = [s["name"] for s in sources]
    rates = rate_classes(profile, names)
    total = 0
    for i, source in enumerate(sources):
        if protocols is not None and source["protocol"] not in protocols:
            continue
        spec = tree(profile, source["name"], rates[source["name"]], i)
        total += sum(f["size"] for f in spec["static"] + spec["large"])
        total += sum(f["size"] * 3 for f in spec["zip"]) + sum(f["size"] for f in spec["gz"])
        entry = rates[source["name"]]
        total += 2 * (entry.get("daily_size") or entry.get("rotate_bytes", 0))
    return total
