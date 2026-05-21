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

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


# Re-probe each ProjectDB at most once per this window. Anything fresher
# is already covered by PR-2's create-time probe; the cron just keeps
# the cache warm for long-lived bindings.
PROBE_RECHECK_INTERVAL = timedelta(hours=24)

# A binding whose probe is older than this AND which keeps getting
# skipped (a project that is continuously analysing) is overdue: the
# recheck can't safely run mid-analysis, but silently skipping forever
# would let credentials drift unverified (§2.5). Past the ceiling we
# log a warning and count it so an operator can act.
PROBE_OVERDUE_CEILING = PROBE_RECHECK_INTERVAL * 7


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
            # Lost the race for the leader role. Expected and even
            # desirable under multi-worker deploys — surface it on the
            # Grafana panel so an operator can confirm leader election
            # is actually happening.
            from app.obs.metrics import cron_lock_lost_total

            cron_lock_lost_total.labels(job=name).inc()
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
        from app.obs.metrics import break_glass_grants_total

        break_glass_grants_total.labels(action="expired").inc(len(expired_ids))
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
    overdue = 0
    overdue_cutoff = datetime.now(tz=timezone.utc) - PROBE_OVERDUE_CEILING
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
            # A continuously-busy project can be skipped indefinitely;
            # once its probe is past the ceiling, surface it so the
            # silent skip doesn't hide drifting credentials.
            if row.last_probe_at is None or row.last_probe_at < overdue_cutoff:
                overdue += 1
                log.warning(
                    "probe_recheck: ProjectDB %s overdue (last_probe_at=%s) "
                    "but skipped — project continuously analysing",
                    row.id,
                    row.last_probe_at,
                )
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
    # Update the disabled gauge to the current fleet-wide total, not just
    # the delta — that's what the Grafana panel wants to show. We do this
    # in the same session so the value is consistent with the writes above.
    from app.models.projects import ProjectDB as _PDB
    from app.obs.metrics import project_db_disabled
    from sqlalchemy import func as _func

    total_disabled = (
        await session.execute(
            select(_func.count()).select_from(_PDB).where(_PDB.disabled_at.isnot(None))
        )
    ).scalar() or 0
    project_db_disabled.set(int(total_disabled))

    await session.commit()
    log.info(
        "probe_recheck: rechecked=%d disabled=%d overdue=%d",
        rechecked,
        disabled,
        overdue,
    )
    return {"rechecked": rechecked, "disabled": disabled, "overdue": overdue}


async def _retention_purge(session: AsyncSession) -> dict[str, int]:
    """Trim bookkeeping tables that grow unboundedly.

    Two sweeps:

    * ``audit_logs`` — 180-day window; only the noisy webhook-received
      entries are deleted. Auth / approval / break-glass entries are
      kept indefinitely (spec §14.4 audit retention).
    * ``runtime_observations`` — 14-day window. The OTLP receiver
      writes one row per ``(service, operation, kind)`` triple it
      sees; without this sweep the table grows unbounded on any
      busy deployment. Phase 2 backlog P2-2 promised this retention
      and the 9th-round audit caught it as missing in PR-25.

    Both windows are fixed for Phase 1; spec §14.4 mentions
    "configurable per-tenant" but ships the single-tenant defaults.
    We never delete a finding or a graph node here.
    """
    audit_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=180)
    res = await session.execute(
        text(
            "DELETE FROM audit_logs WHERE created_at < :cutoff "
            "AND action IN ('webhook.received',) RETURNING id"
        ),
        {"cutoff": audit_cutoff},
    )
    deleted_audit = len(res.fetchall())

    runtime_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=14)
    res = await session.execute(
        text(
            "DELETE FROM runtime_observations "
            "WHERE last_seen_at < :cutoff RETURNING id"
        ),
        {"cutoff": runtime_cutoff},
    )
    deleted_runtime = len(res.fetchall())

    await session.commit()
    log.info(
        "retention_purge: deleted audit=%d runtime=%d",
        deleted_audit,
        deleted_runtime,
    )
    return {"deleted": deleted_audit, "runtime_deleted": deleted_runtime}


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


# ---------------------------------------------------------------------------
# Stale analysis_runs sweep (PR-33 — closes the "wedged run" gap the
# 11th-round e2e audit flagged on its readiness doc).
# ---------------------------------------------------------------------------

# A run is considered abandoned when its ``started_at`` is older than
# the longest stage budget plus a margin. The orchestrator's per-stage
# ``time_budget_sec`` defaults to 1800s (30 min); the maximum pipeline
# walks ~12 stages, so 6h is the soft ceiling beyond which a running
# row almost certainly means a worker crash.
_STALE_RUN_AFTER_SEC = 6 * 60 * 60


async def _reset_stale_runs(session: AsyncSession) -> dict[str, int]:
    """Flip ``status='running'`` rows whose worker is long gone to
    ``status='failed'`` with an explanatory ``error_log``.

    Spec §2.7's "always-on" promise needs this — without the sweep,
    a SIGKILLed worker leaves the GUI showing "running" forever and
    the operator has no way to re-trigger without admin SQL access.

    The sweep is harmless when nothing is wrong: rows that finished
    cleanly have ``status='completed'`` or ``'failed'`` already and
    are excluded by the WHERE clause.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(
        seconds=_STALE_RUN_AFTER_SEC
    )
    res = await session.execute(
        text(
            "UPDATE analysis_runs SET "
            "  status = 'failed', "
            "  completed_at = now(), "
            "  error_log = coalesce(error_log, '') "
            "    || E'\\n[reset_stale_runs] worker heartbeat lost; "
            "run exceeded 6h budget' "
            " WHERE status = 'running' AND started_at < :cutoff "
            " RETURNING id"
        ),
        {"cutoff": cutoff},
    )
    reset_ids = [row[0] for row in res.fetchall()]
    await session.commit()
    if reset_ids:
        log.warning(
            "reset_stale_runs: %d analysis runs flipped to failed",
            len(reset_ids),
        )
    return {"reset": len(reset_ids)}


async def run_reset_stale_runs(ctx: dict) -> dict[str, int] | None:
    return await with_advisory_lock(
        SessionLocal_factory(), "reset_stale_runs", _reset_stale_runs
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
    "run_reset_stale_runs",
    "PROBE_RECHECK_INTERVAL",
]
