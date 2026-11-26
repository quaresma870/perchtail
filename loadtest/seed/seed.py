"""Seeds a PerchTail instance for a load test through its public API, and
removes what it seeded.

    python loadtest/seed/seed.py init
    docker compose logs perchtail | python loadtest/seed/seed.py admin-init
    python loadtest/seed/seed.py apply --profile tiny
    python loadtest/seed/seed.py status
    python loadtest/seed/seed.py clean

Every object the kit creates is recorded, by id, in a ledger kept with the
instance's state (ledger.json). `apply` only reuses objects from the ledger
and refuses when one of the kit's names is already taken by something it
didn't create; `clean` only touches ledger objects whose names still match
the kit's exact patterns. `apply` is idempotent: running it again finds what
it created and only adds what's missing. Passwords and tokens go to the
state directory (ignored by git), never to stdout.

Needs LOADTEST_BASE_URL and LOADTEST_ALLOWED_HOSTS (see common.py), and an
admin account: LOADTEST_ADMIN_USERNAME/LOADTEST_ADMIN_PASSWORD, or the one
`admin-init` sets up on a fresh instance.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import production  # noqa: E402
from common import (  # noqa: E402
    RESTORE_STATE,
    SETTINGS_STATE,
    ApiError,
    Client,
    KitError,
    admin_client,
    change_settings,
    check_state,
    fail,
    fresh_totp,
    load_profile,
    logs_dir,
    name_prefix,
    prefix,
    random_password,
    read_state,
    remove_state,
    restore_cleanup,
    state_dir,
    state_root,
    write_state,
)

USER_CAPABILITIES = ["view", "download"]
SOURCES_ENV = "sources.env"
LOGS_MARKER = ".perchtail-loadtest"
KIT_TREES = ("shared", "agents")
WINDOWS_AGENTS_DIR = "windows-agents"  # agents.py windows-bundle

KINDS = ("customers", "folders", "sources", "roles", "users", "alerts")
NAME_KEY = {"users": "username"}


def role_name() -> str:
    return f"{name_prefix()}role"


def patterns() -> dict[str, re.Pattern]:
    """The exact names the kit gives each kind of object, with the current
    prefix. `clean` touches only ledger objects whose current name matches."""
    p = re.escape(prefix())
    return {
        "customers": re.compile(rf"^{p}-customer-\d{{2}}(-[a-z0-9]+)*$"),
        "folders": re.compile(rf"^{p}-c\d{{2}}-folder-\d{{2}}(-[a-z0-9]+)*$"),
        "sources": re.compile(rf"^{p}-(ssh|smb|local|winrm|agent)-\d{{3}}$"),
        "roles": re.compile(rf"^{p}-role(-[a-z]+)?$"),
        "users": re.compile(rf"^{p}-user-\d{{3}}$"),
        "alerts": re.compile(rf"^{p}-alert-\d{{2}}$"),
    }


def _another_prefix_hint() -> str:
    current = prefix()
    return (
        f"PerchTail can't rename or delete user accounts, so the simplest way out is another "
        f"prefix for the kit's names: set LOADTEST_PREFIX (now {current!r}), e.g. to "
        f"{current + '2'!r}. Otherwise remove or rename the other objects, or use the state "
        "directory that created them"
    )


def warn(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


# --- ledger ---------------------------------------------------------------


class Ledger:
    """The ids of everything the kit created on this instance, by prefix,
    kind and name, saved after every change so a crash loses nothing. Each
    prefix has its own part, and the kit only ever works on the current
    prefix's: objects created under another prefix are cleaned with that
    prefix."""

    FILE = "ledger.json"

    def __init__(self) -> None:
        data = read_state(self.FILE, {})
        if data and any(kind in data for kind in KINDS):
            data = {"lt": data}  # the layout before prefixes: always `lt`
        self.prefix = prefix()
        self.all: dict[str, dict[str, dict[str, int]]] = {
            p: {kind: dict(part.get(kind, {})) for kind in KINDS} for p, part in data.items()
        }
        self.data = self.all.setdefault(self.prefix, {kind: {} for kind in KINDS})

    def owned(self, kind: str) -> dict[str, int]:
        return dict(self.data[kind])

    def other_prefixes(self) -> list[str]:
        """Prefixes, other than the current one, with objects in the ledger."""
        return sorted(p for p, part in self.all.items() if p != self.prefix and any(part.values()))

    def _save(self) -> None:
        write_state(self.FILE, {p: part for p, part in self.all.items() if any(part.values())})

    def record(self, kind: str, name: str, object_id: int) -> None:
        if not patterns()[kind].match(name):
            raise KitError(f"internal: {name!r} isn't a kit name for {kind}")
        self.data[kind][name] = object_id
        self._save()

    def forget(self, kind: str, name: str) -> None:
        if self.data[kind].pop(name, None) is not None:
            self._save()


def claim(
    ledger: Ledger, kind: str, name: str, current: list[dict], create: Callable[[], dict]
) -> tuple[dict, bool]:
    """The kit's object `name`: the one in the ledger when it still exists
    under that name, otherwise a new one. Refuses when the name is taken by
    an object the kit didn't create. Returns (object, created)."""
    key = NAME_KEY.get(kind, "name")
    by_id = {item["id"]: item for item in current}
    owned_id = ledger.owned(kind).get(name)
    if owned_id is not None:
        item = by_id.get(owned_id)
        if item is not None and item[key] == name:
            return item, False
        if item is not None:
            raise KitError(
                f"{kind[:-1]} {owned_id}, created by the kit as {name!r}, "
                f"is now named {item[key]!r}: "
                "rename it back, or run `seed.py clean` (which leaves it alone and forgets it)"
            )
        ledger.forget(kind, name)  # removed outside the kit
    taken = [item for item in current if item[key] == name]
    if taken:
        raise KitError(
            f"{name!r} already exists on this instance (id {taken[0]['id']}) "
            "and isn't in this kit's "
            f"ledger ({state_dir() / Ledger.FILE}): the kit never adopts objects it didn't create. "
            + _another_prefix_hint()
        )
    item = create()
    ledger.record(kind, name, item["id"])
    return item, True


