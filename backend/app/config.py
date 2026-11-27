import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

# Query parameters of a database URL that carry a secret (libpq reads both),
# hidden like the password whenever the URL is shown.
_SECRET_URL_PARAMETERS = frozenset({"password", "sslpassword"})
_POSTGRES_SCHEMES = ("postgres://", "postgresql://")


def value_or_file(name: str, value: str, file: str) -> str:
    """A setting that can also be read from a file named by NAME_FILE (a
    Docker or Kubernetes secret): the file's content, without the trailing
    line break, when only the file is set; the value otherwise. Setting both
    is refused, as is a file that can't be read. The messages never carry
    the value or the file's content."""
    if not file:
        return value
    if value:
        raise ValueError(f"{name} and {name}_FILE are both set; set only one of them")
    try:
        content = Path(file).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ValueError(f"{name}_FILE names a file that doesn't exist: {file}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{name}_FILE can't be read: {file} ({type(exc).__name__})") from None
    content = content.rstrip("\r\n")
    if not content.strip():
        raise ValueError(f"{name}_FILE names an empty file: {file}")
    return content


def normalize_database_url(url: str) -> str:
    """postgres:// and postgresql:// (what PostgreSQL's own tools and most
    hosting providers hand out) use psycopg 3, the driver PerchTail ships;
    SQLAlchemy would otherwise look for psycopg2."""
    for scheme in _POSTGRES_SCHEMES:
        if url.startswith(scheme):
            return "postgresql+psycopg://" + url.removeprefix(scheme)
    return url


def redacted_database_url(url: str) -> str:
    """The URL as it may be logged or shown: without the password, in the
    user part or in the query. A URL that can't be parsed is not shown at
    all, since where its password is can't be told."""
    try:
        parsed = make_url(url)
    except ArgumentError:
        return "<unparsable database URL>"
    hidden = {key: "***" for key in parsed.query if key.lower() in _SECRET_URL_PARAMETERS}
    if hidden:
        parsed = parsed.update_query_dict(hidden)
    return parsed.render_as_string(hide_password=True)


def is_postgresql_url(url: str) -> bool:
    return url.startswith("postgresql")


@lru_cache(maxsize=8)
def parse_networks(raw: str) -> tuple[Network, ...]:
    """A comma-separated list of addresses or CIDR blocks (#366). Raises
    ValueError naming the entry that isn't one."""
    networks = []
    for entry in (part.strip() for part in raw.split(",")):
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(f"{entry!r} is not an IP address or CIDR block") from exc
    return tuple(networks)


