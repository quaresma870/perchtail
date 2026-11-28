"""Fills a fresh install of the previous release through its API, for the
upgrade test in .github/workflows/ci.yml.

    docker logs perchtail-previous 2>&1 | python seed.py --base-url URL \
        --out before.json --secrets secrets.json

stdin carries the first super admin's temporary password: either the
contents of DATA_DIR/initial-admin-password, or the container log of a
release that still logs it (the `initial_super_admin.seeded` line).

It changes that password and sets up the two-factor authentication the
release requires of administrators, then creates a customer, nested
folders, a source of each protocol with rules (the push agent's with its
enrollment token), two roles with grants, a local user (an administrator,
with MFA too), the SSO provider and its group mappings, severity patterns,
redaction on with a source's own rule and a system rule one source turns
off, an SMB source with non-default signing and encryption, an alert with
a webhook, the system SMTP server, the monitoring token, a non-default
value for every system setting of 0.6 and 0.7 but audit_view_enabled (the
check reads the audit log) and password_reset_enabled (it needs email and
an https PUBLIC_BASE_URL), MFA for every local account among them, and
non-default agent upgrade settings. It then reads every object back
and writes that snapshot to --out, which verify.py compares with what the
new version returns after the upgrade. Passwords, TOTP secrets and two of
the operator's MFA recovery codes go to --secrets, a separate file that is
never uploaded. Names are fictional.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys

from client import ApiError, Client, TotpDevice

ADMIN = "admin"
OPERATOR = "upgrade-operator"


def strong_password() -> str:
    """Passes the password policy seed sets: long, every character class,
    nothing simple or personal."""
    alphabet = string.ascii_letters + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(28))
    return f"Up{body}7#x"


def temporary_password(text: str) -> str:
    for line in text.splitlines():
        if "initial_super_admin.seeded" in line:
            try:
                return json.loads(line[line.index("{") :])["temporary_password"]
            except (ValueError, KeyError) as exc:
                raise SystemExit(f"cannot read the temporary password from: {line}") from exc
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) == 1:
        return lines[0]
    raise SystemExit("stdin has neither the password file nor an initial_super_admin line")


def sign_in_and_change(
    base_url: str, username: str, old: str, new: str, devices: dict, backup_codes: dict
) -> Client:
    """Signs in with the temporary password, changes it, and signs in with
    the new one. An account the release requires MFA of (a super admin, a
    role with a global capability, every local account with
    require_mfa_local_accounts) can then only set it up: it does, keeps the
    TOTP secret in `devices` and the recovery codes in `backup_codes`, and
    signs in again with a code."""
    client = Client(base_url)
    me = client.post("/auth/login", {"username": username, "password": old})
    if not me.get("must_change_password"):
        raise SystemExit(f"{username} was expected to have to change their password")
    client.post("/auth/change-password", {"current_password": old, "new_password": new})
    client.post("/auth/logout", expect=204)
    client = Client(base_url)
    client.post("/auth/login", {"username": username, "password": new})
    try:
        client.get("/sources")
        return client
    except ApiError as exc:
        if exc.status != 403 or "mfa_setup_required" not in exc.body:
            raise
    enrollment = client.post("/auth/mfa/enroll", {"password": new})
    device = TotpDevice(enrollment["secret"])
    confirmed = client.post("/auth/mfa/confirm", {"code": device.code()})
    backup_codes[username] = confirmed["backup_codes"]
    client.post("/auth/logout", expect=204)
    return sign_in(base_url, username, new, device, devices)


def sign_in(
    base_url: str, username: str, password: str, device: TotpDevice, devices: dict
) -> Client:
    """Signs in with the password and a code, and records the last time step
    used, so verify.py never repeats it."""
    client = Client(base_url)
    client.post(
        "/auth/login", {"username": username, "password": password, "mfa_code": device.code()}
    )
    devices[username] = {"secret": device.secret, "last": device.last_counter}
    client.get("/sources")
    return client


def seed(api: Client, admin_password: str) -> dict:
    created: dict = {}

    # Settings first: the password policy and MFA for every local account
    # then apply to the user below. Every value differs from the release's
    # default. password_reset_enabled keeps its default: turning it on needs
    # email on and PUBLIC_BASE_URL https, and this install has neither.
    created["system_settings"] = api.patch(
        "/system-settings",
        {
            "audit_retention_days": 120,
            "source_max_roots": 5,
            "connections_view_default": "tree",
            "explorer_refresh_seconds": 45,
            "source_check_minutes": 0,
            "password_min_length": 16,
            "password_history": 3,
            "password_required_classes": 3,
            "password_max_age_days": 180,
            "password_max_length": 64,
            "password_reject_simple": False,
            "password_reject_personal": False,
            "connections_view_switchable": False,
            "explorer_sort_default": "mtime_desc",
            "tail_interval_seconds": 5,
            # Also keeps the CI run from asking GitHub for releases.
            "update_check_enabled": False,
            # Added in 0.7.
            "name_index_enabled": False,
            "name_index_minutes": 30,
            "alerts_interval_seconds": 120,
            "credential_max_age_days": 30,
            "concurrency_global": 32,
            "concurrency_per_source": 8,
            "concurrency_user_transfers": 3,
            "listing_max_entries": 8000,
            "backup_interval_hours": 72,
            "audit_folder_listings": False,
            "redaction_enabled": True,
            "redaction_downloads": False,
            "redaction_download_suffix": False,
            "require_mfa_local_accounts": True,
            "password_reset_minutes": 45,
        },
    )
    # Upgrading push agents (0.7), Settings -> Agents.
    created["agent_upgrades"] = api.patch(
        "/agent-upgrades/settings",
        {
            "policy": "manual",
            "window_days": [0, 2, 4],
            "window_start": "03:30",
            "window_hours": 6,
            "failure_limit": 5,
            "concurrency": 2,
        },
    )

    customer = api.post("/customers", {"name": "Northwind"})
    region = api.post("/folders", {"name": "Region North", "customer_id": customer["id"]})
    site = api.post(
        "/folders",
        {"name": "Site A", "customer_id": customer["id"], "parent_folder_id": region["id"]},
    )
    created["customer"], created["folders"] = customer, [region, site]

    sources = {
        "ssh": api.post(
            "/sources",
            {
                "name": "upgrade-ssh",
                "folder_id": site["id"],
                "customer_id": customer["id"],
                "protocol": "ssh",
                "host": "web-01.example.test",
                "port": 22,
                "base_path": "/var/log/app",
                "roots": [
                    {"label": "App", "path": "/var/log/app"},
                    {"label": "Web", "path": "/var/log/nginx"},
                ],
                "credential": {"username": "reader", "password": strong_password()},
            },
        ),
        "smb": api.post(
            "/sources",
            {
                "name": "upgrade-smb",
                "folder_id": region["id"],
                "customer_id": customer["id"],
                "protocol": "smb",
                "host": "files-01.example.test",
                "base_path": "Logs\\App",
                "smb_require_signing": False,
                "smb_encrypt": True,
                "credential": {"username": "EXAMPLE\\reader", "password": strong_password()},
            },
        ),
        "winrm": api.post(
            "/sources",
            {
                "name": "upgrade-winrm",
                "customer_id": customer["id"],
                "protocol": "winrm",
                "host": "app-01.example.test",
                "port": 5986,
                "winrm_scheme": "https",
                "base_path": "C:\\Logs\\App",
                "credential": {"username": "EXAMPLE\\reader", "password": strong_password()},
            },
        ),
        "local": api.post(
            "/sources",
            {
                "name": "upgrade-local",
                "folder_id": site["id"],
                "customer_id": customer["id"],
                "protocol": "local",
                "host": "localhost",
                "base_path": "/data/logs",
            },
        ),
        "agent": api.post(
            "/sources",
            {
                "name": "upgrade-agent",
                "folder_id": region["id"],
                "customer_id": customer["id"],
                "protocol": "agent",
                "host": "db-01.example.test",
                "base_path": "/var/log",
            },
        ),
    }
    created["agent_token_issued"] = bool(
        api.post(f"/sources/{sources['agent']['id']}/agent-token").get("token")
    )

    rules = [
        {"type": "include", "pattern": "**/*.log", "notes": "every log"},
        {"type": "exclude", "pattern": "**/debug/**"},
        {"type": "include", "pattern": "re:^archive/app-[0-9]+\\.log\\.gz$"},
        {"type": "exclude", "pattern": "re:.*\\.tmp$", "notes": "temporary files"},
    ]
    for source in sources.values():
        for rule in rules:
            api.post(f"/sources/{source['id']}/rules", rule)

    api.post("/severity-patterns", {"level": "error", "pattern": "FATAL"})
    api.post(
        f"/sources/{sources['ssh']['id']}/severity-patterns",
        {"level": "warning", "pattern": "re:slow query \\d+ms", "highlight_line": True},
    )

    operators = api.post(
        "/roles",
        {"name": "Upgrade Operators", "global_capabilities": ["create_source", "view_audit_log"]},
    )
    viewers = api.post("/roles", {"name": "Upgrade Viewers"})
    api.post(
        f"/roles/{operators['id']}/grants",
        {"scope_type": "customer", "scope_id": customer["id"], "capabilities": ["view"]},
    )
    api.post(
        f"/roles/{operators['id']}/grants",
        {
            "scope_type": "folder",
            "scope_id": region["id"],
            "capabilities": ["view", "download", "manage_rules", "manage_source"],
        },
    )
    api.post(
        f"/roles/{operators['id']}/grants",
        {
            "scope_type": "source",
            "scope_id": sources["ssh"]["id"],
            "capabilities": ["view", "run_now"],
        },
    )
    api.post(
        f"/roles/{viewers['id']}/grants",
        {"scope_type": "customer", "scope_id": customer["id"], "capabilities": ["view"]},
    )

    operator_temp = strong_password()
    api.post(
        "/users",
        {
            "username": OPERATOR,
            "password": operator_temp,
            "role_id": operators["id"],
            "display_name": "Upgrade Operator",
            "email": "operator@example.test",
        },
    )

    sso = api.post(
        "/sso",
        {
            "name": "Example IdP",
            "issuer": "https://idp.example.test/realms/perchtail",
            "client_id": "perchtail-upgrade",
            "client_secret": secrets.token_urlsafe(24),
            "group_claim": "groups",
            "enabled": True,
        },
    )
    api.post("/sso/group-mappings", {"order": 1, "group_name": "ops", "role_id": operators["id"]})
    api.post("/sso/group-mappings", {"order": 2, "group_name": "all", "role_id": viewers["id"]})
    created["sso_id"] = sso["id"]

    api.post(
        "/alerts",
        {
            "name": "Upgrade alert",
            "query": "ERROR",
            "source_id": sources["ssh"]["id"],
            "webhook_url": "https://example.com/perchtail-upgrade-hook",
        },
    )
    api.post("/monitoring/token")

    # Redaction is on (system settings above), with a system rule the SSH
    # source turns off (SourceRedactionOverride) and a rule of its own.
    ticket_rule = api.post(
        "/redaction-rules",
        {
            "name": "Ticket references",
            "pattern": "TCK-[0-9]{6}",
            "max_length": 10,
            "notes": "the service desk's tickets",
        },
    )
    api.put(
        f"/sources/{sources['ssh']['id']}/redaction-overrides/{ticket_rule['id']}",
        {"disabled": True},
    )
    api.post(
        f"/sources/{sources['ssh']['id']}/redaction-rules",
        {
            "name": "Order references",
            "pattern": "ORD-[0-9]{8}",
            "max_length": 12,
            "notes": "the shop's order numbers",
        },
    )
    # The system SMTP server, with a password, saved but not turned on.
    api.put(
        "/smtp-settings",
        {
            "host": "example.com",
            "port": 587,
            "security": "starttls",
            "username": "perchtail@example.test",
            "password": strong_password(),
            "from_address": "perchtail@example.test",
            "from_name": "PerchTail",
            "enabled": False,
        },
    )

    return {"operator_temp": operator_temp, "ids": {k: v["id"] for k, v in sources.items()}}


def snapshot(api: Client, source_ids: dict) -> dict:
    """Every seeded object as the API returns it now, by endpoint."""
    snap: dict = {
        "/system-settings": api.get("/system-settings"),
        "/agent-upgrades": api.get("/agent-upgrades"),
        "/customers": api.get("/customers"),
        "/folders": api.get("/folders"),
        "/sources": api.get("/sources"),
        "/roles": api.get("/roles"),
        "/users": api.get("/users"),
        "/sso": api.get("/sso"),
        "/sso/group-mappings": api.get("/sso/group-mappings"),
        "/auth/sso/status": api.get("/auth/sso/status"),
        "/severity-patterns": api.get("/severity-patterns"),
        "/alerts": api.get("/alerts"),
        "/monitoring/token": api.get("/monitoring/token"),
        "/redaction-rules": api.get("/redaction-rules"),
        "/smtp-settings": api.get("/smtp-settings"),
    }
    for source_id in source_ids.values():
        snap[f"/sources/{source_id}"] = api.get(f"/sources/{source_id}")
        snap[f"/sources/{source_id}/rules"] = api.get(f"/sources/{source_id}/rules")
        snap[f"/sources/{source_id}/severity-patterns"] = api.get(
            f"/sources/{source_id}/severity-patterns"
        )
        snap[f"/sources/{source_id}/redaction-rules"] = api.get(
            f"/sources/{source_id}/redaction-rules"
        )
        # The rules the source runs: shows the override above.
        snap[f"/sources/{source_id}/redaction-rules/effective"] = api.get(
            f"/sources/{source_id}/redaction-rules/effective"
        )
    for role in snap["/roles"]:
        snap[f"/roles/{role['id']}/grants"] = api.get(f"/roles/{role['id']}/grants")
    return snap


def audit_entries(api: Client) -> list[dict]:
    entries: list[dict] = []
    while True:
        body = api.get(f"/audit?limit=100&offset={len(entries)}")
        entries += body["items"]
        if len(entries) >= body["total"] or not body["items"]:
            return entries


def operator_view(base_url: str, password: str, devices: dict) -> dict:
    """What the local user sees with the operators' grants."""
    device = TotpDevice(devices[OPERATOR]["secret"], devices[OPERATOR]["last"])
    client = sign_in(base_url, OPERATOR, password, device, devices)
    return {
        "customers": sorted(c["name"] for c in client.get("/customers")),
        "folders": sorted(f["name"] for f in client.get("/folders")),
        "sources": {
            s["name"]: sorted(s.get("my_capabilities", [])) for s in client.get("/sources")
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--secrets", required=True)
    args = parser.parse_args()

    temp = temporary_password(sys.stdin.read())
    admin_password = strong_password()
    devices: dict = {}
    backup_codes: dict = {}
    api = sign_in_and_change(args.base_url, ADMIN, temp, admin_password, devices, backup_codes)
    seeded = seed(api, admin_password)
    operator_password = strong_password()
    sign_in_and_change(
        args.base_url, OPERATOR, seeded["operator_temp"], operator_password, devices, backup_codes
    )

    api.post("/audit/integrity/verify")
    before = {
        "version": api.get("/system/update")["running_version"],
        "endpoints": snapshot(api, seeded["ids"]),
        "operator_view": operator_view(args.base_url, operator_password, devices),
        "audit": audit_entries(api),
        "audit_integrity": api.get("/audit/integrity"),
        "source_ids": seeded["ids"],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(before, fh, indent=2, sort_keys=True, default=str)
    with open(args.secrets, "w", encoding="utf-8") as fh:
        json.dump(
            {
                ADMIN: admin_password,
                OPERATOR: operator_password,
                "totp": devices,
                # Two unused recovery codes of the operator: each pass of
                # verify.py signs in once with the next one.
                "backup_codes": {OPERATOR: backup_codes[OPERATOR][:2]},
            },
            fh,
        )
    print(
        f"seeded {before['version']}: {len(before['endpoints'])} endpoints, "
        f"{len(before['audit'])} audit entries, integrity {before['audit_integrity']}"
    )


if __name__ == "__main__":
    main()
