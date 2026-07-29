from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30
    database_url: str = "sqlite:///./data/perchtail.db"
    credential_encryption_key: str = "changeme"
    # Username for the auto-seeded break-glass super-admin (see
    # app/bootstrap.py's seed_initial_super_admin) — only used on a fresh
    # deployment with zero users; its password is randomly generated and
    # logged once, never read from config.
    initial_admin_username: str = "admin"
    session_ttl_hours: int = 12
    # Cookie's Secure flag — disable only for local HTTP development; a
    # browser silently drops Secure cookies over plain HTTP.
    session_cookie_secure: bool = True

    scratch_dir: str = "./data/scratch"
    scratch_max_gb: float = 5.0
    # Backstop for crashed/disconnected clients that never send a close
    # signal — deletes anything untouched past this, regardless of refcount.
    scratch_idle_seconds: int = 300
    scratch_sweep_interval_seconds: int = 60


@lru_cache
def get_settings() -> Settings:
    return Settings()
