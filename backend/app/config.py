from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    log_dir: str = "./data/logs"
    log_retention_days: int = 30
    database_url: str = "sqlite:///./data/perchtail.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
