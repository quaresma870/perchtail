"""Checks an upgraded install against what seed.py recorded before the
upgrade, for the upgrade test in .github/workflows/ci.yml.

    python verify.py --base-url URL --container NAME --before before.json \
        --secrets secrets.json --out after.json
    python verify.py downgrade --volume VOLUME --previous-image IMAGE \
        --new-image IMAGE --env-file FILE

The first form needs the upgraded version running in --container; the
second needs it stopped.

Every field seed.py recorded must come back with the same value, except the
differences the CHANGELOG announces, listed in EXPECTED below with the entry
that announces each, and the runtime state in VOLATILE. Any other
difference fails. Fields the new version adds are listed, not failed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from client import ApiError, Client, TotpDevice

ADMIN = "admin"
OPERATOR = "upgrade-operator"
MISSING = object()


@dataclass(frozen=True)
class Expected:
    """A difference the CHANGELOG announces. `where` matches the endpoint,
    `field` the field's path below it (fnmatch, `*` for list positions and
    ids), and `check(before, after)` says whether the new value is the one
    announced. `changelog` quotes the entry."""

    where: str
    field: str
    check: Callable[[object, object], bool]
    changelog: str


def removed(before: object, after: object) -> bool:
    return after is MISSING


def added_manage_source(before: object, after: object) -> bool:
    return isinstance(before, list) and sorted(after) == sorted([*before, "manage_source"])


EXPECTED: list[Expected] = [
    Expected(
        "/sources*",
        "*search_indexing_enabled",
        removed,
        'CHANGELOG [Unreleased] ### Upgrading: "The content index and the Search page are gone'
        ' (#303)"; ### Removed: "the per-source \\"indexed for search\\" option and the Search'
        ' setting (#303)"',
    ),
    Expected(
        "/system-settings",
        "search_view_enabled",
        removed,
        'CHANGELOG [Unreleased] ### Upgrading: "The content index and the Search page are gone'
        ' (#303)"; ### Removed: "the Search setting (#303)"',
    ),
    Expected(
        "/system-settings",
        "audit_retention_days",
        lambda b, a: isinstance(b, int) and 1 <= b <= 89 and a == 90,
        'CHANGELOG [Unreleased] ### Upgrading: "Audit retention is at least 90 days (#252, #383)'
        ' [...] The migration raises a retention set between 1 and 89 days to 90"',
    ),
    Expected(
        "/alerts",
        "*.webhook_url",
        removed,
        'CHANGELOG [Unreleased] ### Upgrading: "Alert webhook URLs are encrypted and never'
        " returned. [...] The alerts API returns `webhook_url_masked` (scheme, host and port)"
        ' instead of `webhook_url`"',
    ),
    Expected(
        "/roles/*/grants",
        "*.capabilities",
        added_manage_source,
        'CHANGELOG [Unreleased] ### Upgrading: "Manage sources is a capability of its own. The'
        " migration keeps what every role could do: in each role with the `create_source` global"
        ' capability, every grant that has `manage_rules` also gets `manage_source`."',
    ),
    Expected(
        "/sources*",
        "*my_capabilities",
        added_manage_source,
        'CHANGELOG [Unreleased] ### Upgrading: "Manage sources is a capability of its own." The'
        " admin's own capabilities on each source include it, as a super admin holds every one.",
    ),
    Expected(
        "operator_view",
        "sources.*",
        added_manage_source,
        'CHANGELOG [Unreleased] ### Upgrading: "Manage sources is a capability of its own. [...]'
        ' every grant that has `manage_rules` also gets `manage_source`." (the operator holds'
        " manage_rules on the folder Region North)",
    ),
    Expected(
        "audit",
        "*.timestamp",
        lambda b, a: isinstance(a, str) and a.endswith("Z") and a[:-1].startswith(str(b)[:19]),
        'CHANGELOG [Unreleased] ### Changed: "Audit times are unambiguous: the audit API returns'
        ' them in UTC with their zone (`Z`)"',
    ),
]

# Runtime state, not data the upgrade carries over: it changes by signing in
# and by the background jobs, whatever the version.
VOLATILE = [
    ("/users", "*.last_login_at"),  # verify.py signs in
    ("/alerts", "*.last_checked_at"),  # the alert pass runs every minute
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.expected: list[tuple[int, str]] = []
        self.added: list[str] = []
        self.passed: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)
        print(f"FAIL  {message}")

    def ok(self, message: str) -> None:
        self.passed.append(message)
        print(f"ok    {message}")


def _match(rules, where: str, field: str):
    for rule in rules:
        rw, rf = (rule.where, rule.field) if isinstance(rule, Expected) else rule
        if fnmatch.fnmatchcase(where, rw) and fnmatch.fnmatchcase(field, rf):
            yield rule


def compare(report: Report, where: str, before: object, after: object, field: str = "") -> None:
    """Every value of `before` must be in `after`. Lists of objects with an
    id are matched by id, other lists of objects or lists by position, and
    lists of plain values as a whole."""
    if isinstance(before, dict) and isinstance(after, dict):
        for key, value in before.items():
            sub = f"{field}.{key}" if field else key
            if key in after:
                compare(report, where, value, after[key], sub)
            else:
                differ(report, where, sub, value, MISSING)
        report.added += [
            f"{where} {field + '.' if field else ''}{k}" for k in after.keys() - before.keys()
        ]
        return
    if isinstance(before, list) and isinstance(after, list) and before:
        if all(isinstance(x, dict) and "id" in x for x in before):
            by_id = {x.get("id"): x for x in after if isinstance(x, dict)}
            for item in before:
                sub = f"{field}.{item['id']}" if field else str(item["id"])
                if item["id"] in by_id:
                    compare(report, where, item, by_id[item["id"]], sub)
                else:
                    differ(report, where, sub, item, MISSING)
            return
        if all(isinstance(x, (dict, list)) for x in before) and len(before) == len(after):
            for i, (b, a) in enumerate(zip(before, after, strict=True)):
                compare(report, where, b, a, f"{field}.{i}" if field else str(i))
            return
    if before != after:
        differ(report, where, field, before, after)


def differ(report: Report, where: str, field: str, before: object, after: object) -> None:
    if any(True for _ in _match(VOLATILE, where, field)):
        return
    for rule in _match(EXPECTED, where, field):
        if rule.check(before, after):
            report.expected.append(
                (EXPECTED.index(rule), f"{where} {field}: {short(before)} -> {short(after)}")
            )
            return
    report.fail(f"{where} {field or '(whole)'}: {short(before)} -> {short(after)}")


def short(value: object) -> str:
    if value is MISSING:
        return "(missing)"
    text = json.dumps(value, default=str)
    return text if len(text) <= 120 else text[:117] + "..."


def sign_in(base_url: str, username: str, secrets: dict, report: Report) -> Client:
    """Signs in with the password seed.py set. When the new version requires
    MFA of the account, sets up TOTP first (or reuses what an earlier run
    set up, kept in the secrets file) and signs in with a code.

    CHANGELOG [Unreleased] ### Upgrading: "Administrators must use MFA. Local
    accounts of super admins and of roles with any global capability must set
    up two-factor authentication: at their first sign-in after upgrading,
    they can only set it up." Both seeded accounts are such accounts."""
    password = secrets[username]
    devices = secrets.setdefault("totp", {})
    client = Client(base_url)
    if username in devices:
        device = TotpDevice(devices[username]["secret"], devices[username]["last"])
        client.post(
            "/auth/login",
            {"username": username, "password": password, "mfa_code": device.code()},
        )
        devices[username]["last"] = device.last_counter
        client.get("/sources")
        report.ok(f"{username} signed in with the password and an MFA code")
        return client
    client.post("/auth/login", {"username": username, "password": password})
    try:
        client.get("/sources")
        report.ok(f"{username} signed in with the password set before the upgrade")
        return client
    except ApiError as exc:
        if exc.status != 403 or "mfa_setup_required" not in exc.body:
            raise
    enrollment = client.post("/auth/mfa/enroll", {"password": password})
    device = TotpDevice(enrollment["secret"])
    client.post("/auth/mfa/confirm", {"code": device.code()})
    devices[username] = {"secret": device.secret, "last": device.last_counter}
    client.post("/auth/logout", expect=204)
    without_code = Client(base_url)
    try:
        without_code.post("/auth/login", {"username": username, "password": password})
        without_code.get("/sources")
        report.fail(f"{username} signed in without an MFA code after setting MFA up")
    except ApiError:
        pass
    client = Client(base_url)
    client.post(
        "/auth/login", {"username": username, "password": password, "mfa_code": device.code()}
    )
    devices[username]["last"] = device.last_counter
    client.get("/sources")
    report.ok(
        f"{username} signed in with the password set before the upgrade, set up MFA"
        " (required by the new version) and signed in again with a code"
    )
    return client


