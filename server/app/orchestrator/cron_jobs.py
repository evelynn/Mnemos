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
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import (
    GraphPublicationInvariantError,
    validate_graph_publication_receipt,
)
from app.models.graph import AnalysisRun

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
    from fastapi import HTTPException

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

        try:
            conn = await _resolve_conn_ref(session, row.project_id, row.secret_id)
        except HTTPException as exc:
            # Missing and cross-org Secret UUIDs are both fail-closed by the
            # shared resolver. Keep one contaminated row from aborting the
            # fleet-wide cron pass, but never hand its credential to a probe.
            if exc.status_code != 404:
                raise
            log.warning(
                "probe_recheck: ProjectDB %s has unavailable tenant-scoped secret",
                row.id,
            )
            continue
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

    * ``audit_logs`` — 180-day window; only the noisy webhook ingest
      entries (``webhook.received`` / ``webhook.skipped``) are deleted.
      Auth / approval / break-glass entries are kept indefinitely
      (spec §14.4 audit retention).
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
            "AND action IN ('webhook.received', 'webhook.skipped') "
            "RETURNING id"
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
# walks many stages and the worker hard timeout is 8h.  The stale threshold
# must be strictly longer than that timeout or an idle worker's cron task can
# mark a legitimate run failed while it is still mutating the graph.  The
# conservative 24h default matches .env.example; operators can lower it only
# together with the worker timeout.
def _stale_run_after_sec() -> int:
    import os
    raw = os.environ.get("MNEMOS_STALE_RUN_AFTER_SEC", "")
    try:
        n = int(raw)
        if n > 0:
            return n
    except ValueError:
        pass
    return 24 * 60 * 60


_STALE_RUN_AFTER_SEC = _stale_run_after_sec()
# ARQ's enqueue contract expires an unstarted job after 24 hours by default
# (plus its short defer).  Once that payload is gone, the database row can
# never leave ``queued`` on its own.  A one-hour margin avoids racing the
# Redis expiry boundary while still guaranteeing eventual terminal state.
_STALE_QUEUED_AFTER_SEC = 25 * 60 * 60


async def _find_expired_queued_runs(
    session: AsyncSession, redis: Any
) -> set[uuid.UUID]:
    """Return old queued rows whose ARQ job can no longer execute.

    Age alone is not enough: a deliberately serialized large-repository queue
    may legitimately wait more than a day.  Only terminal/missing ARQ jobs (or
    rows with no persisted job id) are safe to fail. Redis lookup errors retain
    the row so an observability outage cannot discard a source revision.
    """

    from arq.jobs import Job, JobStatus

    queued_cutoff = datetime.now(tz=timezone.utc) - timedelta(
        seconds=_STALE_QUEUED_AFTER_SEC
    )
    rows = (
        await session.execute(
            select(AnalysisRun.id, AnalysisRun.stats)
            .where(
                AnalysisRun.status == "queued",
                AnalysisRun.created_at < queued_cutoff,
            )
            .order_by(AnalysisRun.created_at)
            .limit(500)
        )
    ).all()
    expired: set[uuid.UUID] = set()
    for run_id, stats in rows:
        job_id = stats.get("job_id") if isinstance(stats, dict) else None
        if not isinstance(job_id, str) or not job_id:
            expired.add(run_id)
            continue
        try:
            job_status = await Job(job_id, redis).status()
        except Exception:  # noqa: BLE001 — retain source revision on Redis faults
            log.exception("queued analysis job status unavailable run_id=%s", run_id)
            continue
        if job_status in {JobStatus.complete, JobStatus.not_found}:
            expired.add(run_id)
    return expired