# --- init -----------------------------------------------------------------


def cmd_init(_args: argparse.Namespace) -> None:
    """Creates the state directory and the source account's credentials
    compose.sources.yml hands to the SSH and SMB test servers."""
    env_path = state_root() / SOURCES_ENV
    if not env_path.exists():
        fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("LT_SOURCE_USER=lt-src\n")
            handle.write(f"LT_SOURCE_PASSWORD={random_password(24)}\n")
        print(f"created {env_path}")
    else:
        print(f"kept {env_path}")
    (logs_dir() / "shared").mkdir(parents=True, exist_ok=True)
    print(f"logs directory: {logs_dir()}")


def source_account() -> tuple[str, str]:
    """The test servers' account, from sources.env at the root of the state
    directory, unless overridden per protocol by environment variables."""
    values: dict[str, str] = {}
    env_path = state_root() / SOURCES_ENV
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values.get("LT_SOURCE_USER", ""), values.get("LT_SOURCE_PASSWORD", "")


# --- admin-init -----------------------------------------------------------

_TEMP_PASSWORD = re.compile(r'"temporary_password"\s*:\s*"([^"]+)"')
_USERNAME = re.compile(r'"username"\s*:\s*"([^"]+)"')


def cmd_admin_init(_args: argparse.Namespace) -> None:
    """For a fresh instance: reads the first super admin's one-time
    password on stdin, signs in, replaces it with a random one and turns
    MFA on (PerchTail requires it of super admins), keeping the password
    and the TOTP secret in the instance's admin.json. Stdin is the password
    file (`docker compose exec -T perchtail cat /data/db/initial-admin-password`),
    or, from PerchTail before 0.7, the container logs with the password."""
    saved = read_state("admin.json")
    if saved:
        client = Client()
        try:
            client.login(saved["username"], saved["password"], saved.get("totp_secret"))
        except ApiError as exc:
            fail(
                f"the saved admin credentials don't work any more ({exc.status}); remove admin.json"
            )
        if not saved.get("totp_secret"):
            # An earlier admin-init changed the password but stopped before MFA.
            _enable_admin_mfa(client, saved["username"], saved["password"])
        print(f"admin account {saved['username']!r} already set up ({state_dir() / 'admin.json'})")
        return

    username = temporary = None
    lines = [line.strip() for line in sys.stdin if line.strip()]
    for line in lines:
        if "initial_super_admin.seeded" in line:
            pw = _TEMP_PASSWORD.search(line)
            user = _USERNAME.search(line)
            if pw and user:
                username, temporary = user.group(1), pw.group(1)
    if not temporary and len(lines) == 1 and not lines[0].startswith("{"):
        # The password file: one line, the password itself.
        username = os.environ.get("LOADTEST_ADMIN_USERNAME", "admin")
        temporary = lines[0]
    if not temporary:
        fail(
            "no first admin password on stdin: pipe "
            "`docker compose exec -T perchtail cat /data/db/initial-admin-password`"
        )

    client = Client()
    client.login(username, temporary)
    new_password = random_password()
    client.post(
        "/auth/change-password",
        {"current_password": temporary, "new_password": new_password},
    )
    # Kept before MFA is turned on, so a failure from here on loses nothing:
    # admin-init again picks up where this one stopped.
    write_state("admin.json", {"username": username, "password": new_password})
    _enable_admin_mfa(client, username, new_password)
    saved_in = state_dir() / "admin.json"
    print(f"admin account {username!r} ready, with MFA; credentials in {saved_in}")


def _enable_admin_mfa(client: Client, username: str, password: str) -> None:
    """Turns MFA on for the signed-in admin and keeps its TOTP secret with
    the password in admin.json."""
    secret = client.post("/auth/mfa/enroll", {"password": password})["secret"]
    client.post("/auth/mfa/confirm", {"code": fresh_totp(username, secret)})
    write_state("admin.json", {"username": username, "password": password, "totp_secret": secret})


# --- apply ----------------------------------------------------------------


def customer_names(profile: dict) -> list[str]:
    """`lt-customer-NN`, with the profile's invented name after it when it
    gives one (`customer_names`, e.g. lt-customer-01-kestrel-freight)."""
    names = profile.get("customer_names", [])
    return [
        f"{name_prefix()}customer-{i:02d}" + (f"-{names[i - 1]}" if i <= len(names) else "")
        for i in range(1, int(profile["customers"]) + 1)
    ]


def folder_names(profile: dict, c_index: int) -> list[str]:
    names = profile.get("folder_names", [])
    return [
        f"{name_prefix()}c{c_index:02d}-folder-{i:02d}"
        + (f"-{names[i - 1]}" if i <= len(names) else "")
        for i in range(1, int(profile.get("folders_per_customer", 0)) + 1)
    ]


def ensure_customers(client: Client, ledger: Ledger, profile: dict) -> list[dict]:
    current = client.get("/customers")
    out = []
    for name in customer_names(profile):
        create = partial(client.post, "/customers", {"name": name})
        customer, _ = claim(ledger, "customers", name, current, create)
        out.append(customer)
    return out


