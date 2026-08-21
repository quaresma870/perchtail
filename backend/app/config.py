from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30
    database_url: str = "sqlite:///./data/perchtail.db"
    credential_encryption_key: str = "changeme"
    # Per-install random salt for deriving the Fernet key from
    # credential_encryption_key (see app/crypto.py) — generated on first use
    # and persisted here, not stored in the DB, since crypto.py has no DB
    # session and needs this before any request touches the database.
    credential_salt_path: str = "./data/credential_salt"
    # Persisted across connections so paramiko can detect a host key that
    # changed since the last connection (see app/collectors/ssh.py) — a
    # fresh SSHClient is created per call with no persistence otherwise,
    # so every connection would silently trust whatever key is presented.
    ssh_known_hosts_path: str = "./data/ssh_known_hosts"
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

    scratch_dir: str = "./data/scratch"
    scratch_max_gb: float = 5.0
    # Backstop for crashed/disconnected clients that never send a close
    # signal — deletes anything untouched past this, regardless of refcount.
    scratch_idle_seconds: int = 300
    scratch_sweep_interval_seconds: int = 60

    # Phase 3 full-text search (app/search_index.py) — how often the
    # background indexer sweeps opted-in sources, and the largest single
    # file it will read into memory to index (skips anything bigger, same
    # spirit as the scratch size-guard: a safety valve, not a design goal).
    search_index_interval_seconds: int = 300
    search_index_max_file_size_mb: float = 20.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
