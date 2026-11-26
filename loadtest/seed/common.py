"""Shared helpers for the load-test kit: paths, the profile, the local state
directory, the target-host allow-list and a small HTTP client for
PerchTail's API. Standard library only.

Secrets (passwords, tokens) only ever live in the state directory
(loadtest/.state/ by default, ignored by git) or in environment variables;
nothing here prints them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import string
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

LOADTEST_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = LOADTEST_DIR.parent
PROFILES_DIR = LOADTEST_DIR / "profiles"
DEFAULT_PREFIX = "lt"
_PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{0,15}$")


class KitError(Exception):
    """A problem the operator has to fix (missing setting, refused host)."""


def prefix() -> str:
    """LOADTEST_PREFIX (default `lt`): the first part of every name the kit
    gives the objects it creates, `<prefix>-customer-01` and so on. Another
    prefix gives a fresh set of names, for when the default ones are taken
    by accounts PerchTail can't rename or delete."""
    value = os.environ.get("LOADTEST_PREFIX", DEFAULT_PREFIX).strip()
    if not _PREFIX_RE.match(value):
        raise KitError(
            f"LOADTEST_PREFIX must be a lower-case letter followed by up to 15 lower-case "
            f"letters or digits, got {value!r}"
        )
    return value


def name_prefix() -> str:
    return f"{prefix()}-"


# --- paths and state ------------------------------------------------------
#
# The state directory (loadtest/.state/ by default) holds what doesn't depend
# on an instance at its root: the test servers' account (sources.env), the
# agent binary (bin/) and the generated log files (logs/). Everything about
# one instance (admin account, users, ledger, saved settings, tokens) lives in
# instances/<host>-<port>/, and every file there records the URL it was
# written for: a command refuses state written for another URL.

# Files of the earlier, single-instance layout, at the root of the state
# directory. They aren't tied to an instance, so the kit refuses to guess.
_LEGACY_FILES = (
    "admin.json",
    "users.json",
    "seeded.json",
    "agent-tokens.json",
    "agent-pids.json",
    "monitoring.json",
    "settings-original.json",
    "catalog.json",
)


def state_root() -> Path:
    path = Path(os.environ.get("LOADTEST_STATE_DIR") or LOADTEST_DIR / ".state")
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalized_url(url: str) -> str:
    """scheme://host:port/path, lower-case host, explicit port, no trailing
    slash: the form recorded in the state files and compared."""
    parts = urllib.parse.urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    shown = f"[{host}]" if ":" in host else host
    return f"{parts.scheme.lower()}://{shown}:{port}{parts.path.rstrip('/')}"


def instance_key(url: str) -> str:
    """The instance's folder name, from its host and port."""
    parts = urllib.parse.urlsplit(normalized_url(url))
    host = re.sub(r"[^a-z0-9.-]", "_", (parts.hostname or "").lower())
    return f"{host}-{parts.port}"


def state_dir() -> Path:
    """The state of the instance LOADTEST_BASE_URL names."""
    root = state_root()
    legacy = [name for name in _LEGACY_FILES if (root / name).exists()]
    if legacy:
        raise KitError(
            f"{root} has state from an earlier version of the kit that isn't tied to an "
            f"instance ({', '.join(legacy)}): clean that instance with the earlier version, "
            "or remove those files (keep admin credentials somewhere safe first)"
        )
    path = root / "instances" / instance_key(base_url())
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir() -> Path:
    """Where gen_logs.py writes the served files; the same value
    compose.sources.yml mounts (LOADTEST_LOGS_DIR)."""
    raw = os.environ.get("LOADTEST_LOGS_DIR")
    if raw:
        path = Path(raw)
        if not path.is_absolute():
            path = REPO_DIR / path
    else:
        path = state_root() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def results_root() -> Path:
    path = Path(os.environ.get("LOADTEST_RESULTS_DIR") or LOADTEST_DIR / "results")
    path.mkdir(parents=True, exist_ok=True)
    return path


def read_state(name: str, default: Any = None) -> Any:
    """A state file of this instance, refused when it was written for
    another URL."""
    path = state_dir() / name
    if not path.exists():
        return default
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or "base_url" not in payload or "data" not in payload:
        raise KitError(f"{path} doesn't record the instance it belongs to; remove it")
    expected = normalized_url(base_url())
    if payload["base_url"] != expected:
        raise KitError(
            f"{path} was written for {payload['base_url']}, not {expected}: refusing to use "
            "another instance's state"
        )
    return payload["data"]


def write_state(name: str, data: Any) -> Path:
    """Writes a state file of this instance, with its URL, readable by its
    owner only (where the OS has POSIX permissions), replacing it
    atomically."""
    path = state_dir() / name
    if path.exists():
        read_state(name)  # never overwrite another instance's file
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(
            {"base_url": normalized_url(base_url()), "data": data}, handle, indent=2, sort_keys=True
        )
        handle.write("\n")
    os.replace(tmp, path)
    return path