def ensure_folders(
    client: Client, ledger: Ledger, customers: list[dict], profile: dict
) -> dict[int, list[dict]]:
    current = client.get("/folders")
    out: dict[int, list[dict]] = {}
    for c_index, customer in enumerate(customers, start=1):
        folders = []
        for name in folder_names(profile, c_index):
            create = partial(client.post, "/folders", {"name": name, "customer_id": customer["id"]})
            folder, _ = claim(ledger, "folders", name, current, create)
            if folder["customer_id"] != customer["id"]:
                raise KitError(
                    f"folder {name!r} was moved to another customer; run `seed.py clean` first"
                )
            folders.append(folder)
        out[customer["id"]] = folders
    return out


def _placements(
    customers: list[dict], folders: dict[int, list[dict]]
) -> list[tuple[int, int | None]]:
    """Where sources go, round-robin: each customer's root, then each of
    its folders."""
    slots: list[tuple[int, int | None]] = []
    for customer in customers:
        slots.append((customer["id"], None))
        slots.extend((customer["id"], f["id"]) for f in folders[customer["id"]])
    return slots


def _env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise KitError(f"{name} is required for this profile")
    return value


def _split_host(entry: str, default_port: int) -> tuple[str, int]:
    """'host', 'host:port', '[v6]' or '[v6]:port'."""
    entry = entry.strip()
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if entry.count(":") == 1:
        host, port = entry.split(":")
        return host, int(port)
    return entry, default_port


def protocol_hosts(
    profile: dict, protocol: str, default: str | None, default_port: int
) -> list[tuple[str, int]]:
    """The servers a protocol's sources are spread over, round-robin:
    LOADTEST_<PROTOCOL>_HOSTS (comma-separated host or host:port), else the
    profile's hosts.<protocol> list, else LOADTEST_<PROTOCOL>_HOST and
    _PORT, else the local test server."""
    upper = protocol.upper()
    raw = os.environ.get(f"LOADTEST_{upper}_HOSTS")
    entries = (
        [h for h in raw.split(",") if h.strip()]
        if raw
        else list(profile.get("hosts", {}).get(protocol, []))
    )
    port = int(_env(f"LOADTEST_{upper}_PORT", str(default_port)))
    if not entries:
        single = _env(f"LOADTEST_{upper}_HOST", default, required=default is None)
        entries = [single]
    return [_split_host(entry, port) for entry in entries]


def _own_tree(profile: dict, base: str, name: str | None, sep: str = "/") -> str:
    """A production source reads its own tree, prod/<name> under the base."""
    if not name or "production" not in profile:
        return base
    return base.rstrip("/\\") + sep + "prod" + sep + name


def source_spec(profile: dict, protocol: str, index: int, name: str | None = None) -> dict:
    """How to reach the servers for a protocol, for its `index`-th source
    (from 0). Defaults match compose.sources.yml; the LOADTEST_<PROTOCOL>_*
    variables, or the profile's hosts, point at the servers of a dedicated
    test environment instead. WinRM has no default: there is no WinRM test
    server here."""
    user, password = source_account()
    if protocol in ("ssh", "smb"):
        default_host, default_port = ("lt-ssh", 22) if protocol == "ssh" else ("lt-smb", 445)
        hosts = protocol_hosts(profile, protocol, default_host, default_port)
        host, port = hosts[index % len(hosts)]
        upper = protocol.upper()
        return {
            "host": host,
            "port": port,
            "base_path": _own_tree(
                profile,
                _env(f"LOADTEST_{upper}_BASE_PATH", "/srv/logs" if protocol == "ssh" else "ltlogs"),
                name,
            ),
            "credential": {
                "username": _env(f"LOADTEST_{upper}_USERNAME", user),
                "password": _env(f"LOADTEST_{upper}_PASSWORD", password),
            },
        }
    if protocol == "local":
        return {
            "host": "localhost",
            "port": None,
            "base_path": _own_tree(
                profile, _env("LOADTEST_LOCAL_BASE_PATH", "/srv/lt-local"), name
            ),
        }
    if protocol == "winrm":
        hosts = protocol_hosts(profile, "winrm", None, 5986)
        host, port = hosts[index % len(hosts)]
        spec = {
            "host": host,
            "port": port,
            "base_path": _own_tree(
                profile, _env("LOADTEST_WINRM_BASE_PATH", required=True), name, "\\"
            ),
            "winrm_scheme": _env("LOADTEST_WINRM_SCHEME", "https"),
            "credential": {
                "username": _env("LOADTEST_WINRM_USERNAME", required=True),
                "password": _env("LOADTEST_WINRM_PASSWORD", required=True),
            },
        }
        ca_file = _env("LOADTEST_WINRM_CA_FILE")
        if ca_file:
            spec["tls_ca_pem"] = Path(ca_file).read_text(encoding="utf-8")
        jea = _env("LOADTEST_WINRM_JEA_CONFIGURATION")
        if jea and "matrix" in profile and index == 1:
            # protocol_matrix's second WinRM source goes through the JEA
            # endpoint (docs/examples/jea), to measure it apart.
            spec["winrm_configuration"] = jea
        return spec
    if protocol == "agent":
        return {"host": "agent", "port": None, "base_path": "(the agent's own folder)"}
    raise KitError(f"unknown protocol {protocol}")


