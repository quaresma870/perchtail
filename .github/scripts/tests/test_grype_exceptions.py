import datetime as dt
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grype_exceptions  # noqa: E402

TODAY = dt.date(2026, 11, 21)


def config(*rules: tuple[str, str]) -> str:
    lines = ["ignore:"]
    for vuln, reason in rules:
        lines += [f"  - vulnerability: {vuln}", "    package:", "      name: python"]
        lines.append(f'    reason: "{reason}"')
    return "\n".join(lines) + "\n"


class GrypeExceptionsTests(unittest.TestCase):
    def run_main(self, text: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, ".grype.yaml")
            path.write_text(text, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                code = grype_exceptions.main(["--config", str(path), "--today", TODAY.isoformat()])
            return code, out.getvalue()

    def test_exceptions_before_their_review_date_pass(self):
        code, out = self.run_main(config(("CVE-1", "Not used. Review by 2026-12-31.")))
        self.assertEqual(code, 0)
        self.assertIn("1 Grype exceptions", out)

    def test_an_exception_past_its_review_date_fails(self):
        code, out = self.run_main(
            config(
                ("CVE-1", "Not used. Review by 2026-12-31."),
                ("CVE-2", "Old. Review by 2026-11-20."),
            )
        )
        self.assertEqual(code, 1)
        self.assertIn("CVE-2", out)
        self.assertNotIn("CVE-1:", out)

    def test_an_exception_without_a_review_date_fails(self):
        code, out = self.run_main(config(("CVE-1", "No date here.")))
        self.assertEqual(code, 1)
        self.assertIn("Review by", out)

    def test_the_repository_exceptions_are_well_formed(self):
        text = grype_exceptions.DEFAULT_CONFIG.read_text(encoding="utf-8")
        entries = grype_exceptions.exceptions(text)
        self.assertTrue(entries)
        for entry in entries:
            self.assertRegex(entry["reason"], r"Review by \d{4}-\d{2}-\d{2}\.")


if __name__ == "__main__":
    unittest.main()
