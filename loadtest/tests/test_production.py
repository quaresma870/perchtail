"""Unit tests for the production profile's plan (seed/production.py) and
its summary (report/report.py): the sources and their write rates, the
kinds of people and their users, the day's schedule and the incident, the
verdict, and the roles seed.py gives each kind.

    python -m unittest discover -s loadtest/tests
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "seed"))
sys.path.insert(0, str(HERE.parent / "report"))

import production  # noqa: E402
import report  # noqa: E402
import seed  # noqa: E402

PROFILES = HERE.parent / "profiles"


def load(name: str) -> dict:
    return json.loads((PROFILES / f"{name}.json").read_text(encoding="utf-8"))


class DistributionTests(unittest.TestCase):
    def test_weighted_counts_add_up_and_round_fairly(self) -> None:
        self.assertEqual(
            production.weighted_counts({"a": 70, "b": 25, "c": 5}, 170), {"a": 119, "b": 42, "c": 9}
        )
        counts = production.weighted_counts({"a": 1, "b": 1, "c": 1}, 10)
        self.assertEqual(sum(counts.values()), 10)
        self.assertTrue(all(3 <= n <= 4 for n in counts.values()))

    def test_spread_interleaves(self) -> None:
        order = production.spread({"a": 6, "b": 3, "c": 1})
        self.assertEqual(Counter(order), {"a": 6, "b": 3, "c": 1})
        b = [i for i, k in enumerate(order) if k == "b"]
        self.assertGreaterEqual(min(y - x for x, y in zip(b, b[1:], strict=False)), 2)

    def test_production_sources(self) -> None:
        profile = load("production")
        plan = production.source_plan(profile, windows_hosts=False)
        self.assertEqual(
            (plan["ssh"], plan["agent"], plan["smb"], plan["winrm"], plan["windows_agents"]),
            (60, 40, 40, 30, 10),
        )
        self.assertEqual(plan["ssh"] + plan["agent"] - plan["windows_agents"], 90)  # Linux
        self.assertEqual(plan["smb"] + plan["winrm"] + plan["windows_agents"], 80)  # Windows
        self.assertEqual(plan["windows_agents_on"], "load machine")
        self.assertEqual(
            production.source_plan(profile, windows_hosts=True)["windows_agents_on"],
            "windows test server",
        )
        with mock.patch.dict(os.environ, {"LOADTEST_PREFIX": "lt"}):
            planned = seed.planned_sources(profile)
        self.assertEqual(len(planned), 170)
        windows = [s["name"] for s in planned if s.get("windows")]
        self.assertEqual(windows, [f"lt-agent-{i:03d}" for i in range(31, 41)])

    def test_rate_classes(self) -> None:
        profile = load("production")
        names = [f"src-{i:03d}" for i in range(170)]
        rates = production.rate_classes(profile, names)
        self.assertEqual(rates, production.rate_classes(profile, names))  # deterministic
        self.assertEqual(
            Counter(r["class"] for r in rates.values()), {"calm": 119, "moving": 42, "intense": 9}
        )
        for r in rates.values():
            if r["class"] == "calm":
                self.assertTrue(2 * 1024 <= r["rate"] <= 8 * 1024)
            elif r["class"] == "moving":
                self.assertTrue(100 * 1024 <= r["rate"] <= 300 * 1024)
            else:
                self.assertEqual(r["daily_size"], 3 * 1024**3)
        # The intense sources aren't bunched at one end (so not all on one protocol).
        intense = [i for i, n in enumerate(names) if rates[n]["class"] == "intense"]
        self.assertGreater(max(intense) - min(intense), 85)

    def test_a_small_profile_gets_every_class(self) -> None:
        rates = production.rate_classes(load("production-mini"), [f"s{i}" for i in range(10)])
        self.assertEqual(
            Counter(r["class"] for r in rates.values()), {"calm": 7, "moving": 2, "intense": 1}
        )

    def test_tree(self) -> None:
        profile = load("production")
        tree = production.tree(profile, "lt-ssh-001", {"class": "calm"}, 0)
        self.assertEqual(
            tree["current"], [{"path": "app-1/current.log", "name": "lt-ssh-001-current"}]
        )
        self.assertEqual(len(tree["static"]), 9)
        self.assertEqual(tree["large"][0]["size"], 200 * 1024 * 1024)
        self.assertEqual(production.tree(profile, "x", {"class": "calm"}, 3)["large"], [])


class PeopleTests(unittest.TestCase):
    def test_user_personas_follow_the_weights(self) -> None:
        users = [f"lt-user-{i:03d}" for i in range(1, 41)]
        kinds = production.user_personas(load("production"), users)
        self.assertEqual(
            Counter(kinds.values()),
            {"support": 16, "oncall": 10, "investigation": 8, "admin": 4, "occasional": 2},
        )
        self.assertEqual(kinds, production.user_personas(load("production"), users))

    def test_profile_personas_override_the_defaults(self) -> None:
        personas = production.personas(load("production-mini"))
        self.assertEqual(personas["support"]["session_minutes"], [2, 4])
        self.assertEqual(personas["support"]["weight"], 40)

    def test_roles_give_each_kind_its_own_scope(self) -> None:
        profile = load("production-mini")
        customers = [{"id": 101}, {"id": 102}]
        folders = {101: [{"id": 201}, {"id": 202}], 102: [{"id": 203}, {"id": 204}]}
        client = mock.Mock()
        created = []

        def post(path, body, **_kw):
            created.append((path, body))
            return {
                "id": len(created),
                "name": body.get("name"),
                "global_capabilities": body.get("global_capabilities", []),
            }

        client.post.side_effect = post
        client.get.side_effect = lambda path: (
            [] if path.endswith("/grants") or path == "/roles" else []
        )
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(
                os.environ,
                {
                    "LOADTEST_STATE_DIR": tmp,
                    "LOADTEST_BASE_URL": "http://localhost:8080",
                    "LOADTEST_ALLOWED_HOSTS": "localhost",
                    "LOADTEST_PREFIX": "lt",
                },
            ),
        ):
            roles = seed.ensure_persona_roles(client, seed.Ledger(), profile, customers, folders)
        self.assertEqual(set(roles), {"support", "oncall", "investigation", "admin", "occasional"})
        ids = {}
        for path, body in created:
            if path == "/roles":
                ids[created.index((path, body)) + 1] = body["name"]
        by_name = {name: [] for name in ids.values()}
        for path, body in created:
            if path != "/roles":
                rid = int(path.split("/")[2])
                by_name[ids[rid]].append(
                    (body["scope_type"], body["scope_id"], tuple(body["capabilities"]))
                )
        self.assertEqual(
            by_name["lt-role-oncall"], [("folder", 201, ("view",)), ("folder", 203, ("view",))]
        )
        self.assertEqual(by_name["lt-role-occasional"], [])  # customers 4 and 5 don't exist here
        self.assertEqual(
            by_name["lt-role-investigation"],
            [("customer", 101, ("view", "download")), ("customer", 102, ("view", "download"))],
        )
        admin_body = next(b for p, b in created if p == "/roles" and b["name"] == "lt-role-admin")
        self.assertIn("view_audit_log", admin_body["global_capabilities"])
        self.assertIn("create_source", admin_body["global_capabilities"])  # to edit sources


class DiskTests(unittest.TestCase):
    def test_disk_per_protocol_matches_the_readme(self) -> None:
        # loadtest/README.md, "Setting up 100 or more sources with a few test servers".
        # On a test server (`--out`), the writer shares the rates out among the
        # sources that aren't agents, and `generate --protocols` counts the same way.
        profile = load("production")
        sources = [s for s in seed.planned_sources(profile) if s["protocol"] != "agent"]

        def gib(*protocols: str) -> float:
            return round(production.disk_needed(profile, sources, set(protocols)) / 1024**3, 1)

        self.assertEqual(
            {"ssh": gib("ssh"), "smb": gib("smb"), "ssh,smb": gib("ssh", "smb")},
            {"ssh": 26.9, "smb": 17.9, "ssh,smb": 44.8},
        )
        self.assertEqual(gib("winrm"), 16.4)


class ScheduleTests(unittest.TestCase):
    def test_the_day_rises_holds_at_least_30_minutes_and_falls(self) -> None:
        stages = production.concurrency_stages(load("production"))
        targets = [s["target"] for s in stages]
        self.assertEqual(targets, sorted(targets[:5]) + targets[5:])  # rises, then falls
        self.assertEqual(max(targets), 30)
        self.assertEqual(targets[-1], 8)
        first, last = production.peak_window(stages)
        self.assertGreaterEqual(last - first, 30 * 60)
        self.assertAlmostEqual(production.total_seconds(stages), 121 * 60, delta=2)

    def test_the_incident_is_in_the_middle_of_the_peak(self) -> None:
        profile = load("production")
        stages = production.concurrency_stages(profile)
        incident = production.incident_window(profile, stages)
        first, last = production.peak_window(stages)
        self.assertEqual(incident["duration"], 600)
        self.assertEqual((incident["users"], incident["zip_users"]), (20, 4))
        self.assertGreaterEqual(incident["start"], first)
        self.assertLessEqual(incident["start"] + incident["duration"], last)
        self.assertEqual(incident["start"] - first, last - incident["start"] - incident["duration"])

    def test_the_incident_can_be_placed_and_stays_in_the_run(self) -> None:
        profile = load("production-mini")
        stages = production.concurrency_stages(profile)
        profile["production"]["incident"] = {"at_minutes": 9, "minutes": 5}
        incident = production.incident_window(profile, stages)
        self.assertEqual(incident["start"] + incident["duration"], production.total_seconds(stages))

    def test_planned_users_over_time(self) -> None:
        stages = [
            {"duration": 60, "target": 10},
            {"duration": 60, "target": 10},
            {"duration": 60, "target": 0},
        ]
        self.assertEqual(report.planned_users(stages, 30), 5)
        self.assertEqual(report.planned_users(stages, 90), 10)
        self.assertEqual(report.planned_users(stages, 150), 5)


class VerdictTests(unittest.TestCase):
    def test_within_the_limits(self) -> None:
        summary = {
            "metrics": {
                "http_req_duration{scenario:production,gop:open}": {
                    "thresholds": {"p(95)<2000": {"ok": True}}
                }
            }
        }
        self.assertEqual(
            report.production_verdict(summary, {"hours": 2, "share": 0.01, "slope": 1}, 0), []
        )

    def test_where_it_is_not(self) -> None:
        summary = {
            "metrics": {
                "http_req_duration{scenario:production,gop:open}": {
                    "thresholds": {"p(95)<2000": {"ok": False}}
                },
                "integrity_errors{suite:production}": {"thresholds": {"count<1": {"ok": False}}},
                "http_req_duration{persona:support,gop:open}": {
                    "thresholds": {"max>=0": {"ok": False}}
                },
            }
        }
        problems = report.production_verdict(
            summary, {"hours": 2, "share": 0.12, "slope": 30 * 1024**2}, 4
        )
        self.assertEqual(len(problems), 4)
        self.assertIn("`http_req_duration{scenario:production,gop:open}` p(95)<2000", problems)
        self.assertTrue(any("possible leak" in p for p in problems))
        self.assertTrue(any("4 requests refused" in p for p in problems))

    def test_a_short_run_never_calls_a_leak(self) -> None:
        self.assertEqual(
            report.production_verdict({"metrics": {}}, {"hours": 0.2, "share": 0.5, "slope": 1}, 0),
            [],
        )

    def test_memory_trend(self) -> None:
        rows = [
            {
                "timestamp": f"2026-11-14T10:{m:02d}:00Z",
                "mem_bytes": str(100 * 1024**2 + m * 1024**2),
            }
            for m in range(60)
        ]
        trend = report.memory_trend(rows)
        self.assertAlmostEqual(trend["slope"] / 1024**2, 60, delta=0.5)
        self.assertAlmostEqual(trend["hours"], 59 / 60)


if __name__ == "__main__":
    unittest.main()
