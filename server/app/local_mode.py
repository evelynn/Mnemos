"""PR-135 — Docker-free local mode.

Mnemos's production topology is docker-compose: Postgres + Redis +
the FastAPI app + an ARQ worker + five analyzer images. That's the
right shape for a real deployment, but it's a heavy ask for an
operator who just wants to *try* the platform, run the test-drive,
or develop a feature on a laptop with no Docker daemon.

Local mode collapses the whole stack into a single Python process
with **zero external services**:

- **Database** → SQLite (``sqlite+aiosqlite://``) via the PR-118
  polyglot type layer that makes the production PG-typed models
  (JSONB / UUID / ARRAY / BIGINT) run unchanged on SQLite.
- **Redis** → ``fakeredis`` — an in-process, single-keyspace Redis
  emulator. Sessions, rate-limit, brute-force lockout, the progress
  bus, and health pings all talk to it exactly as they talk to real
  Redis; no socket, no daemon.
- **ARQ job queue** → an inline shim. ``enqueue_job`` schedules the
  job as an asyncio background task on the API's own event loop, so
  the HTTP request returns immediately (same "fire and forget"
  contract) and the analysis streams progress over SSE just like the
  worker-backed path. No separate worker process.
- **Analyzers** → already run as local subprocesses (node / python /
  dotnet) — proven in PR-114/115/127. No analyzer images needed.

Activation: set ``MNEMOS_LOCAL_MODE=1`` (the ``serve-local`` CLI
command does this for you), or point ``DATABASE_URL`` at a SQLite
URL. Production (Postgres + real Redis) is completely unaffected —
every branch here is gated on :func:`is_local_mode`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import OrderedDict
from datetime import timedelta
from typing import Any

log = logging.getLogger(__name__)


def is_local_mode() -> bool:
    """True when the platform should run without external services.

    Two triggers:
    1. ``MNEMOS_LOCAL_MODE=1`` — explicit (set by ``serve-local``).
    2. A SQLite ``DATABASE_URL`` — implicit; nobody points a real
       deployment at SQLite, so this is an unambiguous local signal.
    """
    if os.environ.get("MNEMOS_LOCAL_MODE") == "1":
        return True
    try:
        from app.config import get_settings

        return get_settings().database_url.startswith("sqlite")
    except Exception:  # noqa: BLE001 — never let detection crash a caller
        return False


# ─── in-process fake Redis (shared keyspace) ──────────────────────

_fake_server = None


def get_fake_redis():
    """Return a ``fakeredis`` async client bound to one shared server.

    Every caller (sessions, rate-limit, progress, …) gets a *distinct*
    client object but they all read/write the *same* in-memory
    keyspace — so a session written by the auth path is visible to the
    health check, exactly like a single real Redis. ``decode_responses``
    matches how ``redis_pool``/``sessions`` build the real client.
    """
    global _fake_server
    import fakeredis
    import fakeredis.aioredis as fakeredis_aio

    if _fake_server is None:
        _fake_server = fakeredis.FakeServer()
    return fakeredis_aio.FakeRedis(server=_fake_server, decode_responses=True)


# ─── inline ARQ queue shim ────────────────────────────────────────


class _InlineJob:
    """Minimal stand-in for arq's Job return value. Callers only ever
    ignore it today, but returning a job-id-carrying object keeps the
    contract honest if that changes."""

    def __init__(self, job_id: str | None):
        self.job_id = job_id


class InlineQueue:
    """ARQ-compatible ``enqueue_job`` that runs jobs in-process.

    Instead of pushing onto a Redis list for a separate worker, the
    job coroutine is scheduled as a background task on the current
    event loop. The HTTP handler that called ``enqueue_job`` returns
    immediately; the job runs concurrently and streams progress over
    the same ProgressBus the SSE endpoint reads — so the live
    analysis view works identically to the worker-backed deployment.

    ``_job_id`` is honoured for idempotency (a duplicate id within a bounded
    retention window is dropped), matching ARQ's dedup contract that
    webhooks.py relies on for push de-duplication without an unbounded set.
    """

    # job-name → coroutine function. Kept as a method so the import of
    # jobs.py (which pulls in arq.cron) is deferred until first use.
    def _registry(self) -> dict[str, Any]:
        from app.orchestrator import jobs

        return {"run_ingest": jobs.run_ingest}

    def __init__(
        self,
        *,
        seen_ttl_sec: float = 60 * 60,
        max_seen_job_ids: int = 4096,
    ) -> None:
        # ARQ keeps a completed job id only for a bounded retention window.
        # The old local shim kept every id forever, so a long-lived local
        # process leaked memory and could reject a legitimate re-enqueue
        # indefinitely.  Expiry timestamps are insertion ordered so pruning
        # stays O(number-expired), with a hard size ceiling as a backstop.
        self._seen_ttl_sec = max(0.0, float(seen_ttl_sec))
        self._max_seen_job_ids = max(1, int(max_seen_job_ids))
        self._seen_job_ids: OrderedDict[str, float] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._tasks_by_job_id: dict[str, asyncio.Task] = {}
        # Serialize job execution: SQLite (local mode) allows a single
        # writer, so two concurrent ``run_ingest`` tasks collide on
        # "database is locked" and one run fails (PR-183 S1). One lock makes
        # a second large analysis queue behind the first instead of failing.
        self._run_lock = asyncio.Lock()

    def _prune_seen_job_ids(self, now: float) -> None:
        while self._seen_job_ids:
            _, expires_at = next(iter(self._seen_job_ids.items()))
            if expires_at > now:
                break
            self._seen_job_ids.popitem(last=False)
        while len(self._seen_job_ids) >= self._max_seen_job_ids:
            self._seen_job_ids.popitem(last=False)

    @staticmethod
    def _defer_seconds(value: Any) -> float:
        if value is None:
            return 0.0
        if isinstance(value, timedelta):
            return max(0.0, value.total_seconds())
        if isinstance(value, (int, float)):
            return max(0.0, float(value))
        log.warning("inline_queue: ignoring unsupported _defer_by=%r", value)
        return 0.0

    async def enqueue_job(
        self,
        function_name: str,
        *args: Any,
        _job_id: str | None = None,
        _defer_by: Any = None,
        _queue_name: str | None = None,
        **kwargs: Any,
    ) -> _InlineJob | None:
        fn = self._registry().get(function_name)
        if fn is None:
            log.warning("inline_queue: unknown job %r — dropped", function_name)
            return None

        now = time.monotonic()
        self._prune_seen_job_ids(now)
        if _job_id is not None and (
            _job_id in self._seen_job_ids
            or _job_id in self._tasks_by_job_id
        ):
            # Match ARQ: duplicate ids return None, which lets API callers
            # avoid creating a queued row with no backing task.
            log.info("inline_queue: dropping duplicate job_id=%s", _job_id)
            return None
        if _job_id is not None:
            self._seen_job_ids[_job_id] = now + self._seen_ttl_sec

        delay = self._defer_seconds(_defer_by)
        task = asyncio.create_task(self._run_deferred(fn, args, delay))
        # Hold a reference so the task isn't garbage-collected mid-flight
        # (asyncio only keeps weak refs to scheduled tasks).
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if _job_id is not None:
            self._tasks_by_job_id[_job_id] = task

            def _forget(_task: asyncio.Task, job_id: str = _job_id) -> None:
                self._tasks_by_job_id.pop(job_id, None)

            task.add_done_callback(_forget)
        return _InlineJob(_job_id)

    async def _run_deferred(self, fn, args: tuple, delay: float) -> None:
        # asyncio.sleep is deliberately inside the tracked task: abort_job()
        # can cancel a deferred webhook before it starts consuming the local
        # SQLite writer lock.
        if delay > 0:
            await asyncio.sleep(delay)
        await self._run(fn, args)

    async def abort_job(self, job_id: str) -> bool:
        """Cancel a queued/running local job by its ARQ-compatible id."""

        task = self._tasks_by_job_id.get(job_id)
        if task is None or task.done():
            return False
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=3.0)
        except asyncio.CancelledError:
            return True
        except TimeoutError:
            # The cooperative run-status check remains authoritative.  The
            # hardened analyzer runner also escalates child termination.
            return False
        return task.cancelled()

    async def _run(self, fn, args: tuple) -> None:
        """Build the minimal ARQ ctx the job functions expect, then
        run. Errors are logged, never propagated — a background job
        crashing must not take down the event loop."""
        from app.orchestrator.progress import ProgressBus

        ctx: dict[str, Any] = {"progress": ProgressBus()}
        try:
            # One writer at a time — SQLite serialises analyses (PR-183 S1).
            async with self._run_lock:
                await fn(ctx, *args)
        except Exception:  # noqa: BLE001
            log.exception("inline_queue: job %s failed", getattr(fn, "__name__", fn))

    async def aclose(self) -> None:
        """Best-effort drain on shutdown — let in-flight jobs finish
        briefly so a serve-local Ctrl-C doesn't orphan a half-written
        analysis. Bounded so shutdown stays snappy."""
        if not self._tasks:
            return
        try:
            await asyncio.wait(self._tasks, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


_inline_queue: InlineQueue | None = None


def get_inline_queue() -> InlineQueue:
    global _inline_queue
    if _inline_queue is None:
        _inline_queue = InlineQueue()
    return _inline_queue


# ─── schema bootstrap ─────────────────────────────────────────────


async def ensure_sqlite_schema() -> None:
    """Create every table on the SQLite engine. Production uses
    Alembic; local mode skips migrations entirely and stamps the
    full current schema in one shot — there's no upgrade path to
    preserve on a throwaway local DB.

    Imports every model module first so ``Base.metadata`` knows the
    full table set (and cross-table FKs resolve) before create_all.
    """
    # Polyglot MUST be installed before any DDL compiles.
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()

    # Pull every model into the metadata registry. This list MUST cover
    # every module under app/models that declares a __tablename__ — a
    # missing module means create_all silently skips its table and the
    # feature 500s at runtime ("no such table"). Keep it complete.
    from app.models import audit as _audit  # noqa: F401
    from app.models import auth as _auth  # noqa: F401
    from app.models import comments as _comments  # noqa: F401
    from app.models import findings as _findings  # noqa: F401
    from app.models import graph as _graph  # noqa: F401
    from app.models import llm as _llm  # noqa: F401
    from app.models import onboarding as _onboarding  # noqa: F401
    from app.models import organization as _org  # noqa: F401
    from app.models import overlays as _overlays  # noqa: F401
    from app.models import plans as _plans  # noqa: F401
    from app.models import projects as _projects  # noqa: F401
    from app.models import runtime as _runtime  # noqa: F401
    from app.models import samples as _samples  # noqa: F401
    from app.models import stages as _stages  # noqa: F401
    from app.models.base import Base

    from app.db import engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("local_mode: SQLite schema created")