def check_state() -> None:
    """Refuses, before a command does anything, when any state file of the
    instance was written for another URL."""
    for path in sorted(state_dir().glob("*.json")):
        read_state(path.name)


def remove_state(name: str) -> None:
    path = state_dir() / name
    if path.exists():
        read_state(name)
        path.unlink()


# --- the restore test's copy (matrix.py, step `operation`) ----------------
#
# A restore runs only into a DATA_DIR of its own inside the container, made
# by the kit with mktemp and recorded here before anything is written there;
# `seed.py clean` removes a copy a run left behind.

RESTORE_STATE = "restore-copy.json"


def restore_copy_pattern() -> re.Pattern:
    """The only directories a restore may write to: what `mktemp -d
    /tmp/<prefix>-restore-XXXXXX` makes."""
    return re.compile(rf"^/tmp/{re.escape(prefix())}-restore-[A-Za-z0-9]{{6,}}$")


def restore_env(container: str, copy: dict | None) -> dict:
    """The environment a restore runs with: every place it writes (DATA_DIR,
    the database, the credential salt, the SSH host keys and its working
    folder) inside the copy the kit made for it in `container` and recorded
    in the instance's state. Anything else is refused, so a restore never
    lands on the live database, whatever DATABASE_URL, CREDENTIAL_SALT_PATH
    or SSH_KNOWN_HOSTS_PATH the container has."""
    if not container or not copy or copy.get("container") != container:
        raise KitError(
            "no restore copy recorded for this container: a restore runs only into a DATA_DIR "
            "the kit made for it"
        )
    target = str(copy.get("dir") or "")
    if not restore_copy_pattern().match(target):
        raise KitError(f"{target!r} isn't a restore copy the kit made; refusing to restore")
    return {
        "DATA_DIR": target,
        "DATABASE_URL": f"sqlite:///{target}/perchtail.db",
        "CREDENTIAL_SALT_PATH": f"{target}/credential_salt",
        "SSH_KNOWN_HOSTS_PATH": f"{target}/ssh_known_hosts",
        "BACKUP_DIR": f"{target}-work",
    }


def restore_cleanup(target: str) -> str:
    """The shell command that removes a restore copy and what the restore
    left next to it; only for a path restore_env accepts."""
    if not restore_copy_pattern().match(target):
        raise KitError(f"{target!r} isn't a restore copy the kit made")
    return f"rm -rf {target} {target}.before-restore-* {target}.restoring-* {target}-work"


def random_password(length: int = 32) -> str:
    """Random, and inside any sensible password policy: every character
    class, no dictionary words, long enough."""
    alphabet = string.ascii_letters + string.digits + "-_.!@#%^*+="
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in value)
            and any(c.isupper() for c in value)
            and any(c.isdigit() for c in value)
            and any(not c.isalnum() for c in value)
        ):
            return value


# --- profile ------------------------------------------------------------


def load_profile(name_or_path: str) -> dict:
    path = Path(name_or_path)
    if not path.suffix:
        path = PROFILES_DIR / f"{name_or_path}.json"
    if not path.exists():
        raise KitError(f"profile not found: {path}")
    profile = json.loads(path.read_text(encoding="utf-8"))
    profile["_path"] = str(path.resolve())
    return profile


# --- target host ----------------------------------------------------------