def planned_sources(profile: dict) -> list[dict]:
    """The sources a profile asks for, in a stable order."""
    plan = []
    for protocol in ("ssh", "smb", "local", "winrm"):
        for i in range(1, int(profile["sources"].get(protocol, 0)) + 1):
            plan.append(
                {
                    "name": f"{name_prefix()}{protocol}-{i:03d}",
                    "protocol": protocol,
                    "tree": "shared",
                    "index": i - 1,
                }
            )
    agents = profile.get("agents", {})
    running, stopped = int(agents.get("running", 0)), int(agents.get("stopped", 0))
    windows = int(agents.get("windows", 0))
    for i in range(1, running + stopped + 1):
        entry = {
            "name": f"{name_prefix()}agent-{i:03d}",
            "protocol": "agent",
            "tree": "agent",
            "index": i - 1,
            "agent_index": i,
            "running": i <= running,
        }
        if running - windows < i <= running:
            # Meant to run on a Windows test server (agents.py windows-bundle).
            entry["windows"] = True
        plan.append(entry)
    return plan


def ensure_sources(
    client: Client, ledger: Ledger, profile: dict, slots: list[tuple[int, int | None]]
) -> list[dict]:
    current = client.get("/sources")
    index_on = bool(profile.get("source_name_index", True))
    out = []
    for n, planned in enumerate(planned_sources(profile)):
        customer_id, folder_id = slots[n % len(slots)]

        def create(
            planned: dict = planned,
            customer_id: int = customer_id,
            folder_id: int | None = folder_id,
        ) -> dict:
            body = {
                "name": planned["name"],
                "customer_id": customer_id,
                "folder_id": folder_id,
                "protocol": planned["protocol"],
                "enabled": True,
                "name_index_enabled": index_on,
                **source_spec(profile, planned["protocol"], planned["index"], planned["name"]),
            }
            return client.post("/sources", body)

        source, created = claim(ledger, "sources", planned["name"], current, create)
        if created:
            print(f"created source {planned['name']}")
        elif not source["enabled"] or source["name_index_enabled"] != index_on:
            source = client.patch(
                f"/sources/{source['id']}", {"enabled": True, "name_index_enabled": index_on}
            )
        if source["rule_count"] == 0:
            client.post(f"/sources/{source['id']}/rules", {"type": "include", "pattern": "**"})
        entry = {k: v for k, v in planned.items() if k != "index"}
        out.append(
            {
                **entry,
                "id": source["id"],
                "customer_id": source["customer_id"],
                "folder_id": source["folder_id"],
            }
        )
    return out


def ensure_role(client: Client, ledger: Ledger, customers: list[dict]) -> dict:
    role, _ = claim(
        ledger,
        "roles",
        role_name(),
        client.get("/roles"),
        lambda: client.post("/roles", {"name": role_name(), "global_capabilities": []}),
    )
    grants = {
        (g["scope_type"], g["scope_id"]): g for g in client.get(f"/roles/{role['id']}/grants")
    }
    for customer in customers:
        grant = grants.get(("customer", customer["id"]))
        if grant is None:
            client.post(
                f"/roles/{role['id']}/grants",
                {
                    "scope_type": "customer",
                    "scope_id": customer["id"],
                    "capabilities": USER_CAPABILITIES,
                },
            )
        elif sorted(grant["capabilities"]) != sorted(USER_CAPABILITIES):
            client.patch(
                f"/roles/{role['id']}/grants/{grant['id']}", {"capabilities": USER_CAPABILITIES}
            )
    return role


MFA_STATE = "users-mfa.json"


def _set_own_password(username: str, current: str) -> str:
    """Signs in as the user with the temporary password an admin set and
    replaces it, as the forced first-login change requires. A user PerchTail
    requires MFA of (a role with a global capability) then turns it on, and
    its TOTP secret is kept in users-mfa.json for the k6 scenarios."""
    client = Client()
    # An account that already has MFA (an earlier run) signs in with it: an
    # admin's password reset doesn't turn MFA off.
    me = client.login(username, current, read_state(MFA_STATE, {}).get(username))
    final = random_password()
    client.post("/auth/change-password", {"current_password": current, "new_password": final})
    if me.get("mfa_required") and not me.get("mfa_enabled"):
        secret = client.post("/auth/mfa/enroll", {"password": final})["secret"]
        client.post("/auth/mfa/confirm", {"code": fresh_totp(username, secret)})
        secrets_by_user = read_state(MFA_STATE, {})
        secrets_by_user[username] = secret
        write_state(MFA_STATE, secrets_by_user)
    return final


def ensure_users(
    client: Client, ledger: Ledger, count: int, role_of: Callable[[str], dict]
) -> dict[str, str]:
    saved: dict[str, str] = read_state("users.json", {})
    current = client.get("/users")
    for i in range(1, count + 1):
        username = f"{name_prefix()}user-{i:03d}"
        role = role_of(username)
        temporary = random_password()
        body = {"username": username, "password": temporary, "role_id": role["id"]}
        user, created = claim(
            ledger, "users", username, current, partial(client.post, "/users", body)
        )
        if created:
            saved[username] = _set_own_password(username, temporary)
            write_state("users.json", saved)
            print(f"created user {username}")
            continue
        if not user["active"] or user["role_id"] != role["id"]:
            client.patch(f"/users/{user['id']}", {"active": True, "role_id": role["id"]})
        if username in saved:
            try:
                me = Client().login(
                    username, saved[username], read_state(MFA_STATE, {}).get(username)
                )
                if not me.get("must_change_password"):
                    continue
            except ApiError:
                pass
        temporary = client.post(f"/users/{user['id']}/reset-password")["temporary_password"]
        saved[username] = _set_own_password(username, temporary)
        write_state("users.json", saved)
        print(f"reset user {username}")
    return {u: saved[u] for u in (f"{name_prefix()}user-{i:03d}" for i in range(1, count + 1))}


