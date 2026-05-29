"""PR-110 — startup-time self-verify.

The CLI ``verify`` (PR-102) is an operator-initiated probe.
That's useful but optional. A platform deployed under
``docker compose up`` with a broken FERNET_KEY or a
mis-rotated SECRET_KEY currently still BOOTS — every request
just 500s or every secret decrypt throws on first use.

This module runs the same checks at FastAPI startup. Hard
failures (config validation, crypto round-trip) raise so
uvicorn never finishes binding the port. Soft failures (DB
briefly unreachable during a rolling restart, missing
analyzer binaries) log a warning and proceed — the
``/health/ready`` deep-check (PR-99) catches those and the
load balancer keeps draining.

The split between hard and soft mirrors the spec:
- Config must be valid. A booted-but-unsafe server is worse
  than no server (operator might think it's working).
- Crypto round-trip must work. A broken FERNET_KEY means the
  first secret read throws — by then a webhook may already
  be enqueued.
- DB / Redis being briefly down at startup is normal during
  ``docker compose restart`` — soft.
- Analyzer binaries are advisory by design (PR-98).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class StartupCheckFailed(RuntimeError):
    """Raised on a hard-fail. The exception propagating out of
    the lifespan handler causes uvicorn to exit non-zero — exactly
    what we want for a docker-compose health check."""


async def _check_config() -> None:
    """Re-run get_settings() so the PR-97 placeholder-SECRET_KEY
    guard fires. Already runs once via the global import, but
    bringing it here makes the check explicit + adds the failure
    to the startup log under a recognisable prefix."""
    from app.config import get_settings

    # get_settings() is lru_cached so this is essentially free
    # — we're calling it for the side-effect of its safety check.
    settings = get_settings()
    log.info("startup_verify.config OK env=%s", settings.mnemos_env)


async def _check_crypto() -> None:
    """Encrypt + decrypt a probe via the configured KMS backend.
    Catches a mis-rotated FERNET_KEY before the first real secret
    read does."""
    from app.safety.crypto import decrypt, encrypt

    probe = "mnemos-startup-probe"
    ct, iv = encrypt(probe)
    if decrypt(ct, iv) != probe:
        raise StartupCheckFailed("crypto.round_trip_mismatch")
    log.info("startup_verify.crypto OK")


async def _check_db_soft() -> bool:
    """Best-effort DB probe. Returns False (with a log) on failure
    — startup proceeds, /health/ready will flip 503 until it
    recovers. Returning ``False`` rather than raising matches the
    spec: "the platform should keep running through transient
    dependency blips" (§2.7 always-on)."""
    try:
        from sqlalchemy import text

        from app.db import SessionLocal

        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
        log.info("startup_verify.database OK")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "startup_verify.database FAILED (soft): %s: %s — /health/ready will reflect this",
            exc.__class__.__name__, exc,
        )
        return False


async def _check_redis_soft() -> bool:
    try:
        from app.orchestrator.redis_pool import get_redis

        r = await get_redis()
        pong = await r.ping()
        if not pong:
            raise RuntimeError("no pong")
        log.info("startup_verify.redis OK")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "startup_verify.redis FAILED (soft): %s: %s — /health/ready will reflect this",
            exc.__class__.__name__, exc,
        )
        return False


def _check_analyzers_soft() -> None:
    """Log which analyzer binaries are missing on PATH. Always
    advisory — operators may deliberately skip languages they
    don't ship (PR-98)."""
    import shutil

    from app.analyzers.registry import _BINARIES

    missing = [b for b in _BINARIES.values() if shutil.which(b) is None]
    if missing:
        log.warning(
            "startup_verify.analyzers missing on PATH: %s "
            "(stages will skip; run `docker compose --profile analyzers build`)",
            ",".join(sorted(missing)),
        )
    else:
        log.info("startup_verify.analyzers OK")


async def run_startup_verify() -> None:
    """Orchestrate the boot-time checks. Hard fails raise
    :class:`StartupCheckFailed`; soft fails are logged but allow
    the platform to come up."""
    await _check_config()
    try:
        await _check_crypto()
    except StartupCheckFailed:
        raise
    except Exception as exc:  # noqa: BLE001
        # Anything other than the explicit StartupCheckFailed
        # (e.g. cryptography ImportError, missing key) is also a
        # hard fail by design — a broken crypto path = unsafe boot.
        raise StartupCheckFailed(
            f"crypto.{exc.__class__.__name__}: {exc}"
        ) from exc
    await _check_db_soft()
    await _check_redis_soft()
    _check_analyzers_soft()
    log.info("startup_verify complete")
