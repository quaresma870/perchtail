"""Unit tests for the growing-file scenarios: the files' layout and writer
(seed/growing.py), the report's tables, and, when node is on the PATH, the
k6 checks themselves (integrity.test.mjs) and a ZIP written the way the
server writes one, read by the k6 code.

    python -m unittest discover -s loadtest/tests
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "seed"))
sys.path.insert(0, str(HERE.parent / "report"))

import growing  # noqa: E402
import report  # noqa: E402

NODE = shutil.which("node")
T = 1763150000000


def read_lines(path: Path) -> list[dict]:
    data = path.read_bytes()
    assert len(data) % growing.LINE_BYTES == 0, len(data)
    return [
        growing.parse(data[i : i + growing.LINE_BYTES])
        for i in range(0, len(data), growing.LINE_BYTES)
    ]


class LineTests(unittest.TestCase):
    def test_line_layout(self) -> None:
        # The same line as integrity.test.mjs checks on the k6 side.
        self.assertEqual(
            growing.line(1, 1, T, 262144, "growing-1"),
            b"G00001 S0000000001 T1763150000000 R000262144 INFO  growing-1            "
            + b"b" * 55
            + b"\n",
        )

    def test_lines_are_lines(self) -> None:
        block = growing.lines(3, 99, 5, T, 1024, "heavy-2")
        self.assertEqual(len(block), 5 * growing.LINE_BYTES)
        for i in range(5):
            raw = block[i * growing.LINE_BYTES : (i + 1) * growing.LINE_BYTES]
            self.assertEqual(raw, growing.line(3, 99 + i, T, 1024, "heavy-2"))
        self.assertEqual(
            growing.parse(block[: growing.LINE_BYTES]),
            {"gen": 3, "seq": 99, "ts_ms": T, "rate": 1024},
        )


class WriterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "growing" / "growing-1.log"

    def test_restart_append_and_rotate_keep_the_sequence(self) -> None:
        f = growing.GrowingFile(self.path, "growing-1", rotate_s=60)
        f.restart(10 * growing.LINE_BYTES + 50)
        self.assertEqual([x["seq"] for x in read_lines(self.path)], list(range(1, 11)))
        # 2.5 lines a second for 2 s: 5 lines; the half line is owed.
        self.assertEqual(f.append(2.5 * growing.LINE_BYTES, 1.8, T), 4 * growing.LINE_BYTES)
        f.append(2.5 * growing.LINE_BYTES, 0.4, T)
        self.assertEqual([x["seq"] for x in read_lines(self.path)], list(range(1, 16)))
        self.assertFalse(f.due(f.started + 59))
        self.assertTrue(f.due(f.started + 60))
        self.assertEqual(f.rotate(f.started + 60), "rename")
        f.append(3 * growing.LINE_BYTES, 1, T)
        old = read_lines(self.path.with_name("growing-1.log.1"))
        new = read_lines(self.path)
        self.assertEqual([(x["gen"], x["seq"]) for x in old], [(1, s) for s in range(1, 16)])
        self.assertEqual([(x["gen"], x["seq"]) for x in new], [(2, 1), (2, 2), (2, 3)])

    def test_rotation_by_size(self) -> None:
        f = growing.GrowingFile(self.path, "heavy-1", rotate_bytes=4 * growing.LINE_BYTES)
        f.restart(0)
        f.append(5 * growing.LINE_BYTES, 1, T)
        self.assertTrue(f.due(f.started))
        f.rotate(f.started)
        self.assertEqual((f.gen, f.seq, f.size), (2, 1, 0))

    def test_a_blocked_rename_is_retried_then_copied_and_truncated(self) -> None:
        f = growing.GrowingFile(self.path, "growing-1", rotate_s=1)
        f.restart(3 * growing.LINE_BYTES)
        with mock.patch.object(growing.os, "replace", side_effect=PermissionError):
            self.assertIsNone(f.rotate(100.0))
            self.assertTrue(f.due(100.5))
            self.assertIsNone(f.rotate(105.0))
            self.assertEqual(f.rotate(110.0), "copytruncate")
        self.assertEqual(len(read_lines(self.path.with_name("growing-1.log.1"))), 3)
        self.assertEqual(self.path.stat().st_size, 0)
        self.assertEqual(f.gen, 2)


class RateTests(unittest.TestCase):
    def test_daily_schedule(self) -> None:
        size, duration = 3 * 1024**3, 3600
        segments = growing.daily_schedule(size, duration, 0.7, 5)
        volume = sum((s["end"] - s["start"]) * s["rate"] for s in segments)
        self.assertAlmostEqual(volume, size, delta=1)
        business = [s for s in segments if 9 <= s["hour"] < 18]
        office = sum((s["end"] - s["start"]) * s["rate"] for s in business)
        self.assertAlmostEqual(office / size, 0.7, places=6)
        mean = office / (9 * duration / 24)
        self.assertAlmostEqual(max(s["rate"] for s in segments) / mean, 5, places=6)
        night = growing.rate_at(segments, 3 * duration / 24 + 1)
        self.assertLess(night, mean / 5)
        self.assertEqual(segments[0]["start"], 0)
        self.assertAlmostEqual(segments[-1]["end"], duration)
        self.assertEqual(growing.rate_at(segments, duration + 1), 0)

    def test_heavy_steps(self) -> None:
        profile = {"files": {"heavy_rates_mb_s": [1, 5], "heavy_step_seconds": 30}}
        self.assertEqual(
            growing.heavy_steps(profile),
            [
                {"step": 1, "start": 0, "end": 30, "rate": growing.MB},
                {"step": 2, "start": 30, "end": 60, "rate": 5 * growing.MB},
            ],
        )

    def test_layout_and_plan(self) -> None:
        profile = {
            "files": {"growing_count": 2, "heavy_count": 1, "daily_size_gb": 1},
            "phases": [],
        }
        shared = growing.layout(profile, "shared")
        self.assertEqual(
            [f["path"] for f in shared["growing_files"] + shared["heavy"] + shared["daily"]],
            [
                "growing/growing-1.log",
                "growing/growing-2.log",
                "heavy/heavy-1.log",
                "daily/access.log",
            ],
        )
        agent = growing.layout(profile, "agent")
        self.assertEqual((len(agent["growing_files"]), agent["heavy"], agent["daily"]), (2, [], []))
        roots = {"shared": Path("s"), "agent-001": Path("a1"), "agent-002": Path("a2")}
        plan = growing.plan(
            {**profile, "files": {**profile["files"], "growing_agents": 1}},
            ["growing_files"],
            roots,
        )
        self.assertEqual(sorted({e["tree"] for e in plan}), ["agent-001", "shared"])
        self.assertEqual(len(plan), 4)
        self.assertEqual(plan[0]["rate"](0), 256 * 1024)

    def test_disk_needed(self) -> None:
        profile = {
            "files": {
                "growing_count": 1,
                "growing_size_mb": 10,
                "growing_rate_kb_s": 1024,
                "growing_rotate_s": 10,
                "growing_agents": 0,
            }
        }
        # (10 MB + 10 MB of growth) twice (the .1 copy), one tree.
        self.assertEqual(growing.disk_needed(profile), 40 * growing.MB)


@unittest.skipUnless(NODE, "node isn't on the PATH")
class K6CodeTests(unittest.TestCase):
    def test_integrity_checks(self) -> None:
        proc = subprocess.run(
            [NODE, "--test", str(HERE / "integrity.test.mjs")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_a_zip_streamed_like_the_server_is_read_by_the_k6_code(self) -> None:
        class Sink(io.RawIOBase):
            """Unseekable, like the folder ZIP the server streams."""

            def __init__(self) -> None:
                self.data = bytearray()

            def writable(self) -> bool:
                return True

            def write(self, b) -> int:
                self.data += b
                return len(b)

        sink = Sink()
        with zipfile.ZipFile(sink, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, gen, count in (("growing-1.log", 2, 3000), ("growing-1.log.1", 1, 5000)):
                info = zipfile.ZipInfo(f"growing/{name}")
                info.compress_type = zipfile.ZIP_DEFLATED
                with zf.open(info, "w") as dst:
                    dst.write(growing.lines(gen, 1, count, T, 1024, "growing-1"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "folder.zip"
            path.write_bytes(bytes(sink.data))
            lib = (HERE.parent / "k6" / "lib").as_uri()
            script = (
                f"import {{ Stream }} from '{lib}/integrity.js';"
                f"import {{ listZip, readMember }} from '{lib}/zip.js';"
                "import { readFileSync } from 'node:fs';"
                "const b = new Uint8Array(readFileSync(process.argv[1]));"
                "const out = listZip(b).map((e) => { const s = new Stream();"
                " const p = readMember(b, e, (x) => s.push(x));"
                " return [e.name, p || s.error, s.lines] });"
                "console.log(JSON.stringify(out))"
            )
            proc = subprocess.run(
                [NODE, "--input-type=module", "-e", script, str(path)],
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            json.loads(proc.stdout),
            [["growing/growing-1.log", None, 3000], ["growing/growing-1.log.1", None, 5000]],
        )


def _values(**values) -> dict:
    return {"values": values, "thresholds": {}}


class ReportTests(unittest.TestCase):
    def summary(self) -> dict:
        tags = "suite:growing_files,gop:open,protocol:ssh"
        return {
            "metrics": {
                f"http_req_duration{{{tags}}}": _values(count=10, med=120.0, **{"p(95)": 900.0}),
                f"http_req_failed{{{tags}}}": _values(rate=0.1),
                f"integrity_errors{{{tags}}}": _values(count=2),
                f"growing_bytes{{{tags}}}": _values(count=5 * 1024 * 1024),
                "http_req_duration{suite:growing_files,gop:follow,protocol:ssh}": _values(count=0),
                "integrity_errors{suite:growing_files}": {
                    "values": {"count": 2},
                    "thresholds": {"count<1": {"ok": False}},
                },
            }
        }

    def test_growing_rows(self) -> None:
        rows = report.growing_rows(self.summary(), "growing_files")
        self.assertEqual(
            rows,
            [
                {
                    "protocol": "ssh",
                    "op": "open",
                    "requests": 10,
                    "p50": 120.0,
                    "p95": 900.0,
                    "errors": 0.1,
                    "integrity": 2,
                    "bytes": 5 * 1024 * 1024,
                }
            ],
        )

    def test_growing_markdown_lists_the_integrity_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            record = {
                "suite": "growing_files",
                "op": "open",
                "protocol": "ssh",
                "source": "lt-ssh-001",
                "path": "growing/growing-1.log",
                "error": 'missing lines (3) at byte 1280: expected G00001 S0000000011, found "x"',
            }
            message = "INTEGRITY " + json.dumps(record)
            (Path(tmp) / "k6.log").write_text(
                f'time="2026-11-14T10:00:00Z" level=warning msg={json.dumps(message)} '
                "source=console\n",
                encoding="utf-8",
            )
            text = "\n".join(
                report.growing_markdown("growing_files", "growing_files", self.summary(), Path(tmp))
            )
        self.assertIn("Integrity errors: **2**", text)
        self.assertIn("limits **not met**", text)
        self.assertIn("| ssh | open | 10 | 120 | 900 | 10.00 % | 2 | 5.0 |", text)
        self.assertIn("missing lines (3) at byte 1280", text)

    def test_follow_refusals_are_reported_apart_from_errors(self) -> None:
        summary = self.summary()
        self.assertEqual(report.follow_refusal_lines(summary, "daily_log"), [])
        summary["metrics"]["refused_too_large{suite:daily_log}"] = _values(count=3)
        summary["metrics"]["follow_end_only{suite:daily_log}"] = _values(count=7)
        text = "\n".join(report.follow_refusal_lines(summary, "daily_log"))
        self.assertIn('as the viewer\'s "Tail the end"): 7 tabs', text)
        self.assertIn("`refused_too_large`", text)
        self.assertIn("it isn't counted as an error): 3.", text)

    def test_falls_behind(self) -> None:
        self.assertFalse(report.falls_behind(1.0, 1.4, 2))
        self.assertFalse(report.falls_behind(1.0, 2.9, 2))
        self.assertTrue(report.falls_behind(1.0, 3.5, 2))
        self.assertIsNone(report.falls_behind(None, 3.5, 2))

    def test_heavy_verdict(self) -> None:
        profile = {
            "files": {"heavy_count": 2, "heavy_rates_mb_s": [1, 5], "heavy_step_seconds": 60}
        }

        def lag(rate: str, part: str, med: float) -> tuple[str, dict]:
            key = f"follow_lag_seconds{{suite:growing_heavy,rate:{rate},part:{part}}}"
            return key, _values(count=10, med=med, **{"p(95)": med * 2})

        summary = {
            "metrics": dict(
                [
                    lag("1", "early", 1.0),
                    lag("1", "late", 1.2),
                    lag("5", "early", 2),
                    lag("5", "late", 9),
                ]
            )
        }
        with tempfile.TemporaryDirectory() as tmp:
            text = "\n".join(report.heavy_markdown("growing_heavy", summary, Path(tmp), profile, 2))
        self.assertIn("**Follow stops keeping up from 5 MB/s per file.**", text)
        self.assertIn("| 1 | 1.0 | 1.2 |", text)


if __name__ == "__main__":
    unittest.main()