def docker_exec(container: str, *args: str, user: str | None = None) -> str:
    result = subprocess.run(
        ["docker", "exec", *(["-u", user] if user else []), container, *args],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker exec {container} {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout + result.stderr


def check_migrations(report: Report, container: str) -> None:
    """The database is at the repository's head. `alembic current` can't
    run next to the app (it takes the instance lock, which refuses a second
    process on DATA_DIR), so the revision is read from the database,
    read-only."""
    heads = re.findall(
        r"^([0-9a-f]{12}) \(head\)",
        docker_exec(container, "python", "-m", "alembic", "heads"),
        re.M,
    )
    current = docker_exec(
        container,
        "python",
        "-c",
        "import sqlite3; db = sqlite3.connect('file:/data/db/perchtail.db?mode=ro', uri=True);"
        " print(' '.join(r[0] for r in db.execute('select version_num from alembic_version')))",
    ).split()
    if len(heads) == 1 and current == heads:
        report.ok(f"database at the repository's head {heads[0]}")
    else:
        report.fail(f"database revision {current}, repository heads {heads}")


def check_operator(report: Report, base_url: str, secrets: dict, before: dict) -> dict:
    client = sign_in(base_url, OPERATOR, secrets, report)
    view = {
        "customers": sorted(c["name"] for c in client.get("/customers")),
        "folders": sorted(f["name"] for f in client.get("/folders")),
        "sources": {
            s["name"]: sorted(s.get("my_capabilities", [])) for s in client.get("/sources")
        },
    }
    failures = len(report.failures)
    compare(report, "operator_view", before["operator_view"], view)
    if len(report.failures) == failures:
        report.ok(f"{OPERATOR} sees what the grants give: {view}")
    return view


def check_secrets(report: Report, api: Client, before: dict) -> None:
    """Encrypted values are still readable where the API shows them."""
    sources = {s["name"]: s for s in api.get("/sources")}
    for name in ("upgrade-ssh", "upgrade-smb", "upgrade-winrm"):
        if sources.get(name, {}).get("has_credential") is True:
            report.ok(f"{name} still has its saved credential")
        else:
            report.fail(f"{name} lost its saved credential")
    if sources.get("upgrade-agent", {}).get("has_agent_token") is True:
        report.ok("upgrade-agent still has its enrollment token")
    else:
        report.fail("upgrade-agent lost its enrollment token")
    providers = api.get("/sso")
    seeded = before["endpoints"]["/sso"]
    if (
        len(providers) == 1
        and providers[0]["issuer"] == seeded[0]["issuer"]
        and providers[0]["client_id"] == seeded[0]["client_id"]
        and api.get("/auth/sso/status").get("enabled") is True
        and providers[0].get("secret_unreadable", False) is False
    ):
        report.ok("SSO provider decrypts (issuer, client id, secret) and is still enabled")
    else:
        report.fail(f"SSO provider changed or unreadable: {providers}")
    alerts = api.get("/alerts")
    if alerts and alerts[0].get("webhook_url_masked") == "https://example.com/\u2026":
        report.ok("alert webhook URL encrypted by the migration and decrypts to its host")
    else:
        report.fail(f"alert webhook URL unreadable: {alerts}")
    if api.get("/monitoring/token").get("configured") is True:
        report.ok("monitoring token still configured")
    else:
        report.fail("monitoring token lost")


def check_announced_values(report: Report, api: Client) -> None:
    """Values the migrations give existing data, as the CHANGELOG announces
    them. A field the new version adds is otherwise not compared."""
    # CHANGELOG [Unreleased] ### Upgrading: "SMB signing is a setting of each SMB source.
    # PerchTail always required signed SMB messages, and every source, new or existing,
    # keeps requiring them."
    smb = [s for s in api.get("/sources") if s["protocol"] == "smb"]
    if smb and all(s.get("smb_require_signing") is True for s in smb):
        report.ok("existing SMB sources still require signing")
    else:
        report.fail(f"SMB signing not required on an existing source: {smb}")
    # CHANGELOG [Unreleased] ### Upgrading: "The scheduled backup is on by default for new
    # installs [...] An existing install keeps what it had: the migration stores the old
    # default (off) where the interval was never set."
    interval = api.get("/system-settings").get("backup_interval_hours")
    if interval == 0:
        report.ok("the scheduled backup stays off on the upgraded install")
    else:
        report.fail(f"backup_interval_hours is {interval!r} on the upgraded install, not 0")


def audit_entries(api: Client) -> list[dict]:
    entries: list[dict] = []
    while True:
        body = api.get(f"/audit?limit=100&offset={len(entries)}")
        entries += body["items"]
        if len(entries) >= body["total"] or not body["items"]:
            return entries


def check_audit(report: Report, api: Client, before: dict) -> list[dict]:
    status = api.post("/audit/integrity/verify")
    if status.get("status") == "ok":
        report.ok(f"audit hash chain verifies: {status}")
    else:
        report.fail(f"audit hash chain does not verify: {status}")
    entries = audit_entries(api)
    seeded_ids = {e["id"] for e in before["audit"]}
    retention = before["endpoints"]["/system-settings"]["audit_retention_days"]
    if 1 <= retention <= 89:
        raised = [
            e
            for e in entries
            if e["id"] not in seeded_ids
            and e["action"] == "system_settings.update"
            and e["user_id"] is None
            and str(retention) in json.dumps(e.get("metadata"))
            and "90" in json.dumps(e.get("metadata"))
        ]
        # CHANGELOG [Unreleased] ### Upgrading: "the first start after it records that in
        # the audit log as a `system_settings.update` with no user, with the old and new
        # values."
        if raised:
            report.ok(f"the retention raised to 90 days is in the audit log: {raised[0]}")
        else:
            report.fail("no system_settings.update with no user records the retention raise")
    failures = len(report.failures)
    compare(report, "audit", before["audit"], entries)
    if len(report.failures) == failures:
        report.ok(f"all {len(before['audit'])} audit entries from before the upgrade are there")
    return entries


def check_backup(report: Report, container: str) -> None:
    """Both run as PerchTail's user, as the backup tool refuses root."""
    try:
        out = docker_exec(container, "python", "-m", "app.backup", "create", user="perchtail")
        match = re.search(r"^Backup written: (\S+)$", out, re.M)
        if not match:
            report.fail(f"app.backup create didn't say where it wrote the backup:\n{out}")
            return
        docker_exec(
            container, "python", "-m", "app.backup", "verify", match.group(1), user="perchtail"
        )
    except RuntimeError as exc:
        report.fail(str(exc))
        return
    report.ok(f"app.backup create and verify work in the container ({match.group(1)})")


def save_secrets(path: str, secrets: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(secrets, fh)


def checks(report: Report, args: argparse.Namespace, before: dict, secrets: dict) -> dict:
    check_migrations(report, args.container)
    api = sign_in(args.base_url, ADMIN, secrets, report)

    after: dict = {"endpoints": {}}
    for endpoint, value in before["endpoints"].items():
        try:
            after["endpoints"][endpoint] = api.get(endpoint)
        except ApiError as exc:
            report.fail(f"{endpoint}: {exc}")
            continue
        failures = len(report.failures)
        compare(report, endpoint, value, after["endpoints"][endpoint])
        if len(report.failures) == failures:
            report.ok(f"{endpoint} unchanged but for the expected differences")

    after["operator_view"] = check_operator(report, args.base_url, secrets, before)
    check_secrets(report, api, before)
    check_announced_values(report, api)
    after["audit"] = check_audit(report, api, before)
    check_backup(report, args.container)
    return after


def run(args: argparse.Namespace) -> int:
    report = Report()
    with open(args.before, encoding="utf-8") as fh:
        before = json.load(fh)
    with open(args.secrets, encoding="utf-8") as fh:
        secrets = json.load(fh)
    after: dict = {}
    try:
        after = checks(report, args, before, secrets)
    except Exception as exc:  # noqa: BLE001 - reported as a failure, with the cause
        report.fail(f"stopped early: {type(exc).__name__}: {exc}")
    finally:
        save_secrets(args.secrets, secrets)

    after["expected_differences"] = [text for _, text in report.expected]
    after["added_fields"] = sorted(set(report.added))
    after["failures"] = report.failures
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(after, fh, indent=2, sort_keys=True, default=str)

    print(f"\n{len(report.expected)} expected differences, by CHANGELOG entry:")
    for index, rule in enumerate(EXPECTED):
        seen = [text for i, text in report.expected if i == index]
        if seen:
            print(f"  {len(seen):3} x {rule.where} {rule.field}, e.g. {seen[0]}")
            print(f"        {rule.changelog}")
    added = sorted({re.sub(r"(?<![^ .])\d+(?=\.|$| )", "<id>", a) for a in report.added})
    print(f"{len(added)} fields added by the new version (not compared):")
    for line in added:
        print(f"  {line}")
    if report.failures:
        print(f"\nUPGRADE TEST FAILED: {len(report.failures)} problem(s):")
        for line in report.failures:
            print(f"  - {line}")
        annotate(f"Upgrade from {before.get('version')} failed: {report.failures[0]}")
        return 1
    print(f"\nupgrade test passed: {len(report.passed)} checks")
    return 0


def annotate(message: str) -> None:
    """An error annotation on the run's summary page, under GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        text = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Upgrade test::{text}")


# --- downgrade -------------------------------------------------------------

# The refusal a previous release gives on a database migrated by a newer one:
# from 0.7.0 on, PerchTail's own check (app/schema_version.py); before it,
# Alembic in the entrypoint, which doesn't know the new head revision.
NEWER_DATABASE = [
    r"startup\.database_newer_than_app",
    r"Can't locate revision identified by '[0-9a-f]{12}'",
]


def volume_digest(volume: str, image: str) -> str:
    out = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            f"{volume}:/data",
            image,
            "-c",
            "cd /data/db && sha256sum perchtail.db* credential_salt",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    ).stdout
    return hashlib.sha256(out.encode()).hexdigest()


def downgrade(args: argparse.Namespace) -> int:
    """Starting the previous release on the upgraded volume must fail at
    startup, saying the database is newer, without changing it."""
    digest = volume_digest(args.volume, args.new_image)
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--name",
                "perchtail-downgrade",
                "--env-file",
                args.env_file,
                "-v",
                f"{args.volume}:/data",
                args.previous_image,
            ],
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        code, output = result.returncode, result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        subprocess.run(["docker", "rm", "-f", "perchtail-downgrade"], capture_output=True)
        code = None
        output = (exc.stdout or b"").decode() + (exc.stderr or b"").decode()
    with open(args.log, "w", encoding="utf-8") as fh:
        fh.write(output)
    problems = []
    if code in (None, 0):
        problems.append(
            "the previous release started on the upgraded database"
            + (" and kept running" if code is None else " and exited cleanly")
        )
    if not any(re.search(p, output) for p in NEWER_DATABASE):
        problems.append("its output doesn't say the database is from a newer version")
    if volume_digest(args.volume, args.new_image) != digest:
        problems.append("the attempt changed the database or the salt")
    if problems:
        print("DOWNGRADE REFUSAL CHECK FAILED:")
        for p in problems:
            print(f"  - {p}")
        print(f"  (exit code {code}; output in {args.log})")
        annotate("Downgrade refusal check failed: " + "; ".join(problems))
        return 1
    print(f"ok    the previous release refused to start (exit code {code}), data unchanged")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    down = sub.add_parser("downgrade")
    down.add_argument("--volume", required=True)
    down.add_argument("--previous-image", required=True)
    down.add_argument("--new-image", required=True)
    down.add_argument("--env-file", required=True)
    down.add_argument("--log", default="downgrade.log")
    down.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--base-url")
    parser.add_argument("--container")
    parser.add_argument("--before")
    parser.add_argument("--secrets")
    parser.add_argument("--out")
    args = parser.parse_args()
    if args.command == "downgrade":
        return downgrade(args)
    missing = [
        n for n in ("base_url", "container", "before", "secrets", "out") if not getattr(args, n)
    ]
    if missing:
        parser.error("missing " + ", ".join("--" + n.replace("_", "-") for n in missing))
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
