"""Periodic background work — ARQ cron entries (spec §2.7).

Three jobs:

* :func:`run_break_glass_expiry` — every 5 minutes, mark stale grants
  consumed so a long-lived TTL grant cannot be reused much later by an
  approver who has since lost privileges.
* :func:`run_probe_recheck` — once a day, re-run the read-only probe
  on every ProjectDB binding and `disabled_at`-mark anything whose
  credentials have lost their read-only character.
* :func:`run_retention_purge` — once a day, vacuum old audit /
  webhook ingest noise per the configured retention window.

Single-leader concurrency uses a Postgres advisory lock
(``pg_try_advisory_lock(hashtext('mnemos:cron:<name>'))``). One worker
acquires it, the others return immediately. No extra dependency, no
env flag for operators to fat-finger. The lock is released on
connection close, so even a SIGKILLed worker frees it after Postgres
notices.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# Re-probe each ProjectDB at most once per this window. Anything fresher
# is already covered by PR-2's create-time probe; the cron just keeps
# the cache warm for long-lived bindings.
PROBE_RECHECK_INTERVAL = timedelta(hours=24)


async def _try_acquire(session: AsyncSession, name: str) -> bool:
    """Return True iff this connection acquired the named advisory lock.

    Connection-scoped (not transaction-scoped) so a long job doesn't
    hold the lock for its full runtime — the caller wraps each iteration
    in a fresh ``with_advisory_lock`` instead.
    """
    res = await session.execute(
        text("SELECT pg_try_advisory_lock(hashtext(:k))"),
        {"k": f"mnemos:cron:{name}"},
    )
    return bool(res.scalar())


async def _release(session: AsyncSession, name: str) -> None:
    await session.execute(
        text("SELECT pg_advisory_unlock(hashtext(:k))"),
        {"k": f"mnemos:cron:{name}"},
    )


async def with_advisory_lock(
    session_factory: Callable[[], AsyncSession],
    name: str,
    fn: Callable[[AsyncSession], Awaitable[Any]],
) -> Any:
    """Run ``fn`` iff we win the named advisory lock; otherwise no-op.

    Each cron entry calls this with the canonical job name so a multi-
    worker deployment still ends up with exactly one execution. The
    session passed to ``fn`` is the same one that holds the lock so
    queries can use it without acquiring a second connection.
    """
    async with session_factory() as session:
        if not await _try_acquire(session, name):
            log.debug("cron %s: another worker holds the lock", name)
            return None
        try:
            return await fn(session)
        finally:
            await _release(session, name)


# ---------------------------------------------------------------------------
# Cron entries
# ---------------------------------------------------------------------------


async def _expire_break_glass(session: AsyncSession) -> dict[str, int]:
    """Mark every grant whose ``expires_at`` is in the past as consumed.

    Cosmetic — the approve endpoint's atomic UPDATE also refuses
    expired grants. The sweep just keeps the dashboard "active grants"
    list honest and gives the audit log a clear expiry record.
    """
    res = await session.execute(
        text(
            """
            UPDATE diff_break_glass_grants
               SET consumed_at = now(), consumed_by = 'system:expiry'
             WHERE consumed_at IS NULL
               AND expires_at <= now()
             RETURNING id
            """
        )
    )
    expired_ids = [row[0] for row in res.fetchall()]
    await session.commit()
    if expired_ids:
        log.info("break_glass_expiry: marked %d grants consumed", len(expired_ids))
    return {"expired": len(expired_ids)}


async def _probe_recheck_one(session: AsyncSession) -> dict[str, int]:
    """Re-probe ProjectDBs whose probe is older than the recheck window."""
    from app.api.project_dbs import _resolve_conn_ref
    from app.data_sampler.probe import probe_via_analyzer
    from app.models.projects import ProjectDB

    cutoff = datetime.now(tz=timezone.utc) - PROBE_RECHECK_INTERVAL
    rows = (
        await session.execute(
            select(ProjectDB).where(
                ProjectDB.disabled_at.is_(None),
                (ProjectDB.last_probe_at.is_(None)) | (ProjectDB.last_probe_at < cutoff),
            )
        )
    ).scalars().all()

    rechecked = 0
    disabled = 0
    for row in rows:
        # Skip while an analysis is running against this DB — we'd
        # otherwise risk yanking the binding out from under it. The
        # orchestrator stages table records the live stage set.
        running = await session.execute(
            text(
                "SELECT 1 FROM analysis_stages "
                "WHERE project_id = :pid AND status = 'running' LIMIT 1"
            ),
            {"pid": row.project_id},
        )
        if running.first() is not None:
            continue

        conn = await _resolve_conn_ref(session, row.secret_id)
        if conn is None:
            continue
        result = await probe_via_analyzer(row.kind, conn)
        row.last_probe_at = datetime.now(tz=timezone.utc)
        row.last_probe_result = result.as_jsonable()
        if not result.is_acceptable() and row.disabled_at is None:
            row.disabled_at = row.last_probe_at
            disabled += 1
        rechecked += 1
    await session.commit()
    log.info("probe_recheck: rechecked=%d disabled=%d", rechecked, disabled)
    return {"rechecked": rechecked, "disabled": disabled}


async def _retention_purge(session: AsyncSession) -> dict[str, int]:
    """Trim audit / webhook receipts older than the retention window.

    We never delete a finding or a graph node here; only the noisy
    bookkeeping tables that grow unboundedly. The window is fixed at
    180 days for now (spec §14.4 mentions "configurable per-tenant"
    but Phase 1 ships the single-tenant default).
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    res = await session.execute(
        text(
            "DELETE FROM audit_logs WHERE created_at < :cutoff "
            "AND action IN ('webhook.received',) RETURNING id"
        ),
        {"cutoff": cutoff},
    )
    deleted = len(res.fetchall())
    await session.commit()
    log.info("retention_purge: deleted=%d", deleted)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# ARQ entry points
# ---------------------------------------------------------------------------


async def run_break_glass_expiry(ctx: dict) -> dict[str, int] | None:
    return await with_advisory_lock(
        SessionLocal_factory(), "break_glass_expiry", _expire_break_glass
    )


async def run_probe_recheck(ctx: dict) -> dict[str, int] | None:
    return await with_advisory_lock(
        SessionLocal_factory(), "probe_recheck", _probe_recheck_one
    )


async def run_retention_purge(ctx: dict) -> dict[str, int] | None:
    return await with_advisory_lock(
        SessionLocal_factory(), "retention_purge", _retention_purge
    )


def SessionLocal_factory():
    """Return the canonical async-session factory.

    Indirected so tests can monkey-patch a fixture-bound session here
    without having to teach every cron entry about test plumbing.
    """
    from app.db import SessionLocal

    return SessionLocal


__all__ = [
    "with_advisory_lock",
    "run_break_glass_expiry",
    "run_probe_recheck",
    "run_retention_purge",
    "PROBE_RECHECK_INTERVAL",
]
