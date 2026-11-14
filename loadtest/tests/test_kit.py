"""Unit tests for the kit's own logic: per-instance state, the ledger, the
agents' environment and the ramp's verdict. Standard library only, no
instance needed:

    python -m unittest discover -s loadtest/tests
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "seed"))
sys.path.insert(0, str(HERE.parent / "report"))
sys.path.insert(0, str(HERE.parent / "collect"))
sys.path.insert(0, str(HERE.parent))

import agents  # noqa: E402
import collect  # noqa: E402
import common  # noqa: E402
import gen_logs  # noqa: E402
import report  # noqa: E402
import run  # noqa: E402
import seed  # noqa: E402

URL_A = "http://localhost:8080"
URL_B = "http://localhost:8080/other"


class StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = mock.patch.dict(
            os.environ,
            {
                "LOADTEST_STATE_DIR": self.tmp.name,
                "LOADTEST_BASE_URL": URL_A,
                "LOADTEST_ALLOWED_HOSTS": "localhost",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def use(self, url: str) -> None:
        os.environ["LOADTEST_BASE_URL"] = url


class InstanceStateTests(StateTestCase):
    def test_normalized_url_makes_the_port_explicit(self) -> None:
        self.assertEqual(common.normalized_url("HTTP://LocalHost/"), "http://localhost:80")
        self.assertEqual(
            common.normalized_url("https://h.example:8443/pt/"), "https://h.example:8443/pt"
        )
        self.assertEqual(common.normalized_url("http://[::1]:8080"), "http://[::1]:8080")

    def test_each_host_and_port_has_its_own_folder(self) -> None:
        self.assertEqual(common.instance_key(URL_A), "localhost-8080")
        self.assertEqual(common.instance_key("http://127.0.0.1:8081"), "127.0.0.1-8081")
        self.assertEqual(common.instance_key("http://[::1]:8080"), "__1-8080")

    def test_state_records_its_url_and_round_trips(self) -> None:
        path = common.write_state("users.json", {"lt-user-001": "x"})
        self.assertEqual(path.parent.name, "localhost-8080")
        self.assertEqual(json.loads(path.read_text())["base_url"], "http://localhost:8080")
        self.assertEqual(common.read_state("users.json"), {"lt-user-001": "x"})

    def test_state_of_another_url_is_refused(self) -> None:
        common.write_state("seeded.json", {"sources": []})
        self.use(URL_B)  # same host and port, so the same folder
        with self.assertRaisesRegex(common.KitError, "another instance's state"):
            common.read_state("seeded.json")
        with self.assertRaisesRegex(common.KitError, "another instance's state"):
            common.write_state("seeded.json", {"sources": []})

    def test_every_command_checks_the_whole_state_first(self) -> None:
        common.write_state("ledger.json", {})
        common.check_state()
        self.use(URL_B)
        with self.assertRaisesRegex(common.KitError, "another instance's state"):
            common.check_state()

    def test_a_file_without_its_url_is_refused(self) -> None:
        folder = common.state_dir()
        (folder / "ledger.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(common.KitError, "doesn't record the instance"):
            common.read_state("ledger.json")

    def test_state_of_the_old_single_instance_layout_is_refused(self) -> None:
        Path(self.tmp.name, "seeded.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(common.KitError, "earlier version of the kit"):
            common.state_dir()

    def test_settings_keep_the_first_original_value_of_each_key(self) -> None:
        client = mock.Mock()
        values = {
            "tail_interval_seconds": 5,
            "explorer_refresh_seconds": 0,
            "concurrency_global": 16,
        }
        client.get.side_effect = lambda path: dict(values)
        common.change_settings(client, {"tail_interval_seconds": 2})
        values["tail_interval_seconds"] = 2
        common.change_settings(client, {"tail_interval_seconds": 3, "concurrency_global": 32})
        self.assertEqual(
            common.read_state(common.SETTINGS_STATE),
            {"tail_interval_seconds": 5, "concurrency_global": 16},
        )
        with self.assertRaisesRegex(common.KitError, "unknown System settings"):
            common.change_settings(client, {"no_such_setting": 1})


class LedgerTests(StateTestCase):
    def test_creates_and_records_a_missing_object(self) -> None:
        ledger = seed.Ledger()
        item, created = seed.claim(
            ledger, "customers", "lt-customer-01", [], lambda: {"id": 7, "name": "lt-customer-01"}
        )
        self.assertTrue(created)
        self.assertEqual(item["id"], 7)
        self.assertEqual(seed.Ledger().owned("customers"), {"lt-customer-01": 7})

    def test_reuses_its_own_object(self) -> None:
        ledger = seed.Ledger()
        ledger.record("users", "lt-user-001", 3)
        current = [{"id": 3, "username": "lt-user-001", "active": False}]
        item, created = seed.claim(ledger, "users", "lt-user-001", current, self.fail)
        self.assertFalse(created)
        self.assertEqual(item["id"], 3)

    def test_never_adopts_an_object_it_did_not_create(self) -> None:
        current = [{"id": 9, "username": "lt-user-001", "active": True}]
        with self.assertRaisesRegex(common.KitError, "never adopts.*LOADTEST_PREFIX"):
            seed.claim(seed.Ledger(), "users", "lt-user-001", current, self.fail)

    def test_refuses_when_its_object_was_renamed(self) -> None:
        ledger = seed.Ledger()
        ledger.record("sources", "lt-ssh-001", 4)
        current = [{"id": 4, "name": "production-ssh"}]
        with self.assertRaisesRegex(common.KitError, "is now named"):
            seed.claim(ledger, "sources", "lt-ssh-001", current, self.fail)

    def test_recreates_an_object_removed_outside_the_kit(self) -> None:
        ledger = seed.Ledger()
        ledger.record("roles", "lt-role", 5)
        item, created = seed.claim(
            ledger, "roles", "lt-role", [], lambda: {"id": 6, "name": "lt-role"}
        )
        self.assertTrue(created)
        self.assertEqual(seed.Ledger().owned("roles"), {"lt-role": 6})

    def test_clean_only_touches_ledger_objects_with_the_exact_name(self) -> None:
        ledger = seed.Ledger()
        ledger.record("sources", "lt-ssh-001", 1)
        ledger.record("sources", "lt-ssh-002", 2)
        ledger.record("sources", "lt-smb-001", 3)
        current = [
            {"id": 1, "name": "lt-ssh-001", "is_system": False},
            {"id": 2, "name": "lt-ssh-002-renamed", "is_system": False},
            {"id": 10, "name": "lt-ssh-003", "is_system": False},  # not the kit's
        ]
        with mock.patch("sys.stderr"):
            owned = seed._owned_items(ledger, "sources", current)
        self.assertEqual([item["id"] for _, item in owned], [1])
        self.assertEqual(seed.Ledger().owned("sources"), {"lt-ssh-001": 1})

    def test_apply_refuses_before_creating_anything(self) -> None:
        profile = {"customers": 1, "folders_per_customer": 1, "users": 2, "sources": {"ssh": 1}}
        ledger = seed.Ledger()
        ledger.record("users", "lt-user-001", 1)
        lists = {
            "/customers": [],
            "/folders": [],
            "/sources": [],
            "/roles": [],
            "/users": [
                {"id": 1, "username": "lt-user-001"},
                {"id": 2, "username": "lt-user-002"},
            ],
        }
        client = mock.Mock()
        client.get.side_effect = lambda path: lists[path]
        with self.assertRaisesRegex(common.KitError, r"nothing seeded: .*lt-user-002 \(id 2\)"):
            seed.preflight(client, ledger, profile)
        client.post.assert_not_called()
        lists["/users"].pop()
        seed.preflight(client, ledger, profile)  # only its own user: fine

    def test_patterns_are_exact(self) -> None:
        self.assertTrue(seed.patterns()["sources"].match("lt-agent-012"))
        self.assertFalse(seed.patterns()["sources"].match("lt-ssh-01"))
        self.assertFalse(seed.patterns()["sources"].match("lt-ssh-001-prod"))
        self.assertFalse(seed.patterns()["users"].match("lt-user-001x"))
        self.assertFalse(seed.patterns()["roles"].match("lt-roles"))
        with self.assertRaises(common.KitError):
            seed.Ledger().record("users", "lt-admin", 1)


class PrefixTests(StateTestCase):
    def test_the_prefix_names_everything_and_is_checked(self) -> None:
        os.environ["LOADTEST_PREFIX"] = "lt2"
        names = seed.planned_names(
            {"customers": 1, "folders_per_customer": 1, "users": 1, "sources": {"smb": 1}}
        )
        self.assertEqual(
            names,
            {
                "customers": ["lt2-customer-01"],
                "folders": ["lt2-c01-folder-01"],
                "sources": ["lt2-smb-001"],
                "roles": ["lt2-role"],
                "users": ["lt2-user-001"],
            },
        )
        self.assertTrue(seed.patterns()["users"].match("lt2-user-001"))
        self.assertFalse(seed.patterns()["users"].match("lt-user-001"))
        for bad in ("LT", "lt-", "2lt", "lt.x", "a" * 17, ""):
            os.environ["LOADTEST_PREFIX"] = bad
            with self.assertRaisesRegex(common.KitError, "LOADTEST_PREFIX"):
                common.prefix()

    def test_each_prefix_has_its_own_part_of_the_ledger(self) -> None:
        seed.Ledger().record("users", "lt-user-001", 1)
        os.environ["LOADTEST_PREFIX"] = "lt2"
        ledger = seed.Ledger()
        self.assertEqual(ledger.owned("users"), {})
        self.assertEqual(ledger.other_prefixes(), ["lt"])
        ledger.record("users", "lt2-user-001", 7)
        with self.assertRaises(common.KitError):
            ledger.record("users", "lt-user-002", 8)  # not a name of this prefix
        data = common.read_state("ledger.json")
        self.assertEqual(data["lt"]["users"], {"lt-user-001": 1})
        self.assertEqual(data["lt2"]["users"], {"lt2-user-001": 7})

    def test_a_ledger_from_before_prefixes_belongs_to_lt(self) -> None:
        common.write_state("ledger.json", {"users": {"lt-user-001": 3}, "roles": {}})
        self.assertEqual(seed.Ledger().owned("users"), {"lt-user-001": 3})


class FakeApi:
    """Just enough of PerchTail's API for `seed.py clean`: lists, PATCH and
    DELETE, a 409 for a role still held by a user, and failures on demand."""

    NO_ACCESS = 2

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail: set[tuple[str, str]] = set()
        self.settings = {"tail_interval_seconds": 2, "source_check_minutes": 1, "theme": "x"}
        self.items = {
            "/customers": [{"id": 1, "name": "lt-customer-01"}, {"id": 9, "name": "prod"}],
            "/folders": [
                {"id": 1, "name": "lt-c01-folder-01", "customer_id": 1, "parent_folder_id": None}
            ],
            "/sources": [
                {"id": 1, "name": "lt-ssh-001", "is_system": False},
                {"id": 2, "name": "lt-ssh-002", "is_system": False},
                {"id": 3, "name": "lt-ssh-003", "is_system": False},  # not the kit's
            ],
            "/roles": [
                {"id": 1, "name": "Super Admin", "is_builtin": True, "is_super_admin": True},
                {"id": 2, "name": "No Access", "is_builtin": True, "is_super_admin": False},
                {"id": 5, "name": "lt-role", "is_builtin": False, "is_super_admin": False},
            ],
            "/users": [
                {"id": 1, "username": "admin", "active": True, "role_id": 1},
                {"id": 11, "username": "lt-user-001", "active": True, "role_id": 5},
                {"id": 12, "username": "lt-user-002", "active": True, "role_id": 5},
            ],
        }

    def _check(self, method: str, path: str) -> None:
        self.calls.append((method, path))
        if (method, path) in self.fail:
            raise common.ApiError(method, path, 500, "boom")

    def get(self, path: str, **_kw):
        if path == "/system-settings":
            return dict(self.settings)
        if path == "/monitoring/token":
            return {"configured": True}
        return [dict(item) for item in self.items[path]]

    def patch(self, path: str, body: dict, **_kw):
        self._check("PATCH", path)
        if path == "/system-settings":
            self.settings.update(body)
            return dict(self.settings)
        collection, object_id = path.rsplit("/", 1)
        for item in self.items[collection]:
            if item["id"] == int(object_id):
                item.update(body)
                return dict(item)
        raise common.ApiError("PATCH", path, 404, "not found")

    def delete(self, path: str, **_kw):
        self._check("DELETE", path)
        collection, object_id = path.rsplit("/", 1)
        if collection == "/roles" and any(
            u["role_id"] == int(object_id) for u in self.items["/users"]
        ):
            raise common.ApiError("DELETE", path, 409, "role still assigned")
        for item in self.items[collection]:
            if item["id"] == int(object_id):
                if collection == "/users":
                    item["active"] = False  # users are only deactivated
                else:
                    self.items[collection].remove(item)
                return None
        raise common.ApiError("DELETE", path, 404, "not found")


class CleanTests(StateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = FakeApi()
        patcher = mock.patch.object(seed, "admin_client", return_value=self.api)
        patcher.start()
        self.addCleanup(patcher.stop)
        ledger = seed.Ledger()
        for kind, name, object_id in (
            ("customers", "lt-customer-01", 1),
            ("folders", "lt-c01-folder-01", 1),
            ("sources", "lt-ssh-001", 1),
            ("sources", "lt-ssh-002", 2),
            ("roles", "lt-role", 5),
            ("users", "lt-user-001", 11),
            ("users", "lt-user-002", 12),
        ):
            ledger.record(kind, name, object_id)
        common.write_state(
            common.SETTINGS_STATE, {"tail_interval_seconds": 5, "source_check_minutes": 1}
        )
        common.write_state("seeded.json", {"sources": []})
        self.args = mock.Mock(files=False)

    def clean(self) -> None:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            seed.cmd_clean(self.args)

    def test_cleans_only_its_objects_in_order(self) -> None:
        self.clean()
        calls = self.api.calls
        self.assertEqual(
            [c for c in calls if c[0] == "DELETE"],
            [
                ("DELETE", "/sources/1"),
                ("DELETE", "/sources/2"),
                ("DELETE", "/folders/1"),
                ("DELETE", "/customers/1"),
                ("DELETE", "/users/11"),
                ("DELETE", "/users/12"),
                ("DELETE", "/roles/5"),
            ],
        )
        # Users go to No Access before the role they held is deleted.
        self.assertLess(calls.index(("PATCH", "/users/12")), calls.index(("DELETE", "/roles/5")))
        self.assertEqual([s["id"] for s in self.api.items["/sources"]], [3])
        self.assertEqual([c["id"] for c in self.api.items["/customers"]], [9])
        users = {u["username"]: u for u in self.api.items["/users"]}
        self.assertEqual(users["lt-user-001"]["role_id"], FakeApi.NO_ACCESS)
        self.assertFalse(users["lt-user-001"]["active"])
        self.assertTrue(users["admin"]["active"])
        self.assertEqual(self.api.settings["tail_interval_seconds"], 5)
        self.assertEqual(self.api.settings["theme"], "x")
        ledger = seed.Ledger()
        self.assertEqual(ledger.owned("sources"), {})
        self.assertEqual(set(ledger.owned("users")), {"lt-user-001", "lt-user-002"})
        self.assertIsNone(common.read_state("seeded.json"))

    def test_a_failure_is_reported_and_the_rest_goes_on(self) -> None:
        self.api.fail = {("DELETE", "/sources/1"), ("PATCH", "/users/11")}
        with self.assertRaisesRegex(common.KitError, "clean incomplete, 3 problem") as caught:
            self.clean()
        message = str(caught.exception)
        self.assertIn("source 'lt-ssh-001': 500 boom", message)
        self.assertIn("user 'lt-user-001': 500 boom", message)
        # lt-user-001 still holds the role, so PerchTail refuses to delete it.
        self.assertIn("role 'lt-role': 409", message)
        self.assertIn(("DELETE", "/sources/2"), self.api.calls)
        self.assertIn(("DELETE", "/users/12"), self.api.calls)
        self.assertEqual(self.api.settings["tail_interval_seconds"], 5)
        ledger = seed.Ledger()
        self.assertEqual(ledger.owned("sources"), {"lt-ssh-001": 1})
        self.assertEqual(ledger.owned("roles"), {"lt-role": 5})

    def test_main_exits_non_zero_when_clean_is_incomplete(self) -> None:
        self.api.fail = {("DELETE", "/sources/2")}
        with (
            mock.patch.object(sys, "argv", ["seed.py", "clean"]),
            mock.patch("sys.stdout"),
            mock.patch("sys.stderr"),
            self.assertRaises(SystemExit) as caught,
        ):
            seed.main()
        self.assertNotEqual(caught.exception.code, 0)


class SettingsTests(StateTestCase):
    def test_restore_puts_back_only_the_keys_that_differ(self) -> None:
        common.write_state(common.SETTINGS_STATE, {"a": 1, "b": 2})
        client = mock.Mock()
        client.get.return_value = {"a": 5, "b": 2, "c": 9}
        with mock.patch("sys.stdout"):
            seed.restore_settings(client)
        client.patch.assert_called_once_with("/system-settings", {"a": 1})
        self.assertIsNone(common.read_state(common.SETTINGS_STATE))

    def test_saved_values_stay_when_the_restore_fails(self) -> None:
        common.write_state(common.SETTINGS_STATE, {"a": 1})
        client = mock.Mock()
        client.get.return_value = {"a": 5}
        client.patch.side_effect = common.ApiError("PATCH", "/system-settings", 500, "boom")
        with self.assertRaises(common.ApiError):
            seed.restore_settings(client)
        self.assertEqual(common.read_state(common.SETTINGS_STATE), {"a": 1})

    def test_a_phase_setting_goes_back_for_the_phases_after_it(self) -> None:
        profile = {
            "settings": {"tail_interval_seconds": 2},
            "phases": [
                {"name": "a", "settings": {"concurrency_global": 32}},
                {"name": "b"},
                {"name": "c", "settings": {"tail_interval_seconds": 5}},
            ],
        }
        common.write_state(
            common.SETTINGS_STATE, {"tail_interval_seconds": 3, "concurrency_global": 16}
        )
        phases = profile["phases"]
        self.assertEqual(
            run.phase_settings(profile, phases[0]),
            {"tail_interval_seconds": 2, "concurrency_global": 32},
        )
        self.assertEqual(
            run.phase_settings(profile, phases[1]),
            {"tail_interval_seconds": 2, "concurrency_global": 16},
        )
        self.assertEqual(
            run.phase_settings(profile, phases[2]),
            {"tail_interval_seconds": 5, "concurrency_global": 16},
        )
        self.assertEqual(
            run.phase_settings(profile, {}),
            {"tail_interval_seconds": 2, "concurrency_global": 16},
        )


class SourceHostTests(StateTestCase):
    def test_sources_are_spread_over_the_hosts_round_robin(self) -> None:
        os.environ["LOADTEST_SSH_HOSTS"] = "ssh-a, ssh-b:2222"
        specs = [seed.source_spec({}, "ssh", i) for i in range(3)]
        self.assertEqual(
            [(s["host"], s["port"]) for s in specs], [("ssh-a", 22), ("ssh-b", 2222), ("ssh-a", 22)]
        )

    def test_profile_hosts_apply_when_the_variable_is_unset(self) -> None:
        profile = {"hosts": {"smb": ["smb-a", "[fd00::5]:1445"]}}
        specs = [seed.source_spec(profile, "smb", i) for i in range(2)]
        self.assertEqual(
            [(s["host"], s["port"]) for s in specs], [("smb-a", 445), ("fd00::5", 1445)]
        )

    def test_default_is_the_local_test_server(self) -> None:
        self.assertEqual(seed.source_spec({}, "ssh", 5)["host"], "lt-ssh")


class LogFilesTests(StateTestCase):
    def test_clean_files_needs_the_marker_and_keeps_other_files(self) -> None:
        root = common.logs_dir()
        (root / "shared").mkdir()
        (root / "mine.txt").write_text("keep", encoding="utf-8")
        with mock.patch("sys.stderr"):
            seed.clean_files()
        self.assertTrue((root / "shared").exists())
        (root / seed.LOGS_MARKER).write_text("x", encoding="utf-8")
        seed.clean_files()
        self.assertFalse((root / "shared").exists())
        self.assertTrue((root / "mine.txt").exists())

    def _small_profile(self) -> Path:
        path = Path(self.tmp.name) / "small-test.json"
        path.write_text(
            json.dumps(
                {
                    "name": "small-test",
                    "files": {"sizes_kb": [1, 2], "gz": 1, "zip": 1, "growing": 1},
                    "agents": {"running": 1, "stopped": 0},
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_generate_out_needs_no_instance(self) -> None:
        profile = self._small_profile()
        out = Path(self.tmp.name) / "served"
        del os.environ["LOADTEST_BASE_URL"]
        del os.environ["LOADTEST_ALLOWED_HOSTS"]
        argv = ["gen_logs.py", "generate", "--profile", str(profile), "--out", str(out)]
        with mock.patch.object(sys, "argv", argv), mock.patch("sys.stdout"):
            gen_logs.main()
        self.assertTrue((out / "app-01" / "service-1k-1.log").exists())
        self.assertTrue((out / "live" / "live-1.log").exists())
        self.assertFalse(list(Path(self.tmp.name).rglob("catalog.json")))
        self.assertFalse((common.logs_dir() / gen_logs.LOGS_MARKER).exists())

    def test_generate_without_out_and_catalog_still_need_the_instance(self) -> None:
        profile = self._small_profile()
        del os.environ["LOADTEST_BASE_URL"]
        for command in (["generate"], ["catalog"]):
            argv = ["gen_logs.py", *command, "--profile", str(profile)]
            with (
                self.subTest(command=command[0]),
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(gen_logs, "fail", side_effect=SystemExit(1)) as failed,
                self.assertRaises(SystemExit),
            ):
                gen_logs.main()
            self.assertIn("LOADTEST_BASE_URL is required", failed.call_args.args[0])


class _Handler(http.server.BaseHTTPRequestHandler):
    """Answers like the test sets it up, and records what it was sent."""

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        server.seen.append(self.headers.get("Authorization"))  # type: ignore[attr-defined]
        status, headers, body = server.reply  # type: ignore[attr-defined]
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


class CollectorHealthTests(unittest.TestCase):
    def _server(self, status: int, headers: dict, body: bytes = b"") -> str:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.seen, server.reply = [], (status, headers, body)  # type: ignore[attr-defined]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.servers = getattr(self, "servers", []) + [server]
        return f"http://127.0.0.1:{server.server_address[1]}"

    def test_reads_the_health_report_with_the_token(self) -> None:
        url = self._server(200, {"Content-Type": "application/json"}, b'{"status": "ok"}')
        self.assertEqual(collect.read_health(url, "tok"), {"status": "ok"})
        self.assertEqual(self.servers[0].seen, ["Bearer tok"])

    def test_a_redirect_is_an_error_and_the_token_stays_put(self) -> None:
        other = self._server(200, {"Content-Type": "application/json"}, b'{"status": "ok"}')
        url = self._server(302, {"Location": f"{other}/monitoring/health"})
        with self.assertRaisesRegex(common.KitError, r"redirect \(HTTP 302 to http://127"):
            collect.read_health(url, "tok")
        self.assertEqual(self.servers[1].seen, ["Bearer tok"])
        self.assertEqual(self.servers[0].seen, [])

    def test_other_errors_read_as_unreachable(self) -> None:
        url = self._server(401, {})
        self.assertIsNone(collect.read_health(url, "tok"))
        self.assertEqual(collect.health_fields(None)["health_status"], "unreachable")


class AgentTests(StateTestCase):
    def test_agents_get_only_what_they_need(self) -> None:
        os.environ["LOADTEST_ADMIN_PASSWORD"] = "secret"
        os.environ["LOADTEST_SSH_PASSWORD"] = "secret"
        os.environ["PATH"] = os.environ.get("PATH", "/usr/bin")
        env = agents.agent_env("ws://localhost:8080/agent/connect", "tok", Path("/x"))
        self.assertNotIn("LOADTEST_ADMIN_PASSWORD", env)
        self.assertNotIn("LOADTEST_SSH_PASSWORD", env)
        self.assertEqual(env["PERCHTAIL_AGENT_TOKEN"], "tok")
        self.assertEqual(env["PERCHTAIL_ALLOW_INSECURE"], "true")
        self.assertIn("PATH", env)

    def test_a_pid_running_the_kit_binary_is_the_kit_agent(self) -> None:
        # This test's own process stands in for an agent: point the kit's
        # binary path at the program it runs.
        with mock.patch.object(agents, "binary_path", return_value=Path(sys.executable)):
            self.assertTrue(agents._is_kit_agent(os.getpid()))

    def test_a_pid_running_another_program_is_not_the_kit_agent(self) -> None:
        self.assertFalse(agents._is_kit_agent(os.getpid()))  # this Python
        self.assertTrue(agents._alive(os.getpid()))


class RampVerdictTests(unittest.TestCase):
    PROFILE = {
        "scenarios": {
            "ramp": {
                "start_vus": 2,
                "step_vus": 2,
                "steps": 3,
                "step_seconds": 40,
                "warmup_seconds": 15,
                "min_requests": 20,
                "thresholds": {"p95_ms": 1000, "error_rate": 0.01},
            }
        }
    }

    @staticmethod
    def summary(run_seconds: float, steps: dict[int, tuple[int, float, float]]) -> dict:
        metrics = {}
        for n, (count, p95, rate) in steps.items():
            metrics[f"http_req_duration{{scenario:ramp,step:{n},stage:steady,flow:ui}}"] = {
                "values": {"count": count, "p(95)": p95}
            }
            metrics[f"http_req_failed{{scenario:ramp,step:{n},stage:steady}}"] = {
                "values": {"rate": rate}
            }
        return {"metrics": metrics, "state": {"testRunDurationMs": run_seconds * 1000}}

    def verdict(self, data: dict) -> str:
        return "\n".join(report.ramp_markdown("ramp", data, self.PROFILE))

    def test_a_step_past_a_limit_breaks_even_with_too_few_requests(self) -> None:
        # The server collapses at step 2: requests hang, few complete, and
        # those that do are slow or fail. That is the breaking point, not a
        # step to skip on the way to "not reached".
        data = self.summary(150, {1: (50, 300, 0), 2: (5, 5000, 0.5), 3: (60, 300, 0)})
        rows = report.ramp_rows(data, self.PROFILE)
        self.assertEqual([r["breaks"] for r in rows], [False, True, False])
        text = self.verdict(data)
        self.assertIn("too few requests, and past a limit", text)
        self.assertIn("Breaking point: step 2, 4 concurrent users", text)
        self.assertNotIn("not reached", text)

    def test_fewer_requests_than_the_step_before_breaks(self) -> None:
        # Within the limits, but step 2 completed fewer requests than step 1
        # with more users: the server stopped keeping up.
        data = self.summary(150, {1: (50, 300, 0), 2: (12, 400, 0), 3: (60, 300, 0)})
        rows = report.ramp_rows(data, self.PROFILE)
        self.assertEqual([r["dropped"] for r in rows], [False, True, False])
        text = self.verdict(data)
        self.assertIn("fewer than the step before's 50", text)
        self.assertIn("Breaking point: step 2, 4 concurrent users", text)
        self.assertNotIn("not reached", text)

    def test_the_first_counted_step_past_a_limit_is_the_breaking_point(self) -> None:
        data = self.summary(150, {1: (50, 300, 0), 2: (90, 1500, 0), 3: (120, 2500, 0)})
        text = self.verdict(data)
        self.assertIn("Breaking point: step 2, 4 concurrent users", text)

    def test_too_few_requests_within_limits_and_growing_is_skipped(self) -> None:
        data = self.summary(150, {1: (10, 300, 0), 2: (15, 300, 0), 3: (60, 300, 0)})
        rows = report.ramp_rows(data, self.PROFILE)
        self.assertEqual([r["skipped"] for r in rows], [True, True, False])
        text = self.verdict(data)
        self.assertIn("too few requests to judge", text)
        self.assertIn("not reached: within limits up to 6 concurrent users", text)

    def test_a_step_cut_short_does_not_count(self) -> None:
        data = self.summary(120, {1: (50, 300, 0), 2: (50, 300, 0), 3: (50, 9000, 0)})
        rows = report.ramp_rows(data, self.PROFILE)
        self.assertEqual([r["complete"] for r in rows], [True, True, False])
        text = "\n".join(report.ramp_markdown("ramp", data, self.PROFILE))
        self.assertIn("not reached: within limits up to 4 concurrent users", text)


if __name__ == "__main__":
    unittest.main()
