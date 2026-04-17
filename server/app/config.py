from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+asyncpg://mnemos:mnemos@localhost:5432/mnemos"
    )
    redis_url: str = Field(default="redis://localhost:6379/0")
    secret_key: str = Field(default="change-me-in-production")
    fernet_key: str = Field(default="")
    log_level: str = Field(default="INFO")
    session_cookie_name: str = Field(default="mnemos_session")
    session_max_age_sec: int = Field(default=60 * 60 * 24 * 7)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