def ensure_agent_tokens(client: Client, sources: list[dict]) -> None:
    tokens: dict[str, str] = read_state("agent-tokens.json", {})
    current = {s["id"]: s for s in client.get("/sources")}
    for source in sources:
        if source["protocol"] != "agent":
            continue
        if source["name"] in tokens and current[source["id"]]["has_agent_token"]:
            continue
        tokens[source["name"]] = client.post(f"/sources/{source['id']}/agent-token")["token"]
        write_state("agent-tokens.json", tokens)


def ensure_monitoring_token(client: Client) -> None:
    """The collector reads /monitoring/health with the monitoring token.
    An instance that already has one (real monitoring) keeps it: pass it in
    LOADTEST_MONITORING_TOKEN, since regenerating would break whatever
    polls it today. Only an instance with none gets one."""
    if os.environ.get("LOADTEST_MONITORING_TOKEN"):
        return
    saved = read_state("monitoring.json")
    configured = client.get("/monitoring/token")["configured"]
    if saved and configured:
        return
    if configured:
        raise KitError(
            "this instance already has a monitoring token: set LOADTEST_MONITORING_TOKEN "
            "(the kit won't regenerate it, that would break existing monitoring)"
        )
    token = client.post("/monitoring/token")["token"]
    write_state("monitoring.json", {"token": token, "created_by_kit": True})
    print("generated a monitoring token (monitoring.json in the instance's state)")


LIST_PATHS = {
    "customers": "/customers",
    "folders": "/folders",
    "sources": "/sources",
    "roles": "/roles",
    "users": "/users",
    "alerts": "/alerts",
}


# --- production ---------------------------------------------------------------


def role_names(profile: dict) -> list[str]:
    if "production" in profile:
        return [f"{role_name()}-{p}" for p in production.personas(profile)]
    return [role_name()]


def ensure_persona_roles(
    client: Client, ledger: Ledger, profile: dict, customers: list[dict], folders: dict
) -> dict[str, dict]:
    """production: one role per kind of person, with the grants of
    production.ROLES (on customers by number, or each customer's first
    folder), and its global capabilities."""
    current = client.get("/roles")
    out = {}
    for persona in production.personas(profile):
        spec = production.ROLES.get(persona, production.ROLES["support"])
        name = f"{role_name()}-{persona}"
        body = {"name": name, "global_capabilities": spec["global"]}
        role, _ = claim(ledger, "roles", name, current, partial(client.post, "/roles", body))
        if sorted(role.get("global_capabilities", [])) != sorted(spec["global"]):
            role = client.patch(f"/roles/{role['id']}", {"global_capabilities": spec["global"]})
        wanted: list[tuple[str, int]] = []
        if spec.get("first_folders"):
            wanted = [("folder", folders[c["id"]][0]["id"]) for c in customers if folders[c["id"]]]
        else:
            numbers = (
                range(1, len(customers) + 1) if spec["customers"] == "all" else spec["customers"]
            )
            wanted = [("customer", customers[n - 1]["id"]) for n in numbers if n <= len(customers)]
        grants = {
            (g["scope_type"], g["scope_id"]): g for g in client.get(f"/roles/{role['id']}/grants")
        }
        for scope_type, scope_id in wanted:
            grant = grants.get((scope_type, scope_id))
            if grant is None:
                client.post(
                    f"/roles/{role['id']}/grants",
                    {
                        "scope_type": scope_type,
                        "scope_id": scope_id,
                        "capabilities": spec["capabilities"],
                    },
                )
            elif sorted(grant["capabilities"]) != sorted(spec["capabilities"]):
                client.patch(
                    f"/roles/{role['id']}/grants/{grant['id']}",
                    {"capabilities": spec["capabilities"]},
                )
        out[persona] = role
    return out


def alert_webhook() -> str | None:
    """Alerts need a webhook whose host resolves to public addresses (the
    server checks it when the alert is created). The kit sets none of its
    own: give one in LOADTEST_ALERT_WEBHOOK_URL. The alerts' query never
    matches, so the webhook is never called; only the server's DNS lookup
    at creation reaches outside."""
    return os.environ.get("LOADTEST_ALERT_WEBHOOK_URL") or None


def alert_names(profile: dict) -> list[str]:
    count = int(production.settings(profile).get("alerts", 0))
    return [f"{name_prefix()}alert-{i:02d}" for i in range(1, count + 1)]