class Settings(BaseSettings):
    # hide_input_in_errors: a refused setting is reported without the values
    # read, which include DATABASE_URL (with its password) and the keys.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30
    # The active log file rotates at this size or at midnight, whichever
    # comes first; all logs together are kept under LOG_MAX_TOTAL_MB by
    # deleting the oldest archives (#172, app/log_rotation.py). 0 disables
    # either limit.
    log_max_file_mb: float = 100
    log_max_total_mb: float = 2048
    # Caps protocol libraries (smbprotocol, paramiko, pypsrp, ...) at INFO
    # while the app itself logs at DEBUG: at DEBUG their messages can carry
    # what is read from sources, before redaction, and slow reads down. Only
    # an explicit false lets them log at DEBUG, with a warning at startup.
    log_quiet_third_party: bool = True
    # The state that must be backed up: the database, the credential salt
    # and SSH known_hosts, unless DATABASE_URL / CREDENTIAL_SALT_PATH /
    # SSH_KNOWN_HOSTS_PATH point elsewhere. Kept apart from LOG_DIR and
    # SCRATCH_DIR so those can live on another disk and a runaway log can't
    # fill the database's space (#172). Before 0.5.0 these files sat in
    # DATA_DIR's parent; see app/data_layout.py for the move.
    data_dir: str = "./data/db"
    # Empty: SQLite in DATA_DIR/perchtail.db. postgresql:// (or postgres://)
    # uses a PostgreSQL 18 database through psycopg 3; TLS and the other libpq
    # options go in the URL's query (sslmode=verify-full&sslrootcert=...).
    # DATABASE_URL_FILE reads it from a file instead (a secret), never both.
    database_url: str = ""
    database_url_file: str = ""
    # PostgreSQL only: connections kept open, and how many more a burst may
    # open on top of them.
    database_pool_size: int = 10
    database_max_overflow: int = 20
    # PostgreSQL only: a database that isn't on this host (loopback or a
    # Unix socket) must be reached with sslmode=verify-ca or verify-full,
    # or startup is refused (app/db.py, check_database_server). false lets it
    # start with a warning instead (a private network such as Compose's).
    database_require_verified_tls: bool = True
    credential_encryption_key: str = "changeme"
    # Or a file holding it (a Docker or Kubernetes secret mounted as a file):
    # its content, without the final line break. Setting both is refused.
    credential_encryption_key_file: str = ""
    # A key that fails the strength check (app/key_strength.py) stops
    # PerchTail from starting. false lets it start with a warning instead:
    # a temporary way out while the key is rotated.
    credential_key_refuse_weak: bool = True
    # The interactive API documentation (/docs) and the OpenAPI schema
    # (/openapi.json) answer 404 unless this is true: the API is internal to
    # the web UI, and the schema maps every endpoint for whoever reaches it.
    api_docs: bool = False
    # Per-install random salt for deriving the Fernet key from
    # credential_encryption_key (see app/crypto.py) — generated on first use
    # and persisted here, not stored in the DB, since crypto.py has no DB
    # session and needs this before any request touches the database.
    credential_salt_path: str = ""
    # Escape hatch for a deployment whose original salt is permanently lost:
    # by default startup refuses to generate a fresh salt while the database
    # already holds data encrypted under the old one (see
    # bootstrap.assert_credential_salt_present). Setting this lets it start
    # anyway -- every stored credential, SSO secret and MFA secret then has
    # to be re-entered, and older audit rows will fail integrity checks.
    credential_salt_allow_regenerate: bool = False
    # Persisted across connections so paramiko can detect a host key that
    # changed since the last connection (see app/collectors/ssh.py) — a
    # fresh SSHClient is created per call with no persistence otherwise,
    # so every connection would silently trust whatever key is presented.
    ssh_known_hosts_path: str = ""
    # What to do with a host that has no key in ssh_known_hosts yet: "tofu"
    # pins it on first connect (and logs the fingerprint), "strict" refuses
    # to connect until an operator adds the key to the file by hand.
    ssh_host_key_policy: Literal["tofu", "strict"] = "tofu"
    # Username for the auto-seeded break-glass super-admin (see
    # app/bootstrap.py's seed_initial_super_admin) — only used on a fresh
    # deployment with zero users; its password is randomly generated, never
    # read from config, and written to DATA_DIR/initial-admin-password
    # (mode 0600, deleted once it is changed); the log only names the file.
    initial_admin_username: str = "admin"
    # The externally-reachable URL this deployment is served at — used to
    # build the OIDC redirect_uri registered with the IdP. Deriving this from
    # the incoming request instead would mean trusting X-Forwarded-* headers
    # from the reverse proxy PerchTail sits behind; an explicit setting sidesteps that
    # trust question entirely. Must match what's registered as the redirect
    # URI in the IdP's client configuration.
    public_base_url: str = "http://localhost:8080"
    session_ttl_hours: int = 12
    # Cookie's Secure flag — disable only for local HTTP development; a
    # browser silently drops Secure cookies over plain HTTP.
    session_cookie_secure: bool = True
    # The OIDC issuer and every endpoint its discovery document names must be
    # https:// (app/auth/providers/oidc.py). For local development only, this
    # also accepts http:// on localhost, 127.0.0.1 and ::1; never elsewhere.
    sso_allow_http_localhost: bool = False

    # Login brute-force lockout (app/login_throttle.py) — consecutive failed
    # attempts against one username before it's locked out, and how long the
    # lockout lasts once triggered.
    login_max_attempts: int = 5
    login_lockout_seconds: int = 300

    # Comma-separated absolute directories that admin-created `local`
    # (local-disk) sources may point at -- e.g. a host log directory mounted
    # read-only into the container. Empty by default, which disables
    # user-created local sources entirely: a web admin choosing an
    # arbitrary base_path would otherwise expose this server's own files
    # (database, secrets). Only the operator, via the environment, decides.
    # The built-in system log source is exempt (see collectors/local.py).
    local_source_roots: str = ""

    scratch_dir: str = "./data/scratch"
    scratch_max_gb: float = 5.0
    # Backstop for crashed/disconnected clients that never send a close
    # signal — deletes anything untouched past this, regardless of refcount.
    scratch_idle_seconds: int = 300
    scratch_sweep_interval_seconds: int = 60
    # Free space to keep on the filesystem holding SCRATCH_DIR (#345): a new
    # fetch is refused (507) once free space would drop below the larger of
    # SCRATCH_MIN_FREE_GB and SCRATCH_MIN_FREE_PERCENT of the filesystem, so
    # a full scratch can't stop the database and the logs sharing its disk.
    # 0 for both turns the check off.
    scratch_min_free_gb: float = 1.0
    scratch_min_free_percent: float = 5.0
    # Largest output one .gz file or one .zip/.tar.gz member may decompress
    # to before the fetch is refused -- a tiny crafted archive could
    # otherwise expand to fill the scratch disk.
    max_decompressed_mb: float = 2048.0
    # Largest single file a remote connector will fetch into scratch, and the
    # largest total a folder ZIP may collect -- keeps one request from filling
    # the scratch disk or (for WinRM/agent) memory.
    max_fetch_mb: float = 2048.0
    # Longest a remote source may go without answering once connected (SFTP
    # channel reads, SMB requests) before the call fails (#343), so a server
    # that stalls after login can't hold a worker thread forever.
    remote_read_timeout_seconds: int = 60
    # How long shutdown waits for running background jobs and the worker
    # pools to finish (#335). Keep the container's stop grace period above it.
    shutdown_timeout_seconds: int = 20

    # SEARCH_INDEX_* settings went with the content index (#303); extra="ignore"
    # above keeps an old .env that still sets them starting.

    # Release check (#215): at most once a day, ask GitHub for the latest
    # release and show it to super admins under Settings -> Backups and
    # updates. On by default; false means no outbound request is ever made
    # (air-gapped installs), whatever the switch on that page says.
    update_check: bool = True
    # How this instance was installed, for the upgrade steps it shows. The
    # Docker image sets PERCHTAIL_INSTALL=docker and the Helm chart
    # PERCHTAIL_INSTALL=kubernetes; anything else counts as an install from
    # source.
    perchtail_install: str = ""
    # Push-agent upgrades from the portal (#319): the folder holding the
    # signed agent binaries of this server's version (the release's
    # perchtail-agent-*, SHA256SUMS and SHA256SUMS.sig), served to connected
    # agents at /agent/binaries/. The Docker image sets /app/agent-binaries;
    # empty, or a folder without them, means agents download the release
    # from GitHub instead.
    agent_binaries_dir: str = ""

    # Full audit log viewer (app/audit_purge.py) -- how often the purge
    # sweep runs. *What* it purges (retention in days, 0 = keep forever) is
    # admin-configurable from Settings -> Audit log (SystemSetting), not an env
    # var, per docs/design-decisions.md ("Audit log"); only the sweep cadence
    # itself is an operational/deployment concern like the other interval
    # settings above.
    audit_purge_interval_seconds: int = 86400

    # Audit log tamper-evidence (app/audit_integrity.py) -- how often the
    # hash-chain verification job re-walks AuditLog. Deliberately its own
    # setting, not reused from audit_purge_interval_seconds or
    # audit_retention_days above -- retention decides what's kept, this
    # decides how often what's kept gets re-verified, and coupling them
    # would mean a retention change silently also changing how often
    # tampering gets caught. In days, not seconds, since a year expressed in
    # seconds (31536000) is unreadable at a glance and APScheduler's
    # IntervalTrigger accepts a `days=` kwarg directly.
    audit_integrity_check_interval_days: int = 1

    # Reverse proxies whose X-Forwarded-For is believed (app/request_context.py):
    # comma-separated addresses or CIDR blocks. The client address recorded
    # in the audit log, and checked by the monitoring allowlists below, is
    # the first address in X-Forwarded-For, read from the right, that isn't
    # one of these; a request from anywhere else keeps its socket address
    # and its X-Forwarded-For is ignored. Loopback only by default.
    trusted_proxies: str = "127.0.0.0/8, ::1"

    # Who may call the monitoring endpoints without the monitoring token
    # (#366, app/monitoring_access.py): comma-separated addresses or CIDR
    # blocks. PROBE_ALLOWED_NETWORKS opens /healthz and /readyz to a load
    # balancer or kubelet that can't send a header; loopback without proxy
    # headers is always allowed. MONITORING_ALLOWED_NETWORKS, when set,
    # restricts /monitoring/health and /metrics to those callers on top of
    # the token.
    probe_allowed_networks: str = ""
    monitoring_allowed_networks: str = ""

    # Online backups (#334, app/backup.py): where `python -m app.backup create`
    # and the scheduled backup write their encrypted archives (by default
    # `backups` next to DATA_DIR, /data/backups in the image), how many
    # manual and scheduled ones to keep (the last 3 pre-upgrade ones are kept
    # on their own), and an optional key of their own. Without it, the
    # archive key is derived from CREDENTIAL_ENCRYPTION_KEY.
    backup_dir: str = ""
    backup_keep: int = 7
    backup_encryption_key: str = ""

    # Which of the three DATA_DIR paths were left to default, so the legacy
    # layout move (app/data_layout.py) never touches an explicit setting.
    derived_from_data_dir: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        derived = set()
        self.database_url = value_or_file(
            "DATABASE_URL", self.database_url, self.database_url_file
        ).strip()
        if not self.database_url:
            self.database_url = f"sqlite:///{os.path.join(self.data_dir, 'perchtail.db')}"
            derived.add("database")
        self.database_url = normalize_database_url(self.database_url)
        try:
            make_url(self.database_url)
        except ArgumentError:
            # SQLAlchemy's own message quotes the URL, password included.
            raise ValueError("DATABASE_URL is not a valid database URL") from None
        if self.database_pool_size < 1 or self.database_max_overflow < 0:
            raise ValueError(
                "DATABASE_POOL_SIZE must be at least 1 and DATABASE_MAX_OVERFLOW at least 0"
            )
        if not self.credential_salt_path:
            self.credential_salt_path = os.path.join(self.data_dir, "credential_salt")
            derived.add("credential_salt")
        if not self.ssh_known_hosts_path:
            self.ssh_known_hosts_path = os.path.join(self.data_dir, "ssh_known_hosts")
            derived.add("ssh_known_hosts")
        self.derived_from_data_dir = frozenset(derived)
        if not self.backup_dir:
            parent = os.path.dirname(os.path.normpath(self.data_dir)) or "."
            self.backup_dir = os.path.join(parent, "backups")
        return self

    @model_validator(mode="after")
    def _read_key_file(self) -> "Settings":
        if not self.credential_encryption_key_file:
            return self
        # The field has a default ("changeme"), so only a key actually set
        # in the environment counts as "both set".
        explicit = (
            self.credential_encryption_key
            if "credential_encryption_key" in self.model_fields_set
            else ""
        )
        self.credential_encryption_key = value_or_file(
            "CREDENTIAL_ENCRYPTION_KEY", explicit, self.credential_encryption_key_file
        )
        return self

    @model_validator(mode="after")
    def _check_networks(self) -> "Settings":
        # Refuse to start on a typo rather than silently allow no one (or
        # everyone) through.
        for name in ("probe_allowed_networks", "monitoring_allowed_networks", "trusted_proxies"):
            try:
                parse_networks(getattr(self, name))
            except ValueError as exc:
                raise ValueError(f"{name.upper()}: {exc}") from exc
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
