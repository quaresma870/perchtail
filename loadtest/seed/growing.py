"""The files that grow while the growing_files, growing_heavy and daily_log
scenarios run, and the process that writes them.

Every line of these files has the same length, LINE_BYTES, and says where
it belongs, so that a reader can check that what it received is an
unbroken piece of the file:

    G00001 S0000000001 T1763150000000 R000262144 INFO  growing-1            aaaa...a\\n

- G: the generation, 1 for the file as created, plus one at each rotation;
- S: the line's sequence number in its generation, from 1, with no gaps;
- T: when the line was written, in milliseconds since the epoch (UTC);
- R: the writer's rate for this file when it wrote the line, in bytes/s;
- the file's name, then a filler letter chosen by the sequence number, up
  to the newline that ends the line.

The k6 side, k6/lib/integrity.js, reads the same layout (the offsets below);
the two are kept in step by a test on a fixed line.

Files and folders, relative to a tree's root:

    growing/growing-N.log    files.growing_count files of files.growing_size_mb,
                             appended at files.growing_rate_kb_s, rotated every
                             files.growing_rotate_s (renamed to .1, started over)
    heavy/heavy-N.log        files.heavy_count files appended at each rate of
                             files.heavy_rates_mb_s for files.heavy_step_seconds,
                             rotated past files.heavy_rotate_mb
    daily/access.log         one file that grows to files.daily_size_gb over
                             files.daily_duration_min, at an hourly pattern

growing/ is in the shared tree and in the first files.growing_agents agent
trees; heavy/ and daily/ are in the shared tree only.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

LINE_BYTES = 128
NAME_WIDTH = 20
# Byte offsets in a line (k6/lib/integrity.js has the same).
OFF_GEN, LEN_GEN = 1, 5
OFF_SEQ, LEN_SEQ = 8, 10
OFF_TS, LEN_TS = 20, 13
OFF_RATE, LEN_RATE = 35, 9
OFF_FILL = 72

DEFAULTS = {
    "growing_count": 0,
    "growing_size_mb": 50,
    "growing_rate_kb_s": 256,
    "growing_rotate_s": 300,
    "growing_agents": 2,
    "heavy_count": 0,
    "heavy_start_mb": 1,
    "heavy_rates_mb_s": [1, 5, 10],
    "heavy_step_seconds": 120,
    "heavy_rotate_mb": 256,
    "daily_size_gb": 0,
    "daily_duration_min": 60,
    "daily_business_share": 0.7,
    "daily_peak_factor": 5,
}
TICK_SECONDS = 0.2
MB = 1024 * 1024


def setting(profile: dict, key: str):
    return profile.get("files", {}).get(key, DEFAULTS[key])


def line(gen: int, seq: int, ts_ms: int, rate: int, name: str) -> bytes:
    """One line, exactly LINE_BYTES long."""
    head = f"G{gen:05d} S{seq:010d} T{ts_ms:013d} R{min(rate, 999_999_999):09d} INFO  "
    head += f"{name[:NAME_WIDTH]:<{NAME_WIDTH}} "
    fill = LINE_BYTES - 1 - len(head)
    return (head + chr(97 + seq % 26) * fill + "\n").encode("ascii")


def lines(gen: int, first_seq: int, count: int, ts_ms: int, rate: int, name: str) -> bytes:
    """`count` lines from `first_seq` on, all stamped `ts_ms`."""
    head = f" T{ts_ms:013d} R{min(rate, 999_999_999):09d} INFO  {name[:NAME_WIDTH]:<{NAME_WIDTH}} "
    fill = LINE_BYTES - 1 - len(head) - 18
    fills = [chr(97 + i) * fill + "\n" for i in range(26)]
    prefix = f"G{gen:05d} S"
    return "".join(
        f"{prefix}{seq:010d}{head}{fills[seq % 26]}" for seq in range(first_seq, first_seq + count)
    ).encode("ascii")


def parse(raw: bytes) -> dict:
    """The fields of one line (for tests and the writer's resume)."""
    return {
        "gen": int(raw[OFF_GEN : OFF_GEN + LEN_GEN]),
        "seq": int(raw[OFF_SEQ : OFF_SEQ + LEN_SEQ]),
        "ts_ms": int(raw[OFF_TS : OFF_TS + LEN_TS]),
        "rate": int(raw[OFF_RATE : OFF_RATE + LEN_RATE]),
    }


# --- layout -----------------------------------------------------------------


def layout(profile: dict, tree: str) -> dict:
    """The growing files of a tree ("shared" or "agent"), as the catalog
    lists them."""
    out: dict = {"growing_files": [], "heavy": [], "daily": []}
    count = int(setting(profile, "growing_count"))
    for n in range(1, count + 1):
        out["growing_files"].append({"path": f"growing/growing-{n}.log", "name": f"growing-{n}"})
    if tree == "shared":
        for n in range(1, int(setting(profile, "heavy_count")) + 1):
            out["heavy"].append({"path": f"heavy/heavy-{n}.log", "name": f"heavy-{n}"})
        if float(setting(profile, "daily_size_gb")) > 0:
            out["daily"].append({"path": "daily/access.log", "name": "daily-access"})
    return out


def disk_needed(profile: dict) -> int:
    """Bytes the growing files can take at their largest, in the shared
    tree and the agent trees (rotated copies included)."""
    count = int(setting(profile, "growing_count"))
    per_file = float(setting(profile, "growing_size_mb")) * MB + float(
        setting(profile, "growing_rate_kb_s")
    ) * 1024 * float(setting(profile, "growing_rotate_s"))
    trees = 1 + int(setting(profile, "growing_agents")) if count else 0
    heavy = (
        int(setting(profile, "heavy_count")) * 2 * float(setting(profile, "heavy_rotate_mb")) * MB
    )
    daily = float(setting(profile, "daily_size_gb")) * 1024 * MB
    return int(count * trees * 2 * per_file + heavy + daily)


# --- rates --------------------------------------------------------------------


def heavy_steps(profile: dict) -> list[dict]:
    """growing_heavy's steps: {start, end, rate} with times in seconds from
    the writer's start and rates in bytes per second, per file."""
    step = float(setting(profile, "heavy_step_seconds"))
    out = []
    for i, mb_s in enumerate(setting(profile, "heavy_rates_mb_s")):
        out.append(
            {"step": i + 1, "start": i * step, "end": (i + 1) * step, "rate": int(mb_s * MB)}
        )
    return out


# Share of a business hour's volume in each of its 12 slots: most slots at
# the base level, and two peaks (morning and after lunch) where the rate
# reaches the peak factor times the hour's mean.
SLOTS_PER_HOUR = 12
BUSINESS_HOURS = range(9, 18)
PEAK_HOURS = {10: 3, 15: 8}  # hour: slot of the peak
OFF_HOURS_WEIGHTS = {
    **{h: 0.4 for h in range(0, 6)},
    6: 0.8,
    7: 1.5,
    8: 2.5,
    18: 2.0,
    19: 1.5,
    20: 1.2,
    21: 1.0,
    22: 0.7,
    23: 0.5,
}


def daily_schedule(
    size_bytes: float, duration_s: float, business_share: float = 0.7, peak_factor: float = 5
) -> list[dict]:
    """The daily_log writer's rates: a "day" of 24 hours compressed into
    `duration_s`, as segments {hour, start, end, rate} (seconds from the
    start, bytes per second) that add up to `size_bytes`.

    `business_share` of the volume falls between 09:00 and 18:00, spread
    evenly over those hours except for two peaks (10:15 and 15:40) at
    `peak_factor` times the mean of the business hours; the rest follows
    a night-low, morning-rise, evening-fall pattern."""
    hour_s = duration_s / 24
    slot_s = hour_s / SLOTS_PER_HOUR
    business_rate = size_bytes * business_share / (len(BUSINESS_HOURS) * hour_s)
    peak_rate = business_rate * peak_factor
    peaks = len(PEAK_HOURS)
    business_slots = len(BUSINESS_HOURS) * SLOTS_PER_HOUR
    # The other business slots share what the peaks leave.
    base_rate = (business_rate * business_slots - peak_rate * peaks) / (business_slots - peaks)
    weights_total = sum(OFF_HOURS_WEIGHTS.values())
    off_volume = size_bytes * (1 - business_share)
    out = []
    for hour in range(24):
        start = hour * hour_s
        if hour in BUSINESS_HOURS:
            for slot in range(SLOTS_PER_HOUR):
                rate = peak_rate if PEAK_HOURS.get(hour) == slot else base_rate
                out.append(
                    {
                        "hour": hour,
                        "start": start + slot * slot_s,
                        "end": start + (slot + 1) * slot_s,
                        "rate": rate,
                    }
                )
        else:
            rate = off_volume * OFF_HOURS_WEIGHTS[hour] / weights_total / hour_s
            out.append({"hour": hour, "start": start, "end": start + hour_s, "rate": rate})
    return out


def rate_at(segments: list[dict], elapsed: float) -> float:
    for seg in segments:
        if seg["start"] <= elapsed < seg["end"]:
            return seg["rate"]
    return 0.0


# --- writing ----------------------------------------------------------------


class GrowingFile:
    """One file being written: its generation, its next sequence number and
    the fraction of a line owed from the last tick."""

    def __init__(self, path: Path, name: str, rotate_s: float = 0, rotate_bytes: int = 0):
        self.path, self.name = path, name
        self.rotate_s, self.rotate_bytes = rotate_s, rotate_bytes
        self.gen, self.seq, self.owed = 1, 1, 0.0
        self.size = 0
        self.started = time.time()
        self.rotation_pending = False
        self.rotations: list[dict] = []

    def restart(self, start_bytes: int, now_ms: int | None = None) -> None:
        """Writes the file afresh: generation 1, `start_bytes` (whole lines)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stale = self.path.with_name(self.path.name + ".1")
        if stale.exists():
            stale.unlink()
        count = start_bytes // LINE_BYTES
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        with self.path.open("wb") as handle:
            done = 0
            while done < count:
                n = min(65536, count - done)
                handle.write(lines(1, done + 1, n, now_ms, 0, self.name))
                done += n
        self.gen, self.seq, self.owed = 1, count + 1, 0.0
        self.size = count * LINE_BYTES
        self.started = time.time()

    def append(self, rate: float, seconds: float, now_ms: int) -> int:
        """Appends `rate` bytes/s for `seconds`, in whole lines; returns the
        bytes written."""
        self.owed += rate * seconds / LINE_BYTES
        count = int(self.owed)
        if count <= 0:
            return 0
        self.owed -= count
        data = lines(self.gen, self.seq, count, now_ms, int(rate), self.name)
        with self.path.open("ab") as handle:
            handle.write(data)
        self.seq += count
        self.size += len(data)
        return len(data)

    def due(self, now: float) -> bool:
        if self.rotation_pending:
            return True
        if self.rotate_s and now - self.started >= self.rotate_s:
            return True
        return bool(self.rotate_bytes and self.size >= self.rotate_bytes)

    def rotate(self, now: float) -> str | None:
        """Renames the file to .1 and starts generation gen+1. On Windows a
        reader holding the file can block the rename; then it is retried
        on the next ticks, and after 10 s the file is copied to .1 and
        truncated instead (logrotate's copytruncate). Returns how it was
        rotated, or None when it is still pending."""
        rotated = self.path.with_name(self.path.name + ".1")
        how = "rename"
        try:
            os.replace(self.path, rotated)
        except PermissionError:
            if not self.rotation_pending:
                self.rotation_pending, self.pending_since = True, now
            if now - self.pending_since < 10:
                return None
            shutil.copyfile(self.path, rotated)
            with self.path.open("r+b") as handle:
                handle.truncate(0)
            how = "copytruncate"
        if how == "rename":
            self.path.touch()
        self.rotation_pending = False
        self.gen, self.seq, self.owed, self.size = self.gen + 1, 1, 0.0, 0
        self.started = now
        self.rotations.append({"at": now, "gen": self.gen, "how": how})
        return how


class EventLog:
    """writer.jsonl in a phase's results: what the writer did and when, for
    the report (the steps of growing_heavy, the hours of daily_log, file
    sizes over time)."""

    def __init__(self, path: Path | None):
        self.path = path

    def write(self, event: str, **fields) -> None:
        if not self.path:
            return
        record = {"t": round(time.time(), 3), "event": event, **fields}
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


def plan(profile: dict, scenarios: list[str], roots: dict[str, Path]) -> list[dict]:
    """What the writer writes for a phase's scenarios: one entry per file,
    {file, kind, rate(elapsed) -> bytes/s, start_bytes}. `roots` maps
    "shared" and each agent tree ("agent-001", ...) to its folder."""
    out = []
    if "growing_files" in scenarios:
        rate = float(setting(profile, "growing_rate_kb_s")) * 1024
        start = int(float(setting(profile, "growing_size_mb")) * MB)
        agent_trees = sorted(k for k in roots if k != "shared")[
            : int(setting(profile, "growing_agents"))
        ]
        for tree in ["shared", *agent_trees]:
            for item in layout(profile, "shared" if tree == "shared" else "agent")["growing_files"]:
                out.append(
                    {
                        "file": GrowingFile(
                            roots[tree] / item["path"],
                            item["name"],
                            rotate_s=float(setting(profile, "growing_rotate_s")),
                        ),
                        "kind": "growing_files",
                        "tree": tree,
                        "rate": lambda _elapsed, rate=rate: rate,
                        "start_bytes": start,
                    }
                )
    if "growing_heavy" in scenarios:
        steps = heavy_steps(profile)
        start = int(float(setting(profile, "heavy_start_mb")) * MB)
        rotate = int(float(setting(profile, "heavy_rotate_mb")) * MB)
        for item in layout(profile, "shared")["heavy"]:
            out.append(
                {
                    "file": GrowingFile(
                        roots["shared"] / item["path"], item["name"], rotate_bytes=rotate
                    ),
                    "kind": "growing_heavy",
                    "tree": "shared",
                    "rate": lambda elapsed, steps=steps: rate_at(steps, elapsed),
                    "start_bytes": start,
                }
            )
    if "protocol_matrix" in scenarios and matrix_layout(profile):
        rate = float(profile["matrix"].get("follow_rate_kb_s", 256)) * 1024
        for tree, root in roots.items():
            out.append(
                {
                    "file": GrowingFile(root / "matrix" / "follow.log", "follow"),
                    "kind": "protocol_matrix",
                    "tree": tree,
                    "rate": lambda _elapsed, rate=rate: rate,
                    "start_bytes": 0,
                }
            )
    if "daily_log" in scenarios:
        segments = daily_schedule(
            float(setting(profile, "daily_size_gb")) * 1024 * MB,
            float(setting(profile, "daily_duration_min")) * 60,
            float(setting(profile, "daily_business_share")),
            float(setting(profile, "daily_peak_factor")),
        )
        for item in layout(profile, "shared")["daily"]:
            out.append(
                {
                    "file": GrowingFile(roots["shared"] / item["path"], item["name"]),
                    "kind": "daily_log",
                    "tree": "shared",
                    "rate": lambda elapsed, segments=segments: rate_at(segments, elapsed),
                    "start_bytes": 0,
                }
            )
    return out


def matrix_layout(profile: dict) -> dict:
    """protocol_matrix's files, the same in every tree: folders of
    `matrix.lists` entries, files of `matrix.sizes_mb`, and follow.log,
    which the writer keeps growing at `matrix.follow_rate_kb_s`."""
    m = profile.get("matrix", {})
    if not m:
        return {}
    return {
        "lists": {str(n): f"matrix/list-{n}" for n in m.get("lists", [100, 1000, 10000])},
        "files": {str(mb): f"matrix/file-{mb}m.log" for mb in m.get("sizes_mb", [1, 10, 50, 200])},
        "follow": "matrix/follow.log",
    }


def write_matrix(root: Path, profile: dict) -> int:
    """Writes the matrix files missing under `root`."""
    spec = matrix_layout(profile)
    written = 0
    for entries, folder in spec.get("lists", {}).items():
        path = root / folder
        if path.exists() and sum(1 for _ in path.iterdir()) == int(entries):
            continue
        path.mkdir(parents=True, exist_ok=True)
        line_block = lines(1, 1, 8, 1763150000000, 0, "list-entry")
        for i in range(1, int(entries) + 1):
            entry = path / f"entry-{i:05d}.log"
            if not entry.exists():
                entry.write_bytes(line_block)
                written += 1
    for mb, rel in spec.get("files", {}).items():
        path = root / rel
        size = int(float(mb) * MB)
        if path.exists() and path.stat().st_size == size // LINE_BYTES * LINE_BYTES:
            continue
        GrowingFile(path, f"file-{mb}m").restart(size)
        written += 1
    if spec and not (root / spec["follow"]).exists():
        GrowingFile(root / spec["follow"], "follow").restart(0)
        written += 1
    return written


def run_writer(
    entries: list[dict],
    events: EventLog,
    should_stop: Callable[[], bool],
    profile: dict,
    sample_every: float = 5.0,
) -> None:
    """Restarts every file, then appends to each at its rate until
    `should_stop()`; rotates when due. Records the start, every step or
    hour change, every rotation and, every `sample_every` seconds, each
    file's size."""
    for entry in entries:
        entry["file"].restart(entry["start_bytes"])
    started = time.time()
    events.write(
        "start",
        files=[
            {
                "path": str(e["file"].path),
                "kind": e["kind"],
                "tree": e["tree"],
                "size": e["file"].size,
            }
            for e in entries
        ],
        heavy_steps=heavy_steps(profile),
    )
    last_tick, last_sample = started, 0.0
    current: dict[str, float] = {}
    while not should_stop():
        time.sleep(TICK_SECONDS)
        now = time.time()
        seconds, last_tick = now - last_tick, now
        elapsed = now - started
        now_ms = int(now * 1000)
        for entry in entries:
            f: GrowingFile = entry["file"]
            rate = entry["rate"](elapsed)
            key = f"{entry['kind']}:{entry['tree']}:{f.name}"
            if current.get(key) != rate:
                current[key] = rate
                events.write(
                    "rate", file=str(f.path), kind=entry["kind"], rate=rate, elapsed=elapsed
                )
            if f.due(now):
                how = f.rotate(now)
                if how:
                    events.write("rotate", file=str(f.path), gen=f.gen, how=how)
            f.append(rate, seconds, now_ms)
        if now - last_sample >= sample_every:
            last_sample = now
            events.write(
                "sizes",
                elapsed=round(elapsed, 1),
                files={str(e["file"].path): e["file"].size for e in entries},
            )
    events.write("stop", elapsed=round(time.time() - started, 1))