def ensure_alerts(client: Client, ledger: Ledger, profile: dict, sources: list[dict]) -> dict:
    """production: alerts on different sources, each watching the source's
    current.log for a word that never appears, so every pass reads what
    was appended (the cost being measured) and never sends anything."""
    names = alert_names(profile)
    if not names:
        return {"created": 0}
    webhook = alert_webhook()
    if not webhook:
        print("no alerts: set LOADTEST_ALERT_WEBHOOK_URL to have them (see the README)")
        return {"created": 0, "skipped": "LOADTEST_ALERT_WEBHOOK_URL not set"}
    candidates = [s for s in sources if s["protocol"] != "agent" or s.get("running")]
    current = client.get("/alerts")
    for i, name in enumerate(names):
        source = candidates[(i * len(candidates)) // len(names)]
        body = {
            "name": name,
            "query": f"lt-never-{random_password(12)}",
            "source_id": source["id"],
            "file_glob": "**/current.log",
            "webhook_url": webhook,
        }
        claim(ledger, "alerts", name, current, partial(client.post, "/alerts", body))
    return {"created": len(names)}


def write_production_plan(
    profile: dict, sources: list[dict], users: dict[str, str], alerts: dict
) -> None:
    """production-plan.json, what the k6 scenario `production` runs."""
    names = sorted(users)
    personas_of = production.user_personas(profile, names)
    stages = production.concurrency_stages(profile)
    rates = production.rate_classes(profile, [s["name"] for s in sources])
    # The incident's file: an intense source's log (a server's, or a
    # running agent's that isn't on a Windows test server).
    usable = [
        s
        for s in sources
        if s["protocol"] != "agent" or (s.get("running") and not s.get("windows"))
    ]
    intense = [s for s in usable if rates[s["name"]]["class"] == "intense"] or usable
    incident = production.incident_window(profile, stages)
    if intense:
        incident.update(source=intense[0]["name"], path="app-1/current.log")
    see_all = [u for u in names if personas_of[u] in ("investigation", "admin")] or names
    windows = [s["name"] for s in sources if s.get("windows")]
    write_state(
        "production-plan.json",
        {
            "personas": {
                p: {**spec, "users": [u for u in names if personas_of[u] == p]}
                for p, spec in production.personas(profile).items()
            },
            "stages": stages,
            "duration": production.total_seconds(stages),
            "peak": production.peak_window(stages),
            "incident": incident,
            "incident_users": see_all,
            "admin_role_target": f"{role_name()}-occasional",
            "user_names": names,
            "classes": {
                c: sum(r["class"] == c for r in rates.values()) for c in production.classes(profile)
            },
            "windows_agents": {"names": windows, "on": windows_agents_on()},
            "alerts": alerts,
        },
    )


def windows_agents_on() -> str:
    return (
        "windows test server" if os.environ.get("LOADTEST_WINDOWS_AGENT_HOSTS") else "load machine"
    )


def planned_names(profile: dict) -> dict[str, list[str]]:
    """Every name `apply` would claim for a profile, by kind."""
    customers = int(profile["customers"])
    return {
        "customers": customer_names(profile),
        "folders": [n for c in range(1, customers + 1) for n in folder_names(profile, c)],
        "sources": [s["name"] for s in planned_sources(profile)],
        "roles": role_names(profile),
        "users": [f"{name_prefix()}user-{i:03d}" for i in range(1, int(profile["users"]) + 1)],
        "alerts": alert_names(profile) if alert_webhook() else [],
    }


def preflight(client: Client, ledger: Ledger, profile: dict) -> None:
    """Refuses before creating anything when one of the profile's names is
    taken by an object outside the ledger."""
    taken = []
    for kind, names in planned_names(profile).items():
        key = NAME_KEY.get(kind, "name")
        wanted = set(names)
        owned = ledger.owned(kind)
        for item in client.get(LIST_PATHS[kind]):
            if item[key] in wanted and owned.get(item[key]) != item["id"]:
                taken.append(f"{item[key]} (id {item['id']})")
    if taken:
        shown = ", ".join(taken[:10]) + (f" and {len(taken) - 10} more" if len(taken) > 10 else "")
        raise KitError(
            f"nothing seeded: these names are taken on this instance by objects that aren't in "
            f"this kit's ledger ({state_dir() / Ledger.FILE}): {shown}. The kit never adopts "
            "objects it didn't create. " + _another_prefix_hint()
        )


def _no_mfa_for_kit_users(client: Client) -> None:
    """The kit's users sign in with a password only. On an instance that
    requires MFA of every local account (`require_mfa_local_accounts`, on
    by default on installs from 1.0), that requirement is turned off for
    the run; change_settings saves the instance's value and `clean` puts
    it back. Administrators keep needing MFA whatever the setting."""
    current = client.get("/system-settings")
    if current.get("require_mfa_local_accounts"):
        change_settings(client, {"require_mfa_local_accounts": False})


def cmd_apply(args: argparse.Namespace) -> None:
    profile = load_profile(args.profile)
    client = admin_client()
    ledger = Ledger()
    preflight(client, ledger, profile)
    customers = ensure_customers(client, ledger, profile)
    folders = ensure_folders(client, ledger, customers, profile)
    sources = ensure_sources(client, ledger, profile, _placements(customers, folders))
    count = int(profile["users"])
    _no_mfa_for_kit_users(client)
    if "production" in profile:
        roles = ensure_persona_roles(client, ledger, profile, customers, folders)
        usernames = [f"{name_prefix()}user-{i:03d}" for i in range(1, count + 1)]
        personas_of = production.user_personas(profile, usernames)
        users = ensure_users(client, ledger, count, lambda u: roles[personas_of[u]])
    else:
        role = ensure_role(client, ledger, customers)
        users = ensure_users(client, ledger, count, lambda _u: role)
    ensure_agent_tokens(client, sources)
    ensure_monitoring_token(client)
    if "production" in profile:
        alerts = ensure_alerts(client, ledger, profile, sources)
        write_production_plan(profile, sources, users, alerts)
    change_settings(client, profile.get("settings", {}))
    write_state(
        "seeded.json",
        {
            "profile": profile["name"],
            "prefix": prefix(),
            "sources": sources,
            "users": sorted(users),
            "seeded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )
    by_protocol: dict[str, int] = {}
    for source in sources:
        by_protocol[source["protocol"]] = by_protocol.get(source["protocol"], 0) + 1
    print(
        f"seeded profile {profile['name']}: {len(customers)} customers, "
        f"{sum(len(f) for f in folders.values())} folders, "
        f"sources {by_protocol}, {len(users)} users"
    )


# --- index ------------------------------------------------------------------


def cmd_index(args: argparse.Namespace) -> None:
    """Asks for a file-name index pass on every seeded source and waits
    until each has one (or the timeout), so name_search finds files."""
    client = admin_client()
    seeded = read_state("seeded.json") or fail("nothing seeded yet")
    owned = set(Ledger().owned("sources").values())
    ids = {s["id"] for s in seeded["sources"] if s.get("running", True)}
    if ids - owned:
        raise KitError(
            "seeded.json names sources that aren't in the ledger; run `seed.py apply` again"
        )
    if not client.get("/system-settings")["name_index_enabled"]:
        print("file-name index is off in System settings; nothing to wait for")
        return
    for source_id in ids:
        try:
            client.post(f"/sources/{source_id}/name-index", expect=(202,))
        except ApiError as exc:
            print(f"source {source_id}: index not started ({exc.status} {exc.detail})")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        pending = [
            s["id"]
            for s in client.get("/sources")
            if s["id"] in ids and s["name_indexed_at"] is None
        ]
        if not pending:
            print(f"file-name index ready on {len(ids)} sources")
            return
        time.sleep(3)
    print(f"warning: sources {pending} still have no file-name index after {args.timeout}s")


# --- clean --------------------------------------------------------------------


def _no_access_role(client: Client) -> dict:
    for role in client.get("/roles"):
        if role["is_builtin"] and not role["is_super_admin"]:
            return role
    raise KitError("no built-in no-access role found")


def _owned_items(ledger: Ledger, kind: str, current: list[dict]) -> list[tuple[str, dict]]:
    """Ledger objects that still exist under the kit's exact name. The rest
    are forgotten: gone already, or renamed by someone (left alone)."""
    key = NAME_KEY.get(kind, "name")
    by_id = {item["id"]: item for item in current}
    out = []
    for name, object_id in ledger.owned(kind).items():
        item = by_id.get(object_id)
        if item is None:
            ledger.forget(kind, name)
        elif item[key] != name or not patterns()[kind].match(item[key]) or item.get("is_system"):
            warn(
                f"{kind[:-1]} {object_id} was created as {name!r} "
                f"but is now {item[key]!r}: left alone"
            )
            ledger.forget(kind, name)
        else:
            out.append((name, item))
    return out


def _delete(
    client: Client, ledger: Ledger, kind: str, name: str, path: str, failures: list[str]
) -> None:
    try:
        client.delete(path)
        ledger.forget(kind, name)
    except ApiError as exc:
        failures.append(f"{kind[:-1]} {name!r}: {exc.status} {exc.detail} (kept in the ledger)")


def restore_settings(client: Client) -> None:
    """Puts back the keys the profile and its phases changed, nothing else.
    The saved values are removed only once they are back."""
    original = read_state(SETTINGS_STATE)
    if not original:
        return
    current = client.get("/system-settings")
    changed = {k: v for k, v in original.items() if k in current and current[k] != v}
    if changed:
        client.patch("/system-settings", changed)
    remove_state(SETTINGS_STATE)
    print(f"restored System settings: {', '.join(sorted(changed)) or 'none differed'}")


def clean_files() -> None:
    """Deletes the kit's trees in the logs directory (shared/, agents/), and
    only when gen_logs.py's marker file is there."""
    root = logs_dir()
    marker = root / LOGS_MARKER
    if not marker.exists():
        warn(f"{root} has no {LOGS_MARKER} marker (gen_logs.py writes it): no files deleted")
        return
    for tree in KIT_TREES:
        shutil.rmtree(root / tree, ignore_errors=True)
    marker.unlink()
    print(f"deleted {', '.join(t + '/' for t in KIT_TREES)} in {root}")


def remove_windows_bundle() -> None:
    """windows-agents/ (agents.py windows-bundle): start-agents.ps1 holds
    the agents' tokens."""
    folder = state_dir() / WINDOWS_AGENTS_DIR
    if folder.is_symlink() or folder.is_file():
        folder.unlink()
    elif folder.exists():
        shutil.rmtree(folder)


def clean_restore_copy() -> None:
    """A restore copy a protocol_matrix run left in the PerchTail container
    (matrix.py, step `operation`), as recorded in the instance's state."""
    copy = read_state(RESTORE_STATE)
    if not copy:
        return
    command = restore_cleanup(str(copy.get("dir") or ""))
    container = str(copy.get("container") or "")
    if not container:
        raise KitError(f"{RESTORE_STATE} names no container")
    user = os.environ.get("LOADTEST_CONTAINER_USER", "perchtail")
    r = subprocess.run(
        ["docker", "exec", "-u", user, container, "sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if r.returncode != 0:
        raise KitError(f"couldn't remove {copy['dir']} in {container}: {r.stderr.strip()[-200:]}")
    remove_state(RESTORE_STATE)


def _clean_step(failures: list[str], what: str, action: Callable[[], None]) -> None:
    """Runs one part of the clean; a failure is noted and the rest goes on."""
    try:
        action()
    except (ApiError, KitError, OSError) as exc:
        failures.append(f"{what}: {exc}")


def cmd_clean(args: argparse.Namespace) -> None:
    """Removes what the kit created under the current prefix (per its
    ledger) and restores the System settings it changed. Users can only be
    deactivated through the API (PerchTail keeps accounts for the audit
    trail): they are moved to the built-in no-access role and deactivated,
    so they hold nothing and can't sign in, and they stay in the ledger for
    the next `apply`. Each object is handled on its own: a failure is noted
    and the rest goes on, and the command ends by listing what it couldn't
    clean, with a non-zero exit code."""
    client = admin_client()
    ledger = Ledger()
    failures: list[str] = []

    def alerts() -> None:
        for name, alert in _owned_items(ledger, "alerts", client.get("/alerts")):
            _delete(client, ledger, "alerts", name, f"/alerts/{alert['id']}", failures)

    def sources() -> None:
        for name, source in _owned_items(ledger, "sources", client.get("/sources")):
            _delete(client, ledger, "sources", name, f"/sources/{source['id']}", failures)

    def folders() -> None:
        owned = _owned_items(ledger, "folders", client.get("/folders"))
        by_id = {f["id"]: f for _, f in owned}

        def depth(folder: dict) -> int:
            d, parent = 0, folder["parent_folder_id"]
            while parent in by_id:
                d, parent = d + 1, by_id[parent]["parent_folder_id"]
            return d

        for name, folder in sorted(owned, key=lambda pair: depth(pair[1]), reverse=True):
            _delete(client, ledger, "folders", name, f"/folders/{folder['id']}", failures)

    def customers() -> None:
        for name, customer in _owned_items(ledger, "customers", client.get("/customers")):
            _delete(client, ledger, "customers", name, f"/customers/{customer['id']}", failures)

    def users() -> None:
        # Before the role: a role still held by a user can't be deleted.
        no_access = _no_access_role(client)
        for name, user in _owned_items(ledger, "users", client.get("/users")):
            try:
                if user["role_id"] != no_access["id"]:
                    client.patch(f"/users/{user['id']}", {"role_id": no_access["id"]})
                if user["active"]:
                    client.delete(f"/users/{user['id']}")
            except ApiError as exc:
                failures.append(f"user {name!r}: {exc.status} {exc.detail} (not deactivated)")

    def roles() -> None:
        for name, role in _owned_items(ledger, "roles", client.get("/roles")):
            if not role["is_builtin"]:
                _delete(client, ledger, "roles", name, f"/roles/{role['id']}", failures)

    for what, action in (
        ("alerts", alerts),
        ("sources", sources),
        ("folders", folders),
        ("customers", customers),
        ("users", users),
        ("roles", roles),
        ("System settings", lambda: restore_settings(client)),
    ):
        _clean_step(failures, what, action)
    for name in ("seeded.json", "agent-tokens.json", "production-plan.json"):
        _clean_step(failures, name, lambda name=name: remove_state(name))
    _clean_step(failures, f"{WINDOWS_AGENTS_DIR}/", remove_windows_bundle)
    _clean_step(failures, "restore copy", clean_restore_copy)
    if args.files:
        _clean_step(failures, "log files", clean_files)
    others = ledger.other_prefixes()
    if others:
        print(
            f"note: the ledger also holds objects created under the prefix(es) "
            f"{', '.join(others)}: clean them with LOADTEST_PREFIX set to each"
        )
    if failures:
        cmd_status(args)
        raise KitError(f"clean incomplete, {len(failures)} problem(s):\n  " + "\n  ".join(failures))
    print(
        "cleaned: the kit's alerts, sources, folders, customers and roles removed; "
        "its users deactivated"
    )
    cmd_status(args)


def cmd_status(_args: argparse.Namespace) -> None:
    """Counts the kit's objects under the current prefix (from the ledger),
    and any other objects whose names start with the prefix, which the kit
    leaves alone."""
    client = admin_client()
    ledger = Ledger()
    current = {kind: client.get(path) for kind, path in LIST_PATHS.items()}
    kit: dict[str, int] = {}
    other: dict[str, int] = {}
    for kind, items in current.items():
        key = NAME_KEY.get(kind, "name")
        owned = set(ledger.owned(kind).values())
        mine = [i for i in items if i["id"] in owned]
        if kind == "users":
            kit["users_active"] = sum(u["active"] for u in mine)
            kit["users_inactive"] = sum(not u["active"] for u in mine)
        else:
            kit[kind] = len(mine)
        other[kind] = sum(i[key].startswith(name_prefix()) and i["id"] not in owned for i in items)
    print(
        json.dumps(
            {
                "prefix": prefix(),
                "kit": kit,
                "other_objects_with_prefix": other,
                "other_prefixes_in_ledger": ledger.other_prefixes(),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="create the state directory and the test servers' credentials")
    sub.add_parser(
        "admin-init", help="set up the admin account of a fresh instance (logs on stdin)"
    )
    p_apply = sub.add_parser("apply", help="create (or complete) everything a profile needs")
    p_apply.add_argument("--profile", required=True)
    p_index = sub.add_parser("index", help="run the file-name index on the seeded sources and wait")
    p_index.add_argument("--timeout", type=int, default=300)
    p_clean = sub.add_parser(
        "clean", help="remove what the kit's ledger lists and restore System settings"
    )
    p_clean.add_argument("--files", action="store_true", help="also delete the generated log trees")
    sub.add_parser("status", help="count the kit's objects and any other objects with its prefix")
    args = parser.parse_args()
    handlers = {
        "init": cmd_init,
        "admin-init": cmd_admin_init,
        "apply": cmd_apply,
        "index": cmd_index,
        "clean": cmd_clean,
        "status": cmd_status,
    }
    try:
        if args.command != "init":
            check_state()
        handlers[args.command](args)
    except (KitError, ApiError) as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
