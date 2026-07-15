from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# Known placeholder values an operator might leave in a fresh ``.env``.
# A production deploy that signs cookies with one of these is trivially
# forgeable, so ``get_settings`` refuses to load (raises) — same
# fail-closed pattern as the FERNET_KEY check in safety/kms.py.
_INSECURE_SECRET_KEYS = frozenset({
    "",
    "change-me-in-production",
    "change-me-to-a-random-32-byte-secret",
    "change-me",
    "changeme",
    "secret",
    "password",
})


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
    # Optional operator-managed Git mirror root for webhook analysis.  A
    # webhook never falls back to the worker's current directory: the exact
    # pushed SHA must already exist in ``<root>/<project_id>[.git]`` and is
    # materialised as a detached, read-only-by-convention worktree.
    source_mirror_root: str = Field(default="")
    source_worktree_root: str = Field(default="")
    # Optional root containing operator-selected manual source directories.
    # When set, workers reject manual paths outside it and completed Git runs
    # can re-open the exact commit without persisting a host-absolute path.
    source_allowed_root: str = Field(default="")
    # Required project-to-directory binding for manual analysis. JSON object
    # keys are project UUIDs and values are relative directories below
    # ``source_allowed_root``. A shared allowed root alone is not an
    # authorization boundary between tenants.
    source_project_roots: str = Field(default="")
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

    # PR-104 — outbound webhook for new P1 findings. Empty disables
    # notifications. Body is a small JSON envelope (project id, kind,
    # risk score, drill-down URL) — Slack incoming-webhook format
    # is the most common target. Best-effort: a failure is logged +
    # audited but never blocks the finding write.
    notify_webhook_url: str = Field(default="")
    # The base URL operators want in notification links — usually the
    # public hostname Mnemos serves on. Falls back to a relative path
    # so links still work when an operator forgets to set it.
    notify_link_base: str = Field(default="")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    s = Settings()
    # PR-97 — fail-closed on a placeholder SECRET_KEY in production. A
    # cookie signed with "change-me-..." is forgeable by anyone who has
    # read the README; the platform refuses to boot instead of running
    # with a forgeable session.
    if (s.mnemos_env or "").lower() == "production":
        key = (s.secret_key or "").strip().lower()
        if key in _INSECURE_SECRET_KEYS or key.startswith("change-me"):
            raise RuntimeError(
                "config: SECRET_KEY is a placeholder and MNEMOS_ENV=production. "
                "Set SECRET_KEY to a random 32+ char string. Generate with "
                "`python -c 'import secrets; print(secrets.token_urlsafe(48))'`."
            )
    return s
