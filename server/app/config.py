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
    # Set False only for local HTTP development. Production deployments
    # behind TLS must keep this True so the session cookie never traverses
    # plaintext, even momentarily during a misconfigured proxy hop.
    session_cookie_secure: bool = Field(default=True)
    # Distinguishes "this is dev" from "this is prod" for safety gates
    # that should fail-closed in production but be permissive locally.
    mnemos_env: str = Field(default="production")

    # OIDC / SSO (Phase C-2). Unset by default -> local auth only.
    oidc_issuer: str = Field(default="")
    oidc_client_id: str = Field(default="")
    oidc_client_secret: str = Field(default="")
    oidc_redirect_uri: str = Field(default="")
    oidc_scopes: str = Field(default="openid email profile")

    # Bearer token required to scrape /metrics. Empty -> open access
    # (use only when /metrics is restricted at the reverse proxy).
    metrics_bearer_token: str = Field(default="")

    # KMS backend selector (Phase C-3): "local" uses env FERNET_KEY,
    # "vault" fetches the DEK from HashiCorp Vault KV-v2 at startup.
    kms_backend: str = Field(default="local")
    # Retained for forward-compatibility; unused by the current backends.
    kms_key_arn: str = Field(default="")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