def allowed_hosts() -> set[str]:
    raw = os.environ.get("LOADTEST_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def base_url() -> str:
    """LOADTEST_BASE_URL, required and never defaulted, and only when its
    host is in LOADTEST_ALLOWED_HOSTS: nobody launches load at someone
    else's instance by mistake."""
    url = os.environ.get("LOADTEST_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise KitError(
            "LOADTEST_BASE_URL is required (e.g. http://localhost:8080); it has no default"
        )
    check_allowed(url)
    return url


def check_allowed(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise KitError(f"LOADTEST_BASE_URL must be an http(s) URL with a host, got {url!r}")
    hosts = allowed_hosts()
    if not hosts:
        raise KitError("LOADTEST_ALLOWED_HOSTS is required: the host names this kit may load")
    if parts.hostname.lower() not in hosts:
        raise KitError(
            f"refusing to run against {parts.hostname!r}: it isn't in LOADTEST_ALLOWED_HOSTS"
        )


# --- HTTP client --------------------------------------------------------


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, detail: str):
        super().__init__(f"{method} {path} -> {status}: {detail}")
        self.status = status
        self.detail = detail


class Client:
    """A cookie-session client for PerchTail's API, sending the same
    headers a browser on the instance's own origin would (Origin, JSON).
    Request and response bodies are never logged."""

    def __init__(self, url: str | None = None, timeout: float = 120.0):
        self.url = (url or base_url()).rstrip("/")
        parts = urllib.parse.urlsplit(self.url)
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.bearer: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        expect: tuple[int, ...] = (200, 201, 202, 204),
        raw: bool = False,
    ) -> Any:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            req.add_header("Origin", self.origin)
        if self.bearer:
            req.add_header("Authorization", f"Bearer {self.bearer}")
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                status, payload = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, payload = exc.code, exc.read()
        if status not in expect:
            detail = _detail(payload)
            raise ApiError(method, path, status, detail)
        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload)
        except ValueError:
            return payload.decode("utf-8", "replace")

    def get(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self.request("POST", path, body, **kw)

    def patch(self, path: str, body: Any, **kw: Any) -> Any:
        return self.request("PATCH", path, body, **kw)

    def put(self, path: str, body: Any, **kw: Any) -> Any:
        return self.request("PUT", path, body, **kw)

    def delete(self, path: str, **kw: Any) -> Any:
        return self.request("DELETE", path, **kw)

    def login(self, username: str, password: str, totp_secret: str | None = None) -> dict:
        """Signs in; with `totp_secret`, with a TOTP code of a time step the
        account hasn't used yet (fresh_totp)."""
        body = {"username": username, "password": password}
        if totp_secret:
            body["mfa_code"] = fresh_totp(username, totp_secret)
        return self.post("/auth/login", body)


def totp(secret: str, counter: int, digits: int = 6) -> str:
    """The TOTP code (RFC 6238, SHA-1) of a base32 secret for a time step."""
    key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 10**digits
    return f"{code:0{digits}d}"


TOTP_STEP_SECONDS = 30
TOTP_STEPS_STATE = "totp-steps.json"


def fresh_totp(username: str, secret: str) -> str:
    """A code for a time step this account hasn't used yet: PerchTail takes
    each step's code once (replay protection), and the kit signs the admin
    in from several commands in a row, so it waits for the next step when
    the current one was already used. The last step used is kept in the
    instance's state."""
    used: dict = read_state(TOTP_STEPS_STATE, {})
    step = int(time.time() // TOTP_STEP_SECONDS)
    last = int(used.get(username, -1))
    if step <= last:
        time.sleep((last + 1) * TOTP_STEP_SECONDS - time.time() + 0.5)
        step = last + 1
    used[username] = step
    write_state(TOTP_STEPS_STATE, used)
    return totp(secret, step)


def _detail(payload: bytes) -> str:
    try:
        body = json.loads(payload)
    except ValueError:
        return payload[:200].decode("utf-8", "replace")
    detail = body.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        detail = detail.get("message") or detail
    return str(detail)[:300]


def admin_client() -> Client:
    """Logged in as the admin the kit uses to seed: LOADTEST_ADMIN_USERNAME
    and LOADTEST_ADMIN_PASSWORD when set (an instance in a dedicated test
    environment), otherwise the account `seed.py admin-init` set up
    (admin.json in the instance's state)."""
    username = os.environ.get("LOADTEST_ADMIN_USERNAME")
    password = os.environ.get("LOADTEST_ADMIN_PASSWORD")
    totp_secret = os.environ.get("LOADTEST_ADMIN_TOTP_SECRET")
    if not (username and password):
        saved = read_state("admin.json")
        if not saved:
            raise KitError(
                "no admin credentials: set LOADTEST_ADMIN_USERNAME/LOADTEST_ADMIN_PASSWORD, "
                "or run `seed.py admin-init` on a fresh instance"
            )
        username, password = saved["username"], saved["password"]
        totp_secret = saved.get("totp_secret")
    client = Client()
    user = client.login(username, password, totp_secret)
    if user.get("must_change_password"):
        raise KitError(f"the admin account {username!r} must change its password first")
    if not user.get("is_super_admin"):
        raise KitError(f"the admin account {username!r} isn't a super admin")
    return client


SETTINGS_STATE = "settings-original.json"


def change_settings(client: Client, settings: dict) -> None:
    """Changes System settings, first saving the instance's value of each
    key the kit hasn't changed before, so `seed.py clean` puts back those
    keys only."""
    if not settings:
        return
    original: dict = read_state(SETTINGS_STATE, {})
    missing = [key for key in settings if key not in original]
    if missing:
        current = client.get("/system-settings")
        unknown = [key for key in missing if key not in current]
        if unknown:
            raise KitError(f"unknown System settings in the profile: {', '.join(unknown)}")
        original.update({key: current[key] for key in missing})
        write_state(SETTINGS_STATE, original)
    client.patch("/system-settings", settings)


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(2)
