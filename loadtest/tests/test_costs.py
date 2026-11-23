"""Unit tests for the cost matrix: counters attributed to a time window,
the fixed columns of its CSV files, the TOTP codes it signs in with, and
the cost units `report.py costs` works out.

    python -m unittest discover -s loadtest/tests
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "seed"))
sys.path.insert(0, str(HERE.parent / "matrix"))
sys.path.insert(0, str(HERE.parent / "report"))

import common  # noqa: E402
import costs  # noqa: E402
import matrix  # noqa: E402
import measure  # noqa: E402
import report  # noqa: E402

MB = 1024 * 1024


class WindowTests(unittest.TestCase):
    READINGS = [
        {
            "t": 100.0,
            "cpu_usec": 1_000_000,
            "mem": 10,
            "rx_bytes": 0,
            "tx_bytes": 0,
            "rx_packets": 0,
            "tx_packets": 0,
        },
        {
            "t": 101.0,
            "cpu_usec": 1_500_000,
            "mem": 30,
            "rx_bytes": 1000,
            "tx_bytes": 10,
            "rx_packets": 5,
            "tx_packets": 5,
        },
        {
            "t": 102.0,
            "cpu_usec": 1_600_000,
            "mem": 20,
            "rx_bytes": 3000,
            "tx_bytes": 30,
            "rx_packets": 9,
            "tx_packets": 7,
        },
    ]

    def test_a_counter_between_readings_is_interpolated(self) -> None:
        self.assertEqual(measure.at(self.READINGS, 100.5, "cpu_usec"), 1_250_000)
        self.assertEqual(measure.at(self.READINGS, 99, "cpu_usec"), 1_000_000)
        self.assertEqual(measure.at(self.READINGS, 105, "cpu_usec"), 1_600_000)
        self.assertIsNone(measure.at(self.READINGS, 100, "missing"))

    def test_window_cost(self) -> None:
        cost = measure.window_cost(self.READINGS, 100.5, 102.0)
        self.assertEqual(cost["cpu_ms"], 350)
        self.assertEqual(cost["rx_bytes"], 2500)
        self.assertEqual(cost["tx_bytes"], 25)
        self.assertEqual(cost["packets"], 11)  # (9 - 2.5) + (7 - 2.5)
        self.assertEqual(cost["mem_peak"], 30)  # the highest reading inside the window

    def test_an_empty_series_costs_nothing_known(self) -> None:
        cost = measure.window_cost([], 0, 1)
        self.assertEqual(
            cost,
            {"cpu_ms": None, "mem_peak": None, "rx_bytes": None, "tx_bytes": None, "packets": None},
        )

    def test_percentile(self) -> None:
        self.assertEqual(measure.percentile([5, 1, 3, 2, 4], 50), 3)
        self.assertEqual(measure.percentile(list(range(1, 101)), 95), 95)
        self.assertIsNone(measure.percentile([], 50))


class CsvTests(unittest.TestCase):
    def test_measurements_have_fixed_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = measure.CsvOut(Path(tmp))
            out.row(
                {
                    "step": "matrix",
                    "protocol": "ssh",
                    "operation": "open",
                    "size_bytes": MB,
                    "parallelism": 5,
                    "delay_ms": 20,
                    "latency_p95_ms": 12.3456,
                }
            )
            out.row({"step": "fixed", "protocol": "smb", "operation": "name-index"})
            with self.assertRaises(ValueError):
                out.row({"step": "matrix", "made_up": 1})
            with (Path(tmp) / "measurements.csv").open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], measure.COLUMNS)
        self.assertEqual(
            rows[0][:7],
            ["step", "protocol", "operation", "size_bytes", "entries", "parallelism", "delay_ms"],
        )
        self.assertTrue(all(len(r) == len(measure.COLUMNS) for r in rows))
        record = dict(zip(rows[0], rows[1], strict=True))
        self.assertEqual(
            (record["size_bytes"], record["delay_ms"], record["latency_p95_ms"]),
            (str(MB), "20", "12.346"),
        )
        self.assertEqual(record["entries"], "")

    def test_a_second_writer_appends_without_a_second_header(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            measure.CsvOut(Path(tmp)).row({"step": "a"})
            measure.CsvOut(Path(tmp)).row({"step": "b"})
            lines = (Path(tmp) / "measurements.csv").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)


class TotpTests(unittest.TestCase):
    def test_rfc_6238_vector(self) -> None:
        # RFC 6238, SHA-1 key "12345678901234567890" at T=59: 94287082 (8 digits).
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(measure.totp(secret, at=59, digits=8), "94287082")
        self.assertEqual(measure.totp(secret, at=1111111109, digits=8), "07081804")
        self.assertEqual(measure.totp(secret, at=59), "287082")


def row(**values) -> dict:
    base = {c: "" for c in measure.COLUMNS}
    base.update({k: str(v) for k, v in values.items()})
    return base


class CostTests(unittest.TestCase):
    ROWS = [
        row(
            step="matrix",
            protocol="ssh",
            operation="open",
            size_bytes=MB,
            parallelism=1,
            delay_ms=0,
            requests=4,
            bytes=4 * MB,
            server_cpu_ms=400,
            source_cpu_ms=80,
            server_net_rx_bytes=5 * MB,
            server_net_packets=400,
        ),
        row(
            step="matrix",
            protocol="ssh",
            operation="open",
            size_bytes=10 * MB,
            parallelism=1,
            delay_ms=0,
            requests=2,
            bytes=20 * MB,
            server_cpu_ms=1000,
            source_cpu_ms=200,
            server_net_rx_bytes=21 * MB,
        ),
        row(
            step="matrix",
            protocol="ssh",
            operation="open",
            size_bytes=MB,
            parallelism=5,
            delay_ms=0,
            requests=10,
            bytes=10 * MB,
            server_cpu_ms=1500,
        ),
        row(
            step="matrix",
            protocol="ssh",
            operation="open",
            size_bytes=MB,
            parallelism=1,
            delay_ms=80,
            requests=4,
            bytes=4 * MB,
            server_cpu_ms=999,
        ),
        row(
            step="matrix",
            protocol="smb",
            operation="download",
            size_bytes=MB,
            parallelism=1,
            delay_ms=0,
            requests=2,
            bytes=2 * MB,
            server_cpu_ms=300,
            server_net_rx_bytes=2 * MB,
        ),
        row(
            step="matrix",
            protocol="smb",
            operation="list",
            entries=100,
            parallelism=1,
            delay_ms=0,
            requests=5,
            server_cpu_ms=50,
        ),
        row(
            step="fixed",
            protocol="ssh",
            operation="source-check",
            requests=1,
            server_cpu_ms=20,
            server_net_rx_bytes=1000,
            wall_s=0.5,
        ),
        row(
            step="fixed",
            protocol="ssh",
            operation="name-index",
            entries=5000,
            requests=1,
            server_cpu_ms=300,
            server_net_rx_bytes=50000,
            wall_s=4,
        ),
    ]

    def find(self, units: list[dict], **match) -> list[dict]:
        return [u for u in units if all(u[k] == v for k, v in match.items())]

    def test_operation_units_leave_out_added_delay(self) -> None:
        units = costs.operation_units(self.ROWS)
        cpu = self.find(units, protocol="ssh", metric="server_cpu_ms_per_request")
        self.assertEqual(
            {u["operation"]: u["value"] for u in cpu},
            {f"open {MB} x1": 100, f"open {10 * MB} x1": 500, f"open {MB} x5": 150},
        )

    def test_mb_units_one_at_a_time(self) -> None:
        units = costs.mb_units(self.ROWS)
        ssh = {u["metric"]: u["value"] for u in self.find(units, protocol="ssh")}
        self.assertAlmostEqual(ssh["server_cpu_ms_per_mb"], 1400 / 24)
        self.assertAlmostEqual(ssh["wire_bytes_per_file_mb"], 26 * MB / 24)
        self.assertAlmostEqual(ssh["source_cpu_ms_per_mb"], 280 / 24)
        smb = {u["metric"]: u["value"] for u in self.find(units, protocol="smb")}
        self.assertEqual(smb["server_cpu_ms_per_mb"], 150)

    def test_source_units(self) -> None:
        units = costs.source_units(self.ROWS, check_minutes=10, index_minutes=15)
        per_hour = self.find(units, metric="server_cpu_ms_per_hour")[0]
        self.assertEqual(per_hour["value"], 20 * 6 + 300 * 4)
        per_10k = self.find(units, metric="server_cpu_ms_per_10000_files")[0]
        self.assertEqual(per_10k["value"], 600)
        self.assertEqual(
            self.find(units, metric="server_net_rx_bytes_per_hour")[0]["value"],
            1000 * 6 + 50000 * 4,
        )
        off = costs.source_units(self.ROWS, check_minutes=0, index_minutes=15)
        self.assertEqual(self.find(off, metric="server_cpu_ms_per_hour")[0]["value"], 1200)

    def test_matrix_cost_by_size(self) -> None:
        self.assertEqual(costs.matrix_cost(self.ROWS, "open", "smallest"), 100)
        self.assertEqual(costs.matrix_cost(self.ROWS, "open", "largest"), 500)
        self.assertEqual(costs.matrix_cost(self.ROWS, "list", "smallest"), 10)
        self.assertIsNone(costs.matrix_cost(self.ROWS, "follow", None))

    def test_user_units_from_a_production_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            phase = run_dir / "production"
            phase.mkdir()
            plan = {
                "stages": [{"duration": 3600, "target": 10}, {"duration": 3600, "target": 10}],
                "personas": {"support": {"session_minutes": [20, 40]}},
            }
            (run_dir / "run.json").write_text(
                json.dumps(
                    {
                        "production": plan,
                        "phases": [{"name": "production", "scenarios": ["production"]}],
                    }
                ),
                encoding="utf-8",
            )
            with (phase / "server.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["elapsed_s", "cpu_percent", "mem_bytes", "db_bytes", "audit_rows"],
                )
                writer.writeheader()
                for i in range(3):
                    writer.writerow(
                        {
                            "elapsed_s": i * 5,
                            "cpu_percent": 100,
                            "mem_bytes": 100 + i,
                            "db_bytes": 1000 + i * 1500,
                            "audit_rows": i * 75,
                        }
                    )
            summary = {
                "metrics": {
                    "sessions{persona:support}": {"values": {"count": 6}},
                    "http_req_duration{persona:support,gop:open}": {"values": {"count": 30}},
                    "http_req_duration{persona:support,gop:admin}": {"values": {"count": 3}},
                }
            }
            (phase / "k6-production.json").write_text(json.dumps(summary), encoding="utf-8")
            units = costs.user_units(run_dir, self.ROWS)
        # 15 user-hours: a rise from 0 to 10 over the first hour, then 10 for an hour.
        overall = {u["metric"]: u["value"] for u in self.find(units, persona="all")}
        self.assertEqual(overall["server_cpu_ms_per_user_hour"], 3 * 100 * 5 * 10 / 15)
        self.assertEqual(overall["audit_rows_per_user_hour"], 10)
        self.assertEqual(overall["db_bytes_per_user_hour"], 200)
        support = self.find(units, persona="support", operation="open")
        rate = next(u for u in support if u["metric"] == "requests_per_session_hour")
        self.assertEqual(rate["value"], 10)  # 30 opens in 6 sessions of 30 minutes
        cpu = next(u for u in support if u["metric"] == "server_cpu_ms_per_session_hour")
        self.assertEqual(cpu["value"], 10 * 100)  # the matrix's smallest open: 100 ms
        mapped = self.find(units, persona="support", operation="mapped operations")[0]
        self.assertEqual(mapped["value"], 1000)  # admin has no matrix counterpart

    def test_costs_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "matrix").mkdir()
            with (run_dir / "matrix" / "measurements.csv").open(
                "w", newline="", encoding="utf-8"
            ) as handle:
                writer = csv.DictWriter(handle, fieldnames=measure.COLUMNS)
                writer.writeheader()
                writer.writerows(self.ROWS)
            text = costs.costs(run_dir)
            with (run_dir / "costs.csv").open(encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
        self.assertEqual(rows[0], costs.COSTS_COLUMNS)
        self.assertIn("| MB transferred | ssh |", text)
        self.assertNotIn("recommend", text.lower())


class SourceCsvTests(unittest.TestCase):
    def test_a_server_collector_is_charged_to_each_cell_by_its_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ssh-a.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(
                    ["t", "name", "processes", "cpu_ms", "rss_bytes", "read_bytes", "write_bytes"]
                )
                for t in range(100, 111):
                    # sshd: 100 ms of CPU and 1 MB read per second from t=103 to t=106.
                    busy = min(max(t - 103, 0), 3)
                    writer.writerow([t, "sshd", 2, 500 + busy * 100, 4096, busy * MB, 0])
            series = costs.read_source_csv(path)
        rows = [
            row(
                step="matrix",
                protocol="ssh",
                operation="open",
                size_bytes=MB,
                parallelism=1,
                requests=3,
                start=103,
                end=106,
            ),
            row(
                step="matrix",
                protocol="ssh",
                operation="list",
                entries=100,
                parallelism=1,
                requests=5,
                start=107,
                end=109,
            ),
            row(
                step="matrix",
                protocol="ssh",
                operation="open",
                size_bytes=MB,
                parallelism=1,
                requests=3,
            ),
        ]
        units = costs.source_csv_units(rows, "ssh-a.csv", series)
        values = {(u["operation"], u["metric"]): u["value"] for u in units}
        self.assertEqual(values[(f"open {MB} x1", "source_host_cpu_ms_per_request")], 100)
        self.assertEqual(values[(f"open {MB} x1", "source_host_disk_bytes_per_request")], MB)
        self.assertEqual(values[("list 100 x1", "source_host_cpu_ms_per_request")], 0)
        self.assertEqual(len(units), 4)  # the cell without a window isn't charged
        self.assertIn("sshd", units[0]["basis"])

    def test_report_costs_passes_the_source_files_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp) / "ssh-a.csv", Path(tmp) / "win-a.csv"
            a.write_text("t,name\n", encoding="utf-8")
            b.write_text("t,name\n", encoding="utf-8")
            with (
                mock.patch.object(costs, "costs", return_value="") as made,
                mock.patch("sys.stdout"),
            ):
                argv = ["costs", tmp, "--production", "p"]
                report.main(argv + ["--source-csv", str(a), "--source-csv", str(b)])
                made.assert_called_once_with(Path(tmp), Path("p"), [a, b])
                made.reset_mock()
                report.main(["costs", tmp, "--source-csv", str(a), str(b)])
                made.assert_called_once_with(Path(tmp), None, [a, b])

    def test_report_costs_refuses_a_missing_source_file(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(costs, "costs") as made,
            mock.patch("sys.stderr"),
            self.assertRaises(SystemExit),
        ):
            report.main(["costs", tmp, "--source-csv", str(Path(tmp) / "none.csv")])
        made.assert_not_called()


class KitContainerTests(unittest.TestCase):
    """tc netem and docker stop/start only touch the kit's test servers."""

    def matrix(self, netem: bool = True, containers: list[str] | None = None):
        m = matrix.Matrix.__new__(matrix.Matrix)
        m.args = mock.Mock(netem=netem, source_container=containers or [])
        m.delay_ms = 0
        return m

    def test_only_a_container_with_the_kit_label_is_accepted(self) -> None:
        with mock.patch.object(matrix, "container_label", return_value="test-source"):
            matrix.require_kit_container("perchtail-lt-ssh-1")
        for label in (None, "", "other"):
            with (
                mock.patch.object(matrix, "container_label", return_value=label),
                self.assertRaisesRegex(common.KitError, "isn't one of the kit's test servers"),
            ):
                matrix.require_kit_container("perchtail-perchtail-1")
        with self.assertRaises(common.KitError):
            matrix.require_kit_container("")

    def test_netem_refuses_another_container_before_running_anything(self) -> None:
        m = self.matrix(containers=["perchtail-lt-ssh-1", "perchtail-perchtail-1"])

        def label(name: str) -> str | None:
            return "test-source" if name == "perchtail-lt-ssh-1" else None

        with (
            mock.patch.object(matrix, "container_label", side_effect=label),
            mock.patch.object(matrix.subprocess, "run") as run_,
        ):
            with self.assertRaisesRegex(common.KitError, "perchtail-perchtail-1"):
                m.set_delay(20)
            with self.assertRaises(common.KitError):
                m.netem("perchtail-perchtail-1", 0)
        run_.assert_not_called()

    def test_without_netem_nothing_runs_in_the_source_containers(self) -> None:
        m = self.matrix(netem=False, containers=["perchtail-perchtail-1"])
        with mock.patch.object(matrix.subprocess, "run") as run_:
            m.set_delay(0)
        run_.assert_not_called()

    def test_reliability_refuses_a_fault_container_that_isnt_the_kits(self) -> None:
        m = self.matrix()
        m.args.reliability = True
        m.fault = {"id": 1, "name": "lt-ssh-002"}
        with (
            mock.patch.dict("os.environ", {"LOADTEST_FAULT_CONTAINER": "perchtail-perchtail-1"}),
            mock.patch.object(matrix, "container_label", return_value=None),
            mock.patch.object(matrix.subprocess, "run") as run_,
            self.assertRaisesRegex(common.KitError, "isn't one of the kit's test servers"),
        ):
            m.step_reliability()
        run_.assert_not_called()


if __name__ == "__main__":
    unittest.main()
