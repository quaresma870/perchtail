import os
from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30
    # The active log file rotates at this size or at midnight, whichever
    # comes first; all logs together are kept under LOG_MAX_TOTAL_MB by
    # deleting the oldest archives (#172, app/log_rotation.py). 0 disables
    # either limit.
    log_max_file_mb: float = 100
    log_max_total_mb: float = 2048
    # Caps protocol libraries (smbprotocol, paramiko, ...) at INFO while the
    # app itself logs at DEBUG; their packet dumps are most of DEBUG's volume.
    log_quiet_third_party: bool = False
    # The state that must be backed up: the database, the credential salt
    # and SSH known_hosts, unless DATABASE_URL / CREDENTIAL_SALT_PATH /
    # SSH_KNOWN_HOSTS_PATH point elsewhere. Kept apart from LOG_DIR and
    # SCRATCH_DIR so those can live on another disk and a runaway log can't
    # fill the database's space (#172). Before 0.5.0 these files sat in
    # DATA_DIR's parent; see app/data_layout.py for the move.
    data_dir: str = "./data/db"
    database_url: str = ""
    credential_encryption_key: str = "changeme"
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
    # deployment with zero users; its password is randomly generated and
    # logged once, never read from config.
    initial_admin_username: str = "admin"
    # The externally-reachable URL this deployment is served at — used to
    # build the OIDC redirect_uri registered with the IdP. Deriving this from
    # the incoming request instead would mean trusting X-Forwarded-* headers
    # from whatever sits in front of this app (see CLAUDE.md's "sit behind an
    # nginx reverse proxy" packaging note); an explicit setting sidesteps that
    # trust question entirely. Must match what's registered as the redirect
    # URI in the IdP's client configuration.
    public_base_url: str = "http://localhost:8080"
    session_ttl_hours: int = 12
    # Cookie's Secure flag — disable only for local HTTP development; a
    # browser silently drops Secure cookies over plain HTTP.
    session_cookie_secure: bool = True

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
    # Largest output one .gz file or one .zip/.tar.gz member may decompress
    # to before the fetch is refused -- a tiny crafted archive could
    # otherwise expand to fill the scratch disk.
    max_decompressed_mb: float = 2048.0
    # Largest single file a remote connector will fetch into scratch, and the
    # largest total a folder ZIP may collect -- keeps one request from filling
    # the scratch disk or (for WinRM/agent) memory.
    max_fetch_mb: float = 2048.0

    # SEARCH_INDEX_* settings went with the content index (#303); extra="ignore"
    # above keeps an old .env that still sets them starting.

    # Release check (#215): at most once a day, ask GitHub for the latest
    # release and show it to super admins under Settings -> System. On by
    # default; false means no outbound request is ever made (air-gapped
    # installs), whatever the switch under System says.
    update_check: bool = True
    # How this instance was installed, for the upgrade steps it shows. The
    # Docker image sets PERCHTAIL_INSTALL=docker; anything else counts as an
    # install from source.
    perchtail_install: str = ""

    # Full audit log viewer (app/audit_purge.py) -- how often the purge
    # sweep runs. *What* it purges (retention in days, 0 = keep forever) is
    # admin-configurable from Settings -> System (SystemSetting), not an env
    # var, per ROADMAP.md's explicit direction; only the sweep cadence
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
    audit_integrity_check_interval_days: int = 365

    # Which of the three DATA_DIR paths were left to default, so the legacy
    # layout move (app/data_layout.py) never touches an explicit setting.
    derived_from_data_dir: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _derive_paths(self) -> "Settings":
        derived = set()
        if not self.database_url:
            self.database_url = f"sqlite:///{os.path.join(self.data_dir, 'perchtail.db')}"
            derived.add("database")
        if not self.credential_salt_path:
            self.credential_salt_path = os.path.join(self.data_dir, "credential_salt")
            derived.add("credential_salt")
        if not self.ssh_known_hosts_path:
            self.ssh_known_hosts_path = os.path.join(self.data_dir, "ssh_known_hosts")
            derived.add("ssh_known_hosts")
        self.derived_from_data_dir = frozenset(derived)
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
