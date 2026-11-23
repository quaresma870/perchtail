"""Fills a fresh install of the previous release through its API, for the
upgrade test in .github/workflows/ci.yml.

    docker logs perchtail-previous 2>&1 | python seed.py --base-url URL \
        --out before.json --secrets secrets.json

stdin carries the first super admin's temporary password: either the
contents of DATA_DIR/initial-admin-password, or the container log of a
release that still logs it (the `initial_super_admin.seeded` line).

It changes that password, then creates a customer, nested folders, a source
of each protocol with rules, two roles with grants, a local user, the SSO
provider and its group mappings, severity patterns, an alert, the monitoring
token and non-default system settings. It then reads every object back and
writes that snapshot to --out, which verify.py compares with what the new
version returns after the upgrade. Passwords and TOTP secrets go to
--secrets, a separate file that is never uploaded.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys

from client import Client

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


def sign_in_and_change(base_url: str, username: str, old: str, new: str) -> Client:
    client = Client(base_url)
    me = client.post("/auth/login", {"username": username, "password": old})
    if not me.get("must_change_password"):
        raise SystemExit(f"{username} was expected to have to change their password")
    client.post("/auth/change-password", {"current_password": old, "new_password": new})
    client.post("/auth/logout", expect=204)
    client = Client(base_url)
    client.post("/auth/login", {"username": username, "password": new})
    return client


def seed(api: Client, admin_password: str) -> dict:
    created: dict = {}

    # Settings first: the password policy then applies to the user below.
    created["system_settings"] = api.patch(
        "/system-settings",
        {
            "audit_retention_days": 30,
            "source_max_roots": 5,
            "connections_view_default": "tree",
            "explorer_refresh_seconds": 45,
            "source_check_minutes": 0,
            "password_min_length": 16,
            "password_history": 3,
            "password_required_classes": 3,
            "password_max_age_days": 180,
        },
    )

    customer = api.post("/customers", {"name": "Upgrade Test Customer"})
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
                "host": "ssh.example.test",
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
                "host": "smb.example.test",
                "base_path": "Logs\\App",
                "credential": {"username": "EXAMPLE\\reader", "password": strong_password()},
            },
        ),
        "winrm": api.post(
            "/sources",
            {
                "name": "upgrade-winrm",
                "customer_id": customer["id"],
                "protocol": "winrm",
                "host": "winrm.example.test",
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
                "host": "agent-host.example.test",
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
            "capabilities": ["view", "download", "manage_rules"],
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

    return {"operator_temp": operator_temp, "ids": {k: v["id"] for k, v in sources.items()}}


def snapshot(api: Client, source_ids: dict) -> dict:
    """Every seeded object as the API returns it now, by endpoint."""
    snap: dict = {
        "/system-settings": api.get("/system-settings"),
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
    }
    for source_id in source_ids.values():
        snap[f"/sources/{source_id}"] = api.get(f"/sources/{source_id}")
        snap[f"/sources/{source_id}/rules"] = api.get(f"/sources/{source_id}/rules")
        snap[f"/sources/{source_id}/severity-patterns"] = api.get(
            f"/sources/{source_id}/severity-patterns"
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


def operator_view(base_url: str, password: str) -> dict:
    """What the local user sees with the operators' grants."""
    client = Client(base_url)
    client.post("/auth/login", {"username": OPERATOR, "password": password})
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
    api = sign_in_and_change(args.base_url, ADMIN, temp, admin_password)
    seeded = seed(api, admin_password)
    operator_password = strong_password()
    sign_in_and_change(args.base_url, OPERATOR, seeded["operator_temp"], operator_password)

    api.post("/audit/integrity/verify")
    before = {
        "version": api.get("/system/update")["running_version"],
        "endpoints": snapshot(api, seeded["ids"]),
        "operator_view": operator_view(args.base_url, operator_password),
        "audit": audit_entries(api),
        "audit_integrity": api.get("/audit/integrity"),
        "source_ids": seeded["ids"],
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(before, fh, indent=2, sort_keys=True, default=str)
    with open(args.secrets, "w", encoding="utf-8") as fh:
        json.dump({ADMIN: admin_password, OPERATOR: operator_password}, fh)
    print(
        f"seeded {before['version']}: {len(before['endpoints'])} endpoints, "
        f"{len(before['audit'])} audit entries, integrity {before['audit_integrity']}"
    )


if __name__ == "__main__":
    main()
