import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import npm_audit  # noqa: E402

TODAY = dt.date(2026, 11, 17)


def advisory(ghsa: str, package: str, severity: str) -> dict:
    return {
        "name": package,
        "severity": severity,
        "title": f"{package} is vulnerable",
        "url": f"https://github.com/advisories/{ghsa}",
    }


def report(*vias: dict) -> dict:
    vulnerabilities = {}
    for via in vias:
        vulnerabilities[via["name"]] = {
            "name": via["name"],
            "severity": via["severity"],
            "via": [via],
        }
    # A package vulnerable only through another one carries no advisory.
    vulnerabilities["vite"] = {"name": "vite", "severity": "high", "via": ["picomatch"]}
    return {"vulnerabilities": vulnerabilities}


def exception(ghsa: str, package: str, expires: str) -> dict:
    return {"id": ghsa, "package": package, "reason": "not shipped", "expires": expires}


class NpmAuditTests(unittest.TestCase):
    def run_main(
        self, audit: dict, exceptions: list, level: str = "high", extra: tuple = ()
    ) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp, "report.json")
            exceptions_path = Path(tmp, "exceptions.json")
            report_path.write_text(json.dumps(audit), encoding="utf-8")
            exceptions_path.write_text(json.dumps(exceptions), encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = npm_audit.main(
                    [
                        "--level",
                        level,
                        "--report",
                        str(report_path),
                        "--exceptions",
                        str(exceptions_path),
                        "--today",
                        TODAY.isoformat(),
                        *extra,
                    ]
                )
            return code, out.getvalue()

    def test_an_advisory_without_an_exception_fails(self):
        code, out = self.run_main(report(advisory("GHSA-aaaa", "picomatch", "high")), [])
        self.assertEqual(code, 1)
        self.assertIn("GHSA-aaaa (picomatch)", out)

    def test_only_excepted_advisories_pass_and_are_listed(self):
        code, out = self.run_main(
            report(advisory("GHSA-aaaa", "picomatch", "high")),
            [exception("GHSA-aaaa", "picomatch", "2026-12-15")],
        )
        self.assertEqual(code, 0)
        self.assertIn("Accepted until 2026-12-15: GHSA-aaaa (picomatch)", out)

    def test_an_expired_exception_fails(self):
        code, out = self.run_main(
            report(advisory("GHSA-aaaa", "picomatch", "high")),
            [exception("GHSA-aaaa", "picomatch", "2026-11-16")],
        )
        self.assertEqual(code, 1)
        self.assertIn("expired on 2026-11-16", out)

    def test_an_exception_matching_no_advisory_fails(self):
        code, out = self.run_main(report(), [exception("GHSA-aaaa", "picomatch", "2026-12-15")])
        self.assertEqual(code, 1)
        self.assertIn("matches no advisory any more", out)

    def test_an_unused_exception_is_no_failure_on_another_branch(self):
        code, _ = self.run_main(
            report(),
            [exception("GHSA-aaaa", "picomatch", "2026-12-15")],
            extra=("--other-branch",),
        )
        self.assertEqual(code, 0)

    def test_an_advisory_below_the_level_passes(self):
        code, _ = self.run_main(report(advisory("GHSA-bbbb", "left-pad", "moderate")), [])
        self.assertEqual(code, 0)

    def test_another_advisory_still_fails_next_to_an_excepted_one(self):
        code, out = self.run_main(
            report(
                advisory("GHSA-aaaa", "picomatch", "high"),
                advisory("GHSA-cccc", "braces", "critical"),
            ),
            [exception("GHSA-aaaa", "picomatch", "2026-12-15")],
        )
        self.assertEqual(code, 1)
        self.assertIn("GHSA-cccc (braces)", out)
        self.assertNotIn("GHSA-aaaa (picomatch): picomatch is vulnerable", out)

    def test_the_repository_exceptions_are_well_formed(self):
        exceptions = json.loads(npm_audit.DEFAULT_EXCEPTIONS.read_text(encoding="utf-8"))
        for entry in exceptions:
            self.assertEqual(set(entry), {"id", "package", "reason", "expires"})
            self.assertTrue(entry["id"].startswith("GHSA-"))
            dt.date.fromisoformat(entry["expires"])
            self.assertTrue(entry["reason"].strip())


if __name__ == "__main__":
    unittest.main()
