"""Health endpoints.

Two variants:
- ``/api/v1/health``       : shallow liveness probe. Returns 200 as long
  as the FastAPI process is up. Intended for container orchestrator
  liveness checks where frequent failure would trigger noisy restarts.
- ``/api/v1/health/ready`` : deep readiness probe. Checks Postgres,
  Redis, and the ARQ worker heartbeat so a load balancer can drain
  traffic away from a broken instance. Returns 503 when any dependency
  is unhealthy.
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.orchestrator.redis_pool import get_redis

router = APIRouter(tags=["health"])

_WORKER_HEARTBEAT_KEY = "mnemos:worker:heartbeat"
_WORKER_STALE_AFTER_SEC = 90


@router.get("/api/v1/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@router.get("/api/v1/health/ready")
async def ready() -> JSONResponse:
    db_ok, db_msg = await _check_db()
    redis_ok, redis_msg = await _check_redis()
    worker_ok, worker_msg = await _check_worker()
    # PR-99 deep checks: ``crypto`` catches a wrong-rotated FERNET_KEY
    # before secrets start failing; ``analyzers`` surfaces which
    # language binaries are missing on PATH so an operator knows
    # which images to install. Both are advisory — they don't flip
    # overall=503 unless crypto is broken (secrets can't decrypt).
    crypto_ok, crypto_msg = _check_crypto()
    analyzers_status, analyzers_msg = _check_analyzers()
    llm_paid_ok, llm_paid_msg = _check_llm_paid_dispatch()

    parts = {
        "database": {"ok": db_ok, "message": db_msg},
        "redis": {"ok": redis_ok, "message": redis_msg},
        "worker": {"ok": worker_ok, "message": worker_msg},
        "crypto": {"ok": crypto_ok, "message": crypto_msg},
        "analyzers": {"ok": analyzers_status, "message": analyzers_msg},
        "llm_paid_dispatch": {"ok": llm_paid_ok, "message": llm_paid_msg},
    }
    # Analyzers being partially installed is degraded-but-running.
    # Crypto failing means every secret decrypt will throw → 503.
    overall = db_ok and redis_ok and worker_ok and crypto_ok
    status = 200 if overall else 503
    return JSONResponse(
        status_code=status,
        content={"status": "ok" if overall else "degraded", "checks": parts},
    )


def _check_llm_paid_dispatch() -> tuple[bool, str]:
    """Report whether new paid LLM attempts have explicit dollar authority.

    This is advisory for service readiness because deterministic indexing and
    replay remain available without a paid-provider budget. Production LLM
    callsites independently enforce the same policy in ``begin_attempt``.
    """

    from app.extractor.cost import (
        BudgetConfigurationError,
        configured_project_dollar_budget,
    )

    try:
        policy = configured_project_dollar_budget()
    except BudgetConfigurationError:
        return False, "disabled_budget_invalid"
    if policy is None:
        return False, "disabled_budget_required"
    return True, "enabled"


def _check_crypto() -> tuple[bool, str]:
    """Encrypt + decrypt a known plaintext via the configured KMS so
    a rotated-incorrectly FERNET_KEY surfaces here, not on the first
    secret read."""
    try:
        from app.safety.crypto import decrypt, encrypt

        probe = b"mnemos-health-probe"
        ct, iv = encrypt(probe.decode())
        out = decrypt(ct, iv)
        return out.encode() == probe, "ok"
    except Exception:  # noqa: BLE001
        return False, "crypto_unavailable"


def _check_analyzers() -> tuple[bool, str]:
    """Probe each language's analyzer binary on PATH. Returns the
    list of missing binaries — analyzers being partially installed
    is normal in Phase-1 (the operator may deliberately skip ones
    they don't have a language for) so this is advisory."""
    import shutil as _shutil

    from app.analyzers.registry import _BINARIES

    missing = sorted(
        binary for binary in _BINARIES.values()
        if _shutil.which(binary) is None
    )
    if not missing:
        return True, "all_present"
    return True, "missing: " + ",".join(missing)


async def _check_db() -> tuple[bool, str]:
    # Import at call time, not module load: serve_local / e2e test fixtures
    # rebind app.db.engine (importlib.reload), and a module-level reference
    # would keep pointing at a disposed engine — the pr138d full-suite flake.
    from app.db import SessionLocal

    try:
        async with SessionLocal() as db:
            await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
        return True, "ok"
    except Exception:  # noqa: BLE001
        return False, "database_unavailable"


async def _check_redis() -> tuple[bool, str]:
    try:
        redis = await get_redis()
        pong = await asyncio.wait_for(redis.ping(), timeout=2.0)
        return bool(pong), "ok" if pong else "no_pong"
    except Exception:  # noqa: BLE001
        return False, "redis_unavailable"


@router.get("/api/v1/health/metrics_summary")
async def metrics_summary() -> JSONResponse:
    """Dashboard-card-friendly counts pulled straight from Postgres.

    The Grafana panels use Prometheus, which lives in the workers'
    process memory — fine for a scrape backend, but if we exposed the
    same numbers via the platform's in-process registry the worker
    that ran the cron would see one count and every other worker
    would see zero. Team B 5th-round must-fix #8.

    Pulls a small fixed set of numbers a new operator wants to see at
    a glance:
      * project_dbs_disabled — bindings the daily probe sweep took
        offline (RW credentials slipped in, etc).
      * break_glass_active — grants that are still consumable.
      * runs_last_24h_failed — analyses that errored out yesterday.
      * webhook_events_24h — pushes the platform ingested.
    """
    from datetime import datetime, timedelta, timezone

    from app.db import SessionLocal  # call-time import (see _check_db note)

    out: dict[str, int | str] = {
        "project_dbs_disabled": 0,
        "break_glass_active": 0,
        "runs_last_24h_failed": 0,
        "webhook_events_24h": 0,
    }
    cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=24)
    try:
        async with SessionLocal() as db:
            out["project_dbs_disabled"] = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM project_dbs WHERE disabled_at IS NOT NULL"
                    )
                )
            ).scalar() or 0
            out["break_glass_active"] = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM diff_break_glass_grants "
                        "WHERE consumed_at IS NULL AND expires_at > now()"
                    )
                )
            ).scalar() or 0
            out["runs_last_24h_failed"] = (
                await db.execute(
                    text(
                        "SELECT COUNT(*) FROM analysis_runs "
                        "WHERE status = 'failed' AND created_at >= :cutoff"
                    ),
                    {"cutoff": cutoff},
                )
            ).scalar() or 0
            out["webhook_events_24h"] = (
                await db.execute(
                    text(
                        # table is audit_log (singular) and its timestamp
                        # column is occurred_at — see migration 0003.
                        "SELECT COUNT(*) FROM audit_log "
                        "WHERE action = 'webhook.received' AND occurred_at >= :cutoff"
                    ),
                    {"cutoff": cutoff},
                )
            ).scalar() or 0
    except Exception:  # noqa: BLE001
        return JSONResponse(
            status_code=503,
            content={"status": "error", "error": "metrics_unavailable"},
        )
    return JSONResponse({"status": "ok", **out})


async def _check_worker() -> tuple[bool, str]:
    # PR-135 — in docker-free local mode there is no separate ARQ
    # worker process: jobs run inline on the API event loop, so the
    # API *is* the worker. A heartbeat would never appear; report
    # healthy with a mode marker instead of a spurious 503.
    from app.local_mode import is_local_mode

    if is_local_mode():
        return True, "inline"
    try:
        redis = await get_redis()
        beat = await redis.get(_WORKER_HEARTBEAT_KEY)
    except Exception:  # noqa: BLE001
        return False, "worker_heartbeat_unavailable"
    if not beat:
        return False, "no_heartbeat"
    try:
        age = time.time() - float(beat)
    except (TypeError, ValueError):
        return False, "invalid_heartbeat"
    if age > _WORKER_STALE_AFTER_SEC:
        return False, f"stale_{int(age)}s"
    return True, f"fresh_{int(age)}s"