async def _reset_stale_runs(
    session: AsyncSession,
    expired_queued_ids: set[uuid.UUID] | None = None,
) -> dict[str, int]:
    """Terminalize abandoned running rows and expired queued rows.

    Spec §2.7's "always-on" promise needs this — without the sweep,
    a SIGKILLed worker leaves the GUI showing "running" forever. Likewise,
    ARQ removes an unstarted job payload after 24 hours while its database
    row otherwise remains "queued" forever. Both cases need an auditable
    terminal transition so the operator can retry without admin SQL access.

    The sweep is harmless when nothing is wrong: rows that finished
    cleanly have ``status='completed'`` or ``'failed'`` already and
    are excluded by the WHERE clause. ``published`` is also excluded here:
    its source graph is durable and may only become ``partial``, never failed.
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
            "run exceeded configured stale-run budget' "
            " WHERE status = 'running' AND started_at < :cutoff "
            " RETURNING id"
        ),
        {"cutoff": cutoff},
    )
    reset_ids = {row[0] for row in res.fetchall()}
    if expired_queued_ids:
        queued_res = await session.execute(
            update(AnalysisRun)
            .where(
                AnalysisRun.id.in_(expired_queued_ids),
                AnalysisRun.status == "queued",
            )
            .values(
                status="failed",
                completed_at=datetime.now(tz=timezone.utc),
                error_log=(
                    func.coalesce(AnalysisRun.error_log, "")
                    + "\n[reset_stale_runs] queued job expired before worker pickup"
                ),
            )
            .returning(AnalysisRun.id)
        )
        reset_ids.update(row[0] for row in queued_res.fetchall())
    await session.commit()
    if reset_ids:
        log.warning(
            "reset_stale_runs: %d analysis runs flipped to failed",
            len(reset_ids),
        )
    return {"reset": len(reset_ids)}


async def _partialize_stale_published_runs(
    session: AsyncSession,
) -> dict[str, int]:
    """Close orphan post-processing while preserving its published graph."""

    cutoff = datetime.now(tz=timezone.utc) - timedelta(
        seconds=_STALE_RUN_AFTER_SEC
    )
    rows = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.status == "published",
                AnalysisRun.started_at < cutoff,
            )
            .order_by(AnalysisRun.started_at)
            .limit(500)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    completed_at = datetime.now(tz=timezone.utc)
    message = (
        "[reset_stale_runs] source graph was published, but postprocess "
        "heartbeat was lost"
    )
    partialized = 0
    for run in rows:
        stats = dict(run.stats) if isinstance(run.stats, dict) else {}
        try:
            publication = validate_graph_publication_receipt(run)
        except GraphPublicationInvariantError as exc:
            log.error(
                "stale published run has invalid receipt run_id=%s: %s",
                run.id,
                exc,
            )
            continue
        receipt = publication.to_payload()
        error = {
            "stage": "stale_recovery",
            "type": "WorkerHeartbeatLost",
            "message": message,
            "cancelled": False,
            "at": completed_at.isoformat(),
        }
        prior_postprocess = stats.get("postprocess")
        postprocess = (
            dict(prior_postprocess) if isinstance(prior_postprocess, dict) else {}
        )
        postprocess.update(
            {
                "status": "partial",
                "completed_at": completed_at.isoformat(),
                "errors": [error],
                "findings_freshness": {
                    "status": "unknown",
                    "reason": "stale_published_postprocess",
                },
            }
        )
        run.status = "partial"
        run.completed_at = completed_at
        run.error_log = message
        run.stats = {
            **stats,
            "graph_publication": receipt,
            "postprocess": postprocess,
            "postprocess_error": error,
        }
        partialized += 1
    await session.commit()
    if partialized:
        log.warning(
            "reset_stale_runs: %d published runs closed as partial",
            partialized,
        )
    return {"partial": partialized}


async def run_reset_stale_runs(ctx: dict) -> dict[str, int] | None:
    async def _reset_with_queue_check(session: AsyncSession) -> dict[str, int]:
        redis = ctx.get("redis")
        expired = (
            await _find_expired_queued_runs(session, redis)
            if redis is not None
            else set()
        )
        reset = await _reset_stale_runs(session, expired)
        partial = await _partialize_stale_published_runs(session)
        return {**reset, **partial}

    return await with_advisory_lock(
        SessionLocal_factory(), "reset_stale_runs", _reset_with_queue_check
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
