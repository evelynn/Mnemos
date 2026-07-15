"""ARQ job functions — staged execution with per-stage progress tracking.

Every stage is wrapped in :class:`app.orchestrator.stages.StageTracker` so
the GUI can show a pipeline view and each summariser pass can exit in
``status=partial`` when its budget is spent without losing progress.
Design rationale: ``docs/analysis-strategy.md``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import aclosing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.registry import (
    DB_ANALYZER_KINDS,
    SOURCE_ANALYZER_LANGUAGES,
    analyzer_available,
    binary_for,
    runner_for,
)
from app.audit.logger import record as audit_record
from app.config import get_settings
from app.db import SessionLocal
from app.extractor.agent import Extractor
from app.extractor.cost import (
    BudgetExceeded,
    LLMRunBudget,
    RunBudgetExceeded,
    require_budget,
)
from app.extractor.packing import _approx_tokens
from app.extractor.runner import summarise_l1, summarise_l2, summarise_l3
from app.graph_publication import (
    GRAPH_HEAD_NEEDS_REBUILD,
    GRAPH_HEAD_READY,
    GraphPublicationInvariantError,
    LegacyGraphWriteRejected,
    bootstrap_graph_head,
    capture_graph_base_generation,
    cleanup_staged_graph,
    promote_staged_graph,
    seal_graph_coverage,
    stage_edge,
    stage_node,
    validate_graph_publication_receipt,
)
from app.merge.contract_id import http_contract_id
from app.merge.findings import run_all as rebuild_findings
from app.merge.runtime import reconcile_observations
from app.models.findings import LLMCall, Summary
from app.models.graph import (
    ANALYSIS_RUN_TERMINAL_STATUSES,
    AnalysisRun,
    Edge,
    GraphHead,
    GraphNodeStage,
    Node,
)
from app.models.projects import Project
from app.orchestrator.progress import ProgressBus
from app.orchestrator.seen_index import SeenFactIndex, configured_memory_limit
from app.orchestrator.source_checkout import PreparedSource, prepare_source
from app.orchestrator.source_manifest import (
    SourceManifest,
    build_source_manifest,
    changed_analyzers,
    deterministic_source_names,
)
from app.orchestrator.stages import StageBudgetExceeded, StageTracker
from app.source_snapshot import (
    SOURCE_SNAPSHOT_CONTRACT,
)

log = logging.getLogger(__name__)
_settings = get_settings()
_ANALYZER_STAGE_TIMEOUT_SEC = 30 * 60.0
_AGENT_EXTRACT_MODEL = "claude-sonnet-4-6"
_AGENT_EXTRACT_FAILED = "agent_extract_failed"
_AGENT_EXTRACT_TIMEOUT = "agent_extract_timeout"
_AGENT_EXTRACT_CANCELLED = "agent_extract_cancelled"
_SUMMARY_RETENTION_BATCH = 900
_AGENT_RUN_DEADLINE = "run_deadline_exceeded"
_MAX_POSTPROCESS_ERROR_CHARS = 2048
_MAX_POSTPROCESS_ERRORS = 4


def _reserve_agent_provider_call(
    run_budget: LLMRunBudget,
    *,
    language: str,
    file_rel: str,
    code: str,
) -> float:
    """Reserve one complete-file agent call from the shared AI budget."""

    return run_budget.reserve(
        _approx_tokens(
            {
                "language": language,
                "file": file_rel,
                "code": code,
            }
        )
        + 512
    )


async def _record_agent_llm_call(
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    language: str,
    file_rel: str,
    status: str,
    fallback_reason: str | None,
) -> None:
    """Durably ledger one physical Agent SDK invocation.

    Agent extraction has no trustworthy token-usage signal, so usage remains
    ``NULL`` instead of pretending the call was free.  The ledger commits
    independently of inferred graph ingest because a rejected provider reply
    still consumed a real call.
    """

    async with SessionLocal() as session:
        session.add(
            LLMCall(
                project_id=project_id,
                analysis_run_id=run_id,
                target_id=f"agent:{language}:{file_rel}",
                level=0,
                model_used=_AGENT_EXTRACT_MODEL,
                tokens_used=None,
                status=status,
                fallback_reason=fallback_reason,
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.commit()


async def _set_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    stats: dict[str, Any] | None = None,
    error_log: str | None = None,
) -> bool:
    if status == "published" and completed_at is not None:
        raise ValueError("published AnalysisRun cannot have completed_at")
    if status in ANALYSIS_RUN_TERMINAL_STATUSES and completed_at is None:
        raise ValueError(f"terminal AnalysisRun status {status} requires completed_at")
    values: dict[str, Any] = {"status": status}
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if stats is not None:
        values["stats"] = stats
    if error_log is not None:
        values["error_log"] = error_log
    allowed_from = {
        "running": {"queued"},
        "published": {"running"},
        # Continuation runs have no source promotion and may still close
        # directly from running. Source-indexing runs close from published.
        "completed": {"running", "published"},
        "partial": {"published"},
        "failed": {"queued", "running"},
        "cancelled": {"queued", "running"},
    }.get(status)
    stmt = update(AnalysisRun).where(AnalysisRun.id == run_id)
    if allowed_from is not None:
        stmt = stmt.where(AnalysisRun.status.in_(allowed_from))
    result = await session.execute(stmt.values(**values))
    await session.commit()
    return bool(result.rowcount)


class AnalysisCancelled(RuntimeError):
    """Cooperative stop requested through the analysis-run API."""


_PROJECT_LOCK_TTL_SEC = 9 * 60 * 60
_PROJECT_LOCK_RETRY_DELAY_SEC = 30
# A crashed owner loses the Redis lease after ``_PROJECT_LOCK_TTL_SEC``.  Keep
# a contending run queued until then instead of dropping the source revision;
# the small margin covers scheduling jitter around lease expiry.
_PROJECT_LOCK_MAX_RETRIES = (
    _PROJECT_LOCK_TTL_SEC // _PROJECT_LOCK_RETRY_DELAY_SEC
) + 2


async def _acquire_project_lock(project_id: uuid.UUID, run_id: uuid.UUID) -> bool:
    """Fail-closed distributed mutation lock for one current project graph."""

    from app.orchestrator.redis_pool import get_redis

    redis = await get_redis()
    acquired = await redis.set(
        f"mnemos:analysis-lock:{project_id}",
        str(run_id),
        nx=True,
        ex=_PROJECT_LOCK_TTL_SEC,
    )
    return bool(acquired)


async def _release_project_lock(project_id: uuid.UUID, run_id: uuid.UUID) -> None:
    from app.orchestrator.redis_pool import get_redis
    from redis.exceptions import WatchError

    redis = await get_redis()
    key = f"mnemos:analysis-lock:{project_id}"
    # Atomic compare-and-delete: a late cleanup must not remove a successor's
    # lease after this run's TTL expired. WATCH/MULTI works in both real Redis
    # and the local-mode fakeredis build, while EVAL is optional in fakeredis.
    for _ in range(3):
        async with redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                if await pipe.get(key) != str(run_id):
                    await pipe.unwatch()
                    return
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return
            except WatchError:
                continue
    log.warning("analysis project lock release raced repeatedly key=%s", key)


async def _reclaim_terminal_project_lock(project_id: uuid.UUID) -> bool:
    """Remove a lease whose owning run has definitively stopped mutating.

    ``completed`` and ``failed`` are written by the worker only after its graph
    work has ended (or by the >job-timeout stale reaper).  ``cancelled`` is
    deliberately excluded: the API commits that state before the cooperative
    abort has necessarily reached an analyzer.  Compare-and-delete in
    :func:`_release_project_lock` prevents deleting a successor lease if the
    key changes between inspection and cleanup.
    """

    from app.orchestrator.redis_pool import get_redis

    redis = await get_redis()
    owner_value = await redis.get(f"mnemos:analysis-lock:{project_id}")
    if not owner_value:
        return False
    try:
        owner_run_id = uuid.UUID(str(owner_value))
    except (TypeError, ValueError):
        return False
    async with SessionLocal() as session:
        owner_status = (
            await session.execute(
                select(AnalysisRun.status).where(
                    AnalysisRun.id == owner_run_id,
                    AnalysisRun.project_id == project_id,
                )
            )
        ).scalar_one_or_none()
    if owner_status not in {"completed", "partial", "failed"}:
        return False
    await _release_project_lock(project_id, owner_run_id)
    return True


async def _recover_prelock_failure(
    bus: ProgressBus,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    stage: str,
    exc: BaseException,
    cancelled: bool = False,
) -> str | None:
    """Close a queued/published run consistently before lock ownership.

    A published resume already owns an immutable source receipt and therefore
    cannot transition to ``failed``. Any pre-lock terminal condition closes
    only its derived phase as ``partial`` and preserves GraphHead. A queued
    run has published nothing and may truthfully become ``failed``. Every
    event is projected from the transition that actually committed.
    """

    status = await _run_status(run_id)
    if status == "published":
        return await _partialize_published_run(
            bus,
            project_id=project_id,
            run_id=run_id,
            stage=stage,
            exc=exc,
            cancelled=cancelled,
        )
    if status == "queued":
        async with SessionLocal() as session:
            failed = await _set_run_status(
                session,
                run_id,
                status="failed",
                completed_at=datetime.now(tz=timezone.utc),
                error_log=str(exc),
            )
        if failed:
            await bus.publish(
                run_id,
                {"event": "run_failed", "status": "failed", "error": str(exc)},
            )
            return "failed"
    return await _publish_current_terminal(bus, run_id)


async def _defer_project_lock_retry(
    ctx: dict[str, Any],
    *,
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    path: str,
    options: dict[str, Any],
) -> bool:
    """Requeue one run that lost the per-project mutation-lock race.

    Multiple ARQ workers are a supported deployment shape.  Failing a newer
    run merely because another worker is still publishing the previous source
    revision loses that refresh. Keep a new run ``queued`` or a postprocess
    resume ``published`` and schedule a bounded, uniquely-idempotent retry.
    If retry can no longer be guaranteed, the common pre-lock recovery closes
    the former as failed and the latter as partial without changing GraphHead.
    """

    raw_attempt = options.get("_project_lock_retry", 0)
    try:
        attempt = max(0, int(raw_attempt))
    except (TypeError, ValueError):
        attempt = 0
    if attempt >= _PROJECT_LOCK_MAX_RETRIES:
        await _recover_prelock_failure(
            bus,
            project_id=project_id,
            run_id=run_id,
            stage="project_lock_retry",
            exc=RuntimeError("analysis_project_lock_retry_exhausted"),
        )
        return False

    next_attempt = attempt + 1
    retry_options = {**options, "_project_lock_retry": next_attempt}
    retry_job_id = f"analysis-lock-retry:{run_id}:{next_attempt}"
    try:
        queue = ctx.get("redis")
        if queue is None or not hasattr(queue, "enqueue_job"):
            from app.orchestrator.queue import get_queue

            queue = await get_queue()
        # A duplicate return means this exact retry is already present; it is
        # still safe to leave the AnalysisRun queued and point cancellation at
        # the same id.
        await queue.enqueue_job(
            "run_ingest",
            str(project_id),
            str(run_id),
            path,
            retry_options,
            _job_id=retry_job_id,
            _defer_by=_PROJECT_LOCK_RETRY_DELAY_SEC,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            _recover_prelock_failure(
                bus,
                project_id=project_id,
                run_id=run_id,
                stage="project_lock_retry_enqueue",
                exc=AnalysisCancelled(
                    "worker_cancelled_while_waiting_for_project_lock"
                ),
                cancelled=True,
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001 — never leave a queued ghost
        log.exception("analysis project lock retry enqueue failed run_id=%s", run_id)
        await _recover_prelock_failure(
            bus,
            project_id=project_id,
            run_id=run_id,
            stage="project_lock_retry_enqueue",
            exc=RuntimeError(
                "analysis_project_lock_retry_enqueue_failed:"
                f"{type(exc).__name__}"
            ),
        )
        return False

    async with SessionLocal() as session:
        # Lock before reading the JSON value. Cancellation and terminal close
        # also take this row lock, so this merge can neither erase a concurrent
        # cancel_requested flag nor overwrite completed postprocess stats with
        # a stale published snapshot.
        run = (
            await session.execute(
                select(AnalysisRun)
                .where(AnalysisRun.id == run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is not None and run.status in {"queued", "published"}:
            prior_stats = run.stats if isinstance(run.stats, dict) else {}
            run.stats = {
                **prior_stats,
                "job_id": retry_job_id,
                "project_lock_wait": {
                    "attempt": next_attempt,
                    "retry_in_sec": _PROJECT_LOCK_RETRY_DELAY_SEC,
                },
            }
            await session.commit()
    await bus.publish(
        run_id,
        {
            "event": "run_deferred",
            "reason": "analysis_project_already_running",
            "retry_in_sec": _PROJECT_LOCK_RETRY_DELAY_SEC,
            "attempt": next_attempt,
        },
    )
    return True


async def _run_status(run_id: uuid.UUID) -> str | None:
    async with SessionLocal() as session:
        return (
            await session.execute(
                select(AnalysisRun.status).where(AnalysisRun.id == run_id)
            )
        ).scalar_one_or_none()


def _seen_stats(index: SeenFactIndex | None) -> dict[str, Any]:
    if index is None:
        return {"storage": "not_initialized"}
    try:
        return index.stats()
    except Exception as exc:  # noqa: BLE001 — diagnostics must not mask a run result
        log.exception("analysis seen-index stats unavailable")
        return {"storage": "unavailable", "error": type(exc).__name__}


def _git_manifest_options(source: PreparedSource) -> dict[str, Any]:
    """Exact Git metadata accepted by :func:`build_source_manifest`.

    A generic immutable archive may not share Git's blob/tree identity, and a
    mutable directory must retain byte hashing plus the end-of-run recheck.
    """

    if (
        source.mutable
        or source.revision is None
        or source.mirror is None
        or source.source_kind not in {"manual_git", "git_mirror"}
    ):
        return {}
    return {
        "git_repository": source.mirror,
        "git_revision": source.revision,
        "tree_prefix": source.tree_prefix,
        # prepare_source created a new, previously non-existent detached
        # worktree at this exact revision. Arbitrary build_source_manifest
        # callers do not receive this bypass and must pass the dirty/HEAD proof.
        "trusted_git_checkout": True,
    }


async def _ensure_run_active(run_id: uuid.UUID) -> None:
    status = await _run_status(run_id)
    if status == "cancelled":
        raise AnalysisCancelled("analysis_cancelled")
    if status != "running":
        # A stale-run sweep or another terminal transition owns the DB state.
        # Continuing to ingest after that point would mutate a graph whose run
        # can no longer publish successfully.
        raise RuntimeError(f"analysis_run_not_active:{status or 'missing'}")


async def _ensure_postprocess_active(run_id: uuid.UUID) -> None:
    """Honor cancellation without rewriting a published source receipt."""

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(AnalysisRun.status, AnalysisRun.stats).where(
                    AnalysisRun.id == run_id
                )
            )
        ).one_or_none()
    if row is None:
        raise RuntimeError("analysis_postprocess_run_missing")
    status, stats = row
    postprocess = stats.get("postprocess") if isinstance(stats, dict) else None
    if isinstance(postprocess, dict) and postprocess.get("cancel_requested") is True:
        raise AnalysisCancelled("postprocess_cancel_requested")
    if status != "published":
        raise RuntimeError(f"analysis_postprocess_not_active:{status}")


async def _publish_current_terminal(bus: ProgressBus, run_id: uuid.UUID) -> str | None:
    status = await _run_status(run_id)
    if status in ANALYSIS_RUN_TERMINAL_STATUSES:
        await bus.publish(run_id, {"event": f"run_{status}", "status": status})
    elif status == "published":
        await bus.publish(run_id, {"event": "run_published", "status": status})
    return status


async def _current_published_stats(
    project_id: uuid.UUID, run_id: uuid.UUID
) -> dict[str, Any] | None:
    """Load the current source baseline from GraphHead, never wall-clock order."""

    async with SessionLocal() as session:
        row = (
            await session.execute(
                select(AnalysisRun, GraphHead)
                .join(
                    GraphHead,
                    and_(
                        GraphHead.project_id == AnalysisRun.project_id,
                        GraphHead.current_run_id == AnalysisRun.id,
                    ),
                )
                .where(
                    AnalysisRun.project_id == project_id,
                    AnalysisRun.id != run_id,
                    GraphHead.state == GRAPH_HEAD_READY,
                )
                .limit(1)
            )
        ).one_or_none()
    if row is None:
        return None
    run, head = row
    validate_graph_publication_receipt(run, head=head)
    return dict(run.stats or {})


async def _close_missing_deterministic_facts(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    source_names: set[str],
    seen_nodes: set[str] | SeenFactIndex,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex,
) -> dict[str, int]:
    """Close facts omitted by a successful authoritative analyzer refresh.

    Upserts alone cannot represent file deletion or rename because no new
    record arrives for the old identity.  We compare the successfully emitted
    logical identities with current facts owned by only the analyzers that ran
    (or were removed from project coverage), then bitemporally close omissions.
    Runtime observations, user data, and skipped live DB probes are outside
    this sweep. Agent-derived facts participate only when their isolated
    producer completed every file (or was authoritatively retired); inferred
    ``link_calls`` edges participate only after a complete recomputation.
    """

    if _settings.mnemos_env != "test":
        raise LegacyGraphWriteRejected(
            "live omission sweeps are disabled; graph promotion owns deletion"
        )

    if not source_names:
        return {"nodes": 0, "edges": 0}

    def owned_by_active(value: Any) -> bool:
        if isinstance(value, str):
            owners = {value}
        else:
            owners = {str(item) for item in (value or [])}
        # A shared fact survives while any non-refreshed producer still owns
        # it.  Closing on a mere intersection lets one analyzer delete another
        # analyzer's contribution.
        return bool(owners) and owners.issubset(source_names)

    closed_at = datetime.now(tz=timezone.utc)
    closed_nodes = 0
    last_node: tuple[str, datetime] | None = None
    while True:
        node_stmt = (
            select(Node.id, Node.valid_from, Node.created_by)
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .order_by(Node.id, Node.valid_from)
            .limit(500)
        )
        if last_node is not None:
            last_id, last_valid_from = last_node
            node_stmt = node_stmt.where(
                or_(
                    Node.id > last_id,
                    and_(Node.id == last_id, Node.valid_from > last_valid_from),
                )
            )
        node_rows = (await session.execute(node_stmt)).all()
        if not node_rows:
            break
        emitted_node_ids = (
            seen_nodes.contains_nodes(row[0] for row in node_rows)
            if isinstance(seen_nodes, SeenFactIndex)
            else seen_nodes
        )
        stale = [
            node_id
            for node_id, _valid_from, created_by in node_rows
            if owned_by_active(created_by) and node_id not in emitted_node_ids
        ]
        if stale:
            closed = await session.execute(
                update(Node)
                .where(
                    Node.project_id == project_id,
                    Node.valid_to.is_(None),
                    Node.id.in_(stale),
                )
                .values(valid_to=closed_at)
            )
            closed_nodes += max(0, int(closed.rowcount or 0))
        last_node = (node_rows[-1][0], node_rows[-1][1])

    closed_edges = 0
    last_edge: tuple[uuid.UUID, datetime] | None = None
    while True:
        edge_stmt = (
            select(
                Edge.id,
                Edge.valid_from,
                Edge.source_id,
                Edge.target_id,
                Edge.kind,
                Edge.created_by,
            )
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .order_by(Edge.id, Edge.valid_from)
            .limit(500)
        )
        if last_edge is not None:
            last_id, last_valid_from = last_edge
            edge_stmt = edge_stmt.where(
                or_(
                    Edge.id > last_id,
                    and_(Edge.id == last_id, Edge.valid_from > last_valid_from),
                )
            )
        edge_rows = (await session.execute(edge_stmt)).all()
        if not edge_rows:
            break
        emitted_edge_ids = (
            seen_edges.contains_edges(
                (row[2], row[3], row[4]) for row in edge_rows
            )
            if isinstance(seen_edges, SeenFactIndex)
            else seen_edges
        )
        stale = [
            edge_id
            for edge_id, _valid_from, source_id, target_id, kind, created_by in edge_rows
            if owned_by_active(created_by)
            and (source_id, target_id, kind) not in emitted_edge_ids
        ]
        if stale:
            closed = await session.execute(
                update(Edge)
                .where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                    Edge.id.in_(stale),
                )
                .values(valid_to=closed_at)
            )
            closed_edges += max(0, int(closed.rowcount or 0))
        last_edge = (edge_rows[-1][0], edge_rows[-1][1])

    return {"nodes": closed_nodes, "edges": closed_edges}


async def _supersede_stale_graph_summaries(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    retained_summary_ids: set[uuid.UUID] | None = None,
) -> int:
    """Retire narratives not regenerated against this successful graph run.

    Summary evidence is validated when the provider call completes, but a
    later source refresh can delete or replace those graph identities.  The
    default zero-token path intentionally creates no replacement prose, so it
    must close every prior current narrative rather than expose it as grounded
    against the new graph.  Summaries produced by this run remain current, as
    do prior rows whose exact evidence hash was revalidated as an L1-L3 cache
    hit.  Bounded L1-L3 limits therefore cannot leave unvisited older targets
    behind.

    Retained IDs are restored inside the caller-owned transaction in batches.
    Updating all candidates first avoids one unbounded ``NOT IN`` bind list;
    the private marker makes the restore precise and invisible until commit.
    """

    closure_marker = uuid.uuid4()
    result = await session.execute(
        update(Summary)
        .where(
            Summary.project_id == project_id,
            Summary.superseded_by.is_(None),
            or_(
                Summary.analysis_run_id.is_(None),
                Summary.analysis_run_id != run_id,
            ),
        )
        .values(superseded_by=closure_marker)
    )
    closed = max(0, int(result.rowcount or 0))
    retained = sorted(retained_summary_ids or (), key=str)
    for offset in range(0, len(retained), _SUMMARY_RETENTION_BATCH):
        restored = await session.execute(
            update(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.id.in_(
                    retained[offset : offset + _SUMMARY_RETENTION_BATCH]
                ),
                Summary.superseded_by == closure_marker,
            )
            .values(superseded_by=None)
        )
        closed -= max(0, int(restored.rowcount or 0))
    return max(0, closed)


# Names a SQL analyzer surfaces as "tables" from raw queries but that are not
# part of a project's domain data model: system catalogs (schema introspection)
# and parser artifacts. A dogfood run on a real Next.js app extracted
# sqlite_master, pg_namespace, the Oracle data dictionary, the DUAL
# pseudo-table, and ``set`` (from ``UPDATE x SET ...``) as data entities,
# inflating and polluting the data map. These never denote domain tables, so
# they are dropped at ingest. Conservatively scoped — ambiguous bare words
# (tables/columns/views) are intentionally NOT listed to avoid dropping a real
# domain table of that name.
_SYSTEM_DATA_ENTITY_NAMES: frozenset[str] = frozenset(
    {
        "set", "dual",  # SQL keyword artifact / Oracle pseudo-table
        "sqlite_master", "sqlite_sequence", "sqlite_stat1", "sqlite_temp_master",
        "information_schema", "schemata",
        "pg_namespace", "pg_class", "pg_attribute", "pg_type", "pg_index",
        "performance_schema", "sys",
        "user_tables", "user_tab_columns", "user_tab_cols", "user_views",
        "user_objects", "user_constraints", "all_tables", "all_tab_columns",
    }
)
_SYSTEM_DATA_ENTITY_PREFIXES: tuple[str, ...] = (
    "sqlite_", "pg_", "dba_", "v$", "gv$", "user_tab", "all_tab",
)

_MAX_GRAPH_IDENTIFIER_CHARS = 4096
_MAX_PRODUCER_NAME_CHARS = 256
_MAX_GRAPH_KIND_CHARS = 128
_CERTAINTY_VALUES = frozenset({"inferred", "asserted", "verified"})


def _valid_graph_text(value: Any, *, max_chars: int) -> bool:
    """Whether a machine-consumed graph identifier is safe to persist.

    The process boundary caps one JSONL record at 1 MiB.  This narrower field
    boundary prevents a source-controlled identifier from becoming an
    unbounded database/index key or smuggling a NUL into later Git, JSON, and
    MCP consumers.
    """

    return (
        isinstance(value, str)
        and 0 < len(value) <= max_chars
        and "\x00" not in value
    )


def _is_system_data_entity(name: str | None) -> bool:
    """True for SQL system catalogs / pseudo-tables that are not part of the
    project's domain data model and should not become DataEntity nodes."""
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in _SYSTEM_DATA_ENTITY_NAMES:
        return True
    return n.startswith(_SYSTEM_DATA_ENTITY_PREFIXES)


async def _record_payload(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    payload: dict[str, Any],
    accept_kinds: set[str],
    totals: dict[str, int],
    fresh: bool = False,
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
    source_root: str | Path | None = None,
    expected_source_name: str | None = None,
    run_id: uuid.UUID | None = None,
) -> bool:
    """Apply one analyzer JSON record if its record_type is in ``accept_kinds``.

    Production analysis always supplies ``run_id`` and therefore writes only
    to run-scoped staging.  ``run_id=None`` retains the historical direct
    helper behavior for isolated writer tests and non-orchestrator callers;
    ``run_ingest`` has no such branch, so a failed/cancelled run cannot mutate
    the live graph before promotion.

    ``fresh`` (PR-200) — the project graph was empty when the run started, so
    the supersede-UPDATE in ``upsert_*`` matches zero rows and can be skipped.
    ``seen_nodes``/``seen_edges`` guard the ONE exception: an id emitted twice
    in the same run (e.g. the ``http.<M>.<path>`` contract node both a Java
    handler and an HTML form produce) — the second write must supersede the
    first, so only the *first* occurrence skips the close. Omit the sets to
    disable the fast path (cold/re-analysis paths keep normal upserts).
    """
    record_type = payload.get("record_type")
    if record_type not in accept_kinds:
        return False
    if run_id is None and _settings.mnemos_env != "test":
        raise LegacyGraphWriteRejected(
            "live graph writes are disabled; production ingest requires run_id"
        )
    if run_id is None:
        # Compatibility for isolated historical writer tests only. Keeping the
        # import behind the test-environment gate makes the production ingest
        # dependency graph stage-only.
        from app.merge.writer import upsert_edge, upsert_node
    raw_source_name = payload.get("source_name")
    if not _valid_graph_text(
        raw_source_name, max_chars=_MAX_PRODUCER_NAME_CHARS
    ):
        return False
    if expected_source_name is not None and raw_source_name != expected_source_name:
        return False
    data = payload.get("data", {}) or {}
    if not isinstance(data, dict):
        return False
    data = dict(data)
    location = data.get("location")
    if location is not None and not isinstance(location, dict):
        return False
    if isinstance(location, dict) and "file" in location:
        file_value = location["file"]
        if not _valid_graph_text(
            file_value, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ):
            return False
        normalized_file = file_value.replace("\\", "/")
        if source_root is not None:
            try:
                file_path = Path(file_value)
                if file_path.is_absolute():
                    normalized_file = (
                        file_path.resolve()
                        .relative_to(Path(source_root).resolve())
                        .as_posix()
                    )
            except (OSError, ValueError):
                # Do not expose an ephemeral worker path as a source
                # reference.  An out-of-root location is a contract error.
                return False
        data["location"] = {**location, "file": normalized_file}
    source_name = raw_source_name

    name = data.get("name")
    if name is not None and not _valid_graph_text(
        name, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
    ):
        return False

    certainty = data.get("certainty")
    if certainty is not None and certainty not in _CERTAINTY_VALUES:
        return False

    def _node_skip(nid: str) -> bool:
        if seen_nodes is None:
            return False
        if isinstance(seen_nodes, SeenFactIndex):
            already_seen = seen_nodes.add_node(nid)
            return fresh and seen_nodes.fast_path_enabled and not already_seen
        already_seen = nid in seen_nodes
        seen_nodes.add(nid)
        return fresh and not already_seen

    def _edge_skip(key: tuple[str, str, str]) -> bool:
        if seen_edges is None:
            return False
        if isinstance(seen_edges, SeenFactIndex):
            already_seen = seen_edges.add_edge(key)
            return fresh and seen_edges.fast_path_enabled and not already_seen
        already_seen = key in seen_edges
        seen_edges.add(key)
        return fresh and not already_seen

    if record_type == "symbol":
        node_id = data.get("id")
        if not _valid_graph_text(
            node_id, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ):
            return False
        if run_id is None:
            await upsert_node(
                session,
                project_id=project_id,
                node_id=node_id,
                kind="Symbol",
                data=data,
                certainty=data.get("certainty", "asserted"),
                source_name=source_name,
                skip_close=_node_skip(node_id),
            )
        else:
            _node_skip(node_id)
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id=node_id,
                kind="Symbol",
                data=data,
                certainty=data.get("certainty", "asserted"),
                source_name=source_name,
            )
        totals["symbols"] += 1
        return True
    elif record_type == "contract":
        node_id = data.get("id")
        if not _valid_graph_text(
            node_id, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ):
            return False
        spec = data.get("spec") or {}
        if not isinstance(spec, dict):
            return False
        if data.get("kind") == "http_endpoint" and spec:
            method = spec.get("method", "GET")
            raw_path = spec.get("path", "/")
            if not _valid_graph_text(method, max_chars=32) or not _valid_graph_text(
                raw_path, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
            ):
                return False
            node_id = http_contract_id(method, raw_path)
            data = {**data, "id": node_id}
        if run_id is None:
            await upsert_node(
                session,
                project_id=project_id,
                node_id=node_id,
                kind="Contract",
                data=data,
                certainty=data.get("certainty", "inferred"),
                source_name=source_name,
                skip_close=_node_skip(node_id),
            )
        else:
            _node_skip(node_id)
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id=node_id,
                kind="Contract",
                data=data,
                certainty=data.get("certainty", "inferred"),
                source_name=source_name,
            )
        totals["contracts"] += 1
        return True
    elif record_type == "data_entity":
        node_id = data.get("id")
        if not _valid_graph_text(
            node_id, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ):
            return False
        # Skip SQL system catalogs / pseudo-tables (schema-introspection
        # targets, ``UPDATE..SET`` parser artifacts) — not domain data.
        if _is_system_data_entity(data.get("name") or node_id):
            return True
        if run_id is None:
            await upsert_node(
                session,
                project_id=project_id,
                node_id=node_id,
                kind="DataEntity",
                data=data,
                certainty=data.get("certainty", "verified"),
                source_name=source_name,
                skip_close=_node_skip(node_id),
            )
        else:
            _node_skip(node_id)
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id=node_id,
                kind="DataEntity",
                data=data,
                certainty=data.get("certainty", "verified"),
                source_name=source_name,
            )
        totals["data_entities"] = totals.get("data_entities", 0) + 1
        return True
    elif record_type == "edge":
        src = data.get("source_id")
        tgt = data.get("target_id")
        kind = data.get("kind", "CALLS")
        if not _valid_graph_text(
            src, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ) or not _valid_graph_text(
            tgt, max_chars=_MAX_GRAPH_IDENTIFIER_CHARS
        ):
            return False
        if not _valid_graph_text(kind, max_chars=_MAX_GRAPH_KIND_CHARS):
            return False
        metadata = data.get("metadata", {}) or {}
        if not isinstance(metadata, dict):
            return False
        if tgt.startswith("http.") and tgt.count(".") >= 2:
            method, _, rest = tgt.removeprefix("http.").partition(".")
            tgt = http_contract_id(method, rest)
        # Drop READS/WRITES edges to a SQL system catalog / pseudo-table so the
        # data map stays domain-only (matches the DataEntity node filter).
        if kind in ("READS", "WRITES") and _is_system_data_entity(
            tgt.removeprefix("data.")
        ):
            return True
        if run_id is None:
            await upsert_edge(
                session,
                project_id=project_id,
                source_id=src,
                target_id=tgt,
                kind=kind,
                data=metadata,
                certainty=data.get("certainty", "asserted"),
                source_name=source_name,
                skip_close=_edge_skip((src, tgt, kind)),
            )
        else:
            _edge_skip((src, tgt, kind))
            await stage_edge(
                session,
                project_id=project_id,
                run_id=run_id,
                source_id=src,
                target_id=tgt,
                kind=kind,
                data=metadata,
                certainty=data.get("certainty", "asserted"),
                source_name=source_name,
            )
        totals["edges"] += 1
        return True
    return False


_VERB_ACCEPT = {
    "symbols": {"symbol", "data_entity"},
    "contracts": {"contract", "edge"},
    "calls": {"edge"},
    # data_access emits the logical DataEntity nodes it discovered in
    # source (e.g. a table named in raw SQL) alongside the READS /
    # WRITES edges to them.
    "data_access": {"edge", "data_entity"},
    "live_schema": {"data_entity"},
}


async def _graph_is_empty(
    session: AsyncSession, project_id: uuid.UUID
) -> bool:
    """True when the project has no current node AND no current edge. Only then
    is the fresh-graph write fast path (PR-200) safe — every supersede-UPDATE
    would match zero rows."""
    node = (
        await session.execute(
            select(Node.id)
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .limit(1)
        )
    ).first()
    if node is not None:
        return False
    edge = (
        await session.execute(
            select(Edge.id)
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .limit(1)
        )
    ).first()
    return edge is None


@dataclass(frozen=True)
class AnalyzerStageOutcome:
    """Completeness proof for one producer verb.

    A producer receives deletion authority only when every required verb has
    an authoritative outcome.  Recoverable stderr and malformed JSON remain
    visible as partial coverage but can never authorize closing old facts.
    """

    producer: str | None
    verb: str
    authoritative: bool
    records: int = 0
    errors: tuple[str, ...] = ()


def _authoritative_agent_refresh_sources(
    source_manifest: SourceManifest | None,
    producer_outcomes: dict[str, list[AnalyzerStageOutcome]],
) -> set[str]:
    """Return Agent owners that proved complete manifest coverage.

    ``_run_agent_extraction_stage`` can be complete for the bounded file list
    it was given while the manifest contains additional files.  Such a pass
    is useful additive coverage, but it cannot authorize omission-based
    closure of older Agent facts.  Keep this proof identical to the later
    producer-coverage receipt and require one unambiguous fallback outcome.
    """

    if source_manifest is None:
        return set()
    authoritative: set[str] = set()
    for producer, outcomes in producer_outcomes.items():
        if producer not in source_manifest.fingerprints:
            continue
        extraction_outcomes = [
            outcome for outcome in outcomes if outcome.verb == "extract"
        ]
        if len(extraction_outcomes) != 1:
            continue
        outcome = extraction_outcomes[0]
        if (
            outcome.authoritative
            and outcome.records >= source_manifest.file_counts.get(producer, 0)
            and str(outcome.producer or "").startswith("agent:")
        ):
            authoritative.add(str(outcome.producer))
    return authoritative


async def _run_analyzer_stage(
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    language: str,
    verb: str,
    path: str,
    position: int,
    totals: dict[str, int],
    fresh: bool = False,
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
    stage_timeout_s: float = _ANALYZER_STAGE_TIMEOUT_SEC,
) -> AnalyzerStageOutcome:
    """One analyser verb (e.g. 'symbols' for 'csharp') as a tracked stage."""
    if stage_timeout_s <= 0:
        raise ValueError("stage_timeout_s must be > 0")
    await _ensure_run_active(run_id)
    producer = binary_for(language)
    runner = runner_for(language)
    if runner is None or not analyzer_available(language):
        # Skipped stage recorded so the GUI still shows it.
        async with StageTracker(
            bus,
            run_id,
            project_id,
            f"{verb}:{language}",
            language=language,
            position=position,
        ) as stage:
            stage.set_stats({"skipped": True, "reason": "no_analyzer"})
        return AnalyzerStageOutcome(
            producer=producer,
            verb=verb,
            authoritative=False,
            errors=("no_analyzer",),
        )

    async with StageTracker(
        bus,
        run_id,
        project_id,
        f"{verb}:{language}",
        language=language,
        position=position,
        time_budget_sec=max(1, int(stage_timeout_s)),
        suppress_budget_exceeded=False,
    ) as stage:
        try:
            # AnalyzerRunner bounds the child process, but the subprocess may
            # already have exited while a graph SELECT/upsert/commit is stuck.
            # The stage deadline therefore encloses consumption and all DB
            # work, not only ``proc.wait()``.  Cancelling this context also
            # closes the async generator, whose finally block kills the child.
            async with asyncio.timeout(stage_timeout_s):
                accept = _VERB_ACCEPT.get(verb, set())
                before_totals = dict(totals)
                contract_errors: list[str] = []
                records_seen = 0
                async with SessionLocal() as session:
                    pending_progress = 0
                    record_stream = runner.run(
                        verb,
                        path,
                        env={"MNEMOS_PROJECT_ID": str(project_id)},
                    )
                    async with aclosing(record_stream):
                        async for rec in record_stream:
                            if rec.stream == "stderr":
                                totals["errors"] += 1
                                message = str(
                                    rec.payload.get("message")
                                    or rec.payload.get("raw")
                                    or "analyzer_stderr"
                                )
                                if len(contract_errors) < 8:
                                    contract_errors.append(message[:500])
                                continue
                            before = sum(totals.values())
                            accepted = await _record_payload(
                                session,
                                project_id=project_id,
                                run_id=run_id,
                                payload=rec.payload,
                                accept_kinds=accept,
                                totals=totals,
                                fresh=fresh,
                                seen_nodes=seen_nodes,
                                seen_edges=seen_edges,
                                source_root=path,
                                expected_source_name=producer,
                            )
                            if accepted:
                                records_seen += 1
                            else:
                                totals["errors"] += 1
                                if len(contract_errors) < 8:
                                    contract_errors.append(
                                        "malformed_jsonl"
                                        if rec.payload.get("parse_error")
                                        else "unexpected_or_invalid_record"
                                    )
                            after = sum(totals.values())
                            if accepted and after > before:
                                pending_progress += after - before
                            # SQLite permits only one writer at a time. Commit
                            # graph rows before StageTracker opens its own
                            # progress-write session.
                            if pending_progress >= 50:
                                await session.commit()
                                await stage.increment(pending_progress)
                                pending_progress = 0
                                await _ensure_run_active(run_id)
                    await session.commit()
                    if pending_progress:
                        await stage.increment(pending_progress)
                    await _ensure_run_active(run_id)
        except TimeoutError as exc:
            raise StageBudgetExceeded(
                f"stage {verb}:{language} exceeded {stage_timeout_s:g}s hard deadline"
            ) from exc
        authoritative = not contract_errors
        stage.set_stats({
            **{
                key: totals.get(key, 0) - before_totals.get(key, 0)
                for key in ("symbols", "edges", "contracts", "data_entities")
            },
            "producer": producer,
            "verb": verb,
            "records": records_seen,
            "authoritative": authoritative,
            "contract_errors": contract_errors,
        })
        if not authoritative:
            stage.mark_partial("analyzer_output_incomplete")
    return AnalyzerStageOutcome(
        producer=producer,
        verb=verb,
        authoritative=authoritative,
        records=records_seen,
        errors=tuple(contract_errors),
    )


async def _record_agent_envelopes(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    envelopes: list[dict[str, Any]],
    accept_kinds: set[str],
    totals: dict[str, int],
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
    expected_source_name: str,
    run_id: uuid.UUID | None = None,
) -> tuple[int, int]:
    """Persist one canonical agent result with endpoint grounding.

    Node envelopes are applied first and flushed.  All edge endpoint IDs are
    then resolved in bounded, project-scoped queries across the published
    baseline plus this run's staged candidates (staged identities shadow the
    baseline) before any inferred edge is written.  This keeps a hallucinated
    table/symbol name from becoming a dangling graph edge while preserving
    run isolation and avoiding one query per edge.
    Returns ``(records_added, dangling_edges_dropped)``.
    """

    node_envelopes = [
        envelope for envelope in envelopes if envelope.get("record_type") != "edge"
    ]
    edge_envelopes = [
        envelope for envelope in envelopes if envelope.get("record_type") == "edge"
    ]
    added = 0
    for envelope in node_envelopes:
        before = sum(totals.values())
        await _record_payload(
            session,
            project_id=project_id,
            run_id=run_id,
            payload=envelope,
            accept_kinds=accept_kinds,
            totals=totals,
            seen_nodes=seen_nodes,
            seen_edges=seen_edges,
            expected_source_name=expected_source_name,
        )
        added += sum(totals.values()) - before

    # Stage writers add ORM rows; flush before querying so newly emitted nodes
    # and previously published deterministic nodes share one exact view.
    await session.flush()
    endpoint_ids: set[str] = set()
    for envelope in edge_envelopes:
        data = envelope.get("data")
        if not isinstance(data, dict):
            continue
        for key in ("source_id", "target_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                endpoint_ids.add(value)

    current_node_kinds: dict[str, str] = {}
    ordered_ids = sorted(endpoint_ids)
    # Stay below conservative SQLite bind-variable limits while keeping the
    # normal Postgres path bounded to a handful of set queries.
    for offset in range(0, len(ordered_ids), 400):
        page = ordered_ids[offset : offset + 400]
        current_node_kinds.update(
            dict(
                (
                    await session.execute(
                        select(Node.id, Node.kind).where(
                            Node.project_id == project_id,
                            Node.valid_to.is_(None),
                            Node.id.in_(page),
                        )
                    )
                ).all()
            )
        )
        if run_id is not None:
            current_node_kinds.update(
                dict(
                    (
                        await session.execute(
                            select(
                                GraphNodeStage.node_id, GraphNodeStage.kind
                            ).where(
                                GraphNodeStage.project_id == project_id,
                                GraphNodeStage.run_id == run_id,
                                GraphNodeStage.node_id.in_(page),
                            )
                        )
                    ).all()
                )
            )

    dropped = 0
    for envelope in edge_envelopes:
        data = envelope.get("data")
        source_id = data.get("source_id") if isinstance(data, dict) else None
        target_id = data.get("target_id") if isinstance(data, dict) else None
        edge_kind = data.get("kind") if isinstance(data, dict) else None
        source_kind = current_node_kinds.get(source_id)
        target_kind = current_node_kinds.get(target_id)
        kinds_match = (
            source_kind == "Symbol" and target_kind == "DataEntity"
            if edge_kind in {"READS", "WRITES"}
            else source_kind == target_kind == "DataEntity"
            if edge_kind == "REFERENCES" and expected_source_name == "agent:sql"
            else source_kind == target_kind == "Symbol"
        )
        if not kinds_match:
            dropped += 1
            continue
        before = sum(totals.values())
        await _record_payload(
            session,
            project_id=project_id,
            run_id=run_id,
            payload=envelope,
            accept_kinds=accept_kinds,
            totals=totals,
            seen_nodes=seen_nodes,
            seen_edges=seen_edges,
            expected_source_name=expected_source_name,
        )
        added += sum(totals.values()) - before
    return added, dropped


def _restore_agent_totals_after_failure(
    totals: dict[str, int], snapshot: dict[str, int], exc: Exception
) -> None:
    """Undo rolled-back counters and emit a source-safe diagnostic."""

    totals.clear()
    totals.update(snapshot)
    # Exception messages/tracebacks from an SDK or DB driver may contain raw
    # source values.  The class is sufficient to route the operational error.
    log.warning("agent_extract: ingest failed: %s", exc.__class__.__name__)


async def _run_agent_extraction_stage(
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    language: str,
    path: str,
    position: int,
    totals: dict[str, int],
    file_limit: int,
    run_budget: LLMRunBudget | None = None,
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
) -> AnalyzerStageOutcome:
    """PR-140 — Claude-Code source extraction for a language with no
    deterministic ggoss analyzer (spec principle #4: delegate to Claude
    Code). Hands each source file to the operator's Claude Code
    subscription and ingests the inferred symbols/edges through the same
    graph path as the analyzers. Degrades to a recorded skip when the
    Agent SDK isn't available or the language has no known file types."""
    from app.extractor.agent_extract import (
        AGENT_LANGUAGE_EXTENSIONS,
        MAX_AGENT_CODE_CHARS,
        discover_source_files,
        extract_file_via_agent_sdk,
        is_agent_sdk_available,
        to_envelopes,
    )

    stage_name = f"agent_extract:{language}"

    if not is_agent_sdk_available():
        async with StageTracker(
            bus, run_id, project_id, stage_name, language=language, position=position
        ) as stage:
            stage.set_stats({"skipped": True, "reason": "agent_sdk_unavailable"})
        return AnalyzerStageOutcome(
            f"agent:{language}", "extract", False,
            errors=("agent_sdk_unavailable",),
        )
    if language not in AGENT_LANGUAGE_EXTENSIONS:
        async with StageTracker(
            bus, run_id, project_id, stage_name, language=language, position=position
        ) as stage:
            stage.set_stats({"skipped": True, "reason": "language_not_supported"})
        return AnalyzerStageOutcome(
            f"agent:{language}", "extract", False,
            errors=("language_not_supported",),
        )

    files = discover_source_files(path, language, limit=file_limit)
    if not files:
        async with StageTracker(
            bus, run_id, project_id, stage_name, language=language, position=position
        ) as stage:
            stage.set_stats({"skipped": True, "reason": "no_source_files"})
        return AnalyzerStageOutcome(
            f"agent:{language}", "extract", False,
            errors=("no_source_files",),
        )

    # Code extraction emits symbols; SQL/DB extraction emits data_entity
    # nodes (tables). Accept both plus edges so either path ingests fully.
    accept = {"symbol", "data_entity", "edge"}
    files_done = 0
    files_failed = 0
    dangling_edges_dropped = 0
    budget_exhausted_reason: str | None = None
    if run_budget is None:
        run_budget = LLMRunBudget.from_env()
    deadline = time.monotonic() + 3600.0
    async with StageTracker(
        bus, run_id, project_id, stage_name,
        language=language, position=position, time_budget_sec=3600,
    ) as stage:
        for file_index, fpath in enumerate(files):
            await _ensure_run_active(run_id)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                files_failed += len(files) - file_index
                break
            try:
                # Never load an arbitrarily large source file just to reject
                # it inside the provider adapter.  One extra character tells
                # us whether complete-file analysis is possible.
                with fpath.open(encoding="utf-8", errors="replace") as handle:
                    code = handle.read(MAX_AGENT_CODE_CHARS + 1)
            except OSError:
                files_failed += 1
                continue
            if len(code) > MAX_AGENT_CODE_CHARS:
                files_failed += 1
                continue
            file_rel = str(fpath.relative_to(path)) if str(fpath).startswith(
                str(path)
            ) else fpath.name
            provider_timeout = remaining
            run_deadline_owns_timeout = False
            physical_call_started = False

            def mark_physical_call_started() -> None:
                nonlocal physical_call_started
                physical_call_started = True

            if run_budget is not None:
                try:
                    # The rolling project cap and the finite per-run budget
                    # both apply to Agent extraction.  Check the durable cap
                    # before reserving a run call so a blocked physical call
                    # is neither attempted nor charged to the in-memory run.
                    async with SessionLocal() as budget_session:
                        await require_budget(budget_session, project_id)
                    # Estimate the exact JSON-like input shape rather than raw
                    # characters so non-ASCII source is escaped conservatively.
                    # The allowance covers extraction instructions/schema.
                    run_remaining = _reserve_agent_provider_call(
                        run_budget,
                        language=language,
                        file_rel=file_rel,
                        code=code,
                    )
                    run_deadline_owns_timeout = run_remaining <= provider_timeout
                    provider_timeout = min(provider_timeout, run_remaining)
                except BudgetExceeded:
                    budget_exhausted_reason = "budget_exceeded"
                    run_budget.stop(budget_exhausted_reason)
                    files_failed += len(files) - file_index
                    break
                except RunBudgetExceeded as exc:
                    budget_exhausted_reason = exc.reason
                    files_failed += len(files) - file_index
                    break
            try:
                extracted = await asyncio.wait_for(
                    extract_file_via_agent_sdk(
                        language=language,
                        file_rel=file_rel,
                        code=code,
                        model=_AGENT_EXTRACT_MODEL,
                        # The orchestration timeout owns the shared run/stage
                        # deadline so it can ledger a timeout accurately.  A
                        # slightly longer integer SDK timeout remains a second
                        # line of defence for direct adapter callers.
                        timeout_s=max(1, int(provider_timeout) + 1),
                        on_provider_start=mark_physical_call_started,
                    ),
                    timeout=provider_timeout,
                )
            except TimeoutError:
                timeout_reason = (
                    _AGENT_RUN_DEADLINE
                    if run_deadline_owns_timeout
                    else _AGENT_EXTRACT_TIMEOUT
                )
                if run_deadline_owns_timeout and run_budget is not None:
                    run_budget.stop(timeout_reason)
                    budget_exhausted_reason = timeout_reason
                if physical_call_started:
                    await _record_agent_llm_call(
                        project_id=project_id,
                        run_id=run_id,
                        language=language,
                        file_rel=file_rel,
                        status="timeout",
                        fallback_reason=timeout_reason,
                    )
                files_failed += len(files) - file_index
                break
            except asyncio.CancelledError:
                if physical_call_started:
                    try:
                        await _record_agent_llm_call(
                            project_id=project_id,
                            run_id=run_id,
                            language=language,
                            file_rel=file_rel,
                            status="cancelled",
                            fallback_reason=_AGENT_EXTRACT_CANCELLED,
                        )
                    except Exception as ledger_exc:  # noqa: BLE001
                        # Cancellation is the control-plane truth. Preserve it
                        # even when the best-effort accounting write fails.
                        log.error(
                            "agent_extract: cancelled-call ledger failed: %s",
                            ledger_exc.__class__.__name__,
                        )
                raise
            except Exception as exc:  # noqa: BLE001
                # The adapter contract is fail-soft, but option construction
                # or a future regression can still escape before it returns a
                # provider outcome.  Do not fabricate a physical-call ledger
                # row; retain prior graph facts by making this pass partial.
                log.warning(
                    "agent_extract: adapter escaped unexpectedly: %s",
                    exc.__class__.__name__,
                )
                if physical_call_started:
                    await _record_agent_llm_call(
                        project_id=project_id,
                        run_id=run_id,
                        language=language,
                        file_rel=file_rel,
                        status="failed",
                        fallback_reason=_AGENT_EXTRACT_FAILED,
                    )
                files_failed += 1
                continue
            # A valid payload proves a physical round-trip even for a legacy
            # test adapter that predates the optional start hook.
            physical_call_started = physical_call_started or extracted is not None
            if physical_call_started:
                await _record_agent_llm_call(
                    project_id=project_id,
                    run_id=run_id,
                    language=language,
                    file_rel=file_rel,
                    status="completed" if extracted else "fallback",
                    fallback_reason=None if extracted else _AGENT_EXTRACT_FAILED,
                )
            if not extracted:
                files_failed += 1
                continue
            # Ingest + COMMIT this file's nodes in a short-lived session,
            # then report progress. Committing before stage.increment()
            # releases the SQLite write lock so the StageTracker's
            # progress flush (a separate session) never contends — the
            # PR-141 "database is locked" failure. A per-file DB error
            # degrades that file, never the whole run.
            added = 0
            totals_snapshot = dict(totals)
            try:
                async with SessionLocal() as session:
                    added, rejected_edges = await _record_agent_envelopes(
                        session,
                        project_id=project_id,
                        run_id=run_id,
                        envelopes=to_envelopes(language, file_rel, extracted),
                        accept_kinds=accept,
                        totals=totals,
                        seen_nodes=seen_nodes,
                        seen_edges=seen_edges,
                        expected_source_name=f"agent:{language}",
                    )
                    await session.commit()
            except Exception as exc:  # noqa: BLE001
                _restore_agent_totals_after_failure(
                    totals, totals_snapshot, exc
                )
                files_failed += 1
                continue
            files_done += 1
            if rejected_edges:
                dangling_edges_dropped += rejected_edges
                # A syntactically valid provider response still does not own
                # permission to create graph references to absent facts.  The
                # accepted nodes remain useful inferred coverage, but deletion
                # sweep authority is withheld for this partial file result.
            if added:
                await stage.increment(added)
        authoritative = files_failed == 0 and dangling_edges_dropped == 0
        stage.set_stats(
            {
                "files_analyzed": files_done,
                "files_failed": files_failed,
                "symbols": totals.get("symbols", 0),
                "edges": totals.get("edges", 0),
                "dangling_edges_dropped": dangling_edges_dropped,
                "extractor": "claude_code",
                "authoritative": authoritative,
                "run_budget": run_budget.stats() if run_budget is not None else None,
                "budget_exhausted_reason": budget_exhausted_reason,
            }
        )
        if not authoritative:
            stage.mark_partial("agent_extraction_incomplete")
    return AnalyzerStageOutcome(
        f"agent:{language}",
        "extract",
        authoritative,
        records=files_done,
        errors=(
            (("agent_file_failures",) if files_failed else ())
            + (("agent_dangling_edges_dropped",) if dangling_edges_dropped else ())
        ),
    )


async def _run_call_linking_stage(
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    position: int,
    totals: dict[str, int],
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
    refreshed_agent_sources: set[str] | None = None,
) -> None:
    """PR-154 — resolve agent-extracted cross-file calls into CALLS edges.

    Each agent-extracted Symbol carries ``data.calls_out`` (bare callee
    names, possibly defined in another file). Match each name to a Symbol
    elsewhere in the project; create a CALLS edge only when the match is
    unambiguous (exactly one non-self symbol of that name) to keep
    precision. This is what makes find_callers/callees work across files
    for Claude-extracted code, not just within a single file."""
    async with StageTracker(
        bus, run_id, project_id, "link_calls", position=position, time_budget_sec=120
    ) as stage:
        async with SessionLocal() as session:
            linked = await link_inferred_calls(
                session,
                project_id,
                totals,
                run_id=run_id,
                seen_nodes=seen_nodes,
                seen_edges=seen_edges,
                refreshed_agent_sources=refreshed_agent_sources,
            )
            await session.commit()
        for _ in range(linked):
            await stage.increment()
        stage.set_stats({"links_created": linked})


async def link_inferred_calls(
    session: AsyncSession,
    project_id: uuid.UUID,
    totals: dict[str, int],
    *,
    run_id: uuid.UUID | None = None,
    seen_nodes: set[str] | SeenFactIndex | None = None,
    seen_edges: set[tuple[str, str, str]] | SeenFactIndex | None = None,
    refreshed_agent_sources: set[str] | None = None,
) -> int:
    """Resolve agent-extracted ``data.calls_out`` callee names into CALLS
    edges, unambiguous matches only. Returns the number of edges created.
    Pure-DB (no bus/stage) so it is directly testable."""
    published_symbols = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
            )
        )
    ).scalars().all()
    # A run-local materialized view: published rows provide the incremental
    # baseline while staged identities replace them.  Without this overlay the
    # linker cannot see symbols emitted earlier in the same isolated run.
    symbols_by_id: dict[str, Any] = {node.id: node for node in published_symbols}
    if run_id is not None:
        staged_symbols = (
            await session.execute(
                select(GraphNodeStage).where(
                    GraphNodeStage.project_id == project_id,
                    GraphNodeStage.run_id == run_id,
                    GraphNodeStage.kind == "Symbol",
                )
            )
        ).scalars().all()

        @dataclass(frozen=True)
        class _StagedSymbolView:
            id: str
            data: dict[str, Any]
            created_by: list[str]

        for node in staged_symbols:
            symbols_by_id[node.node_id] = _StagedSymbolView(
                id=node.node_id,
                data=dict(node.data),
                created_by=[node.source_name],
            )
    symbols = list(symbols_by_id.values())
    refreshed_agent_sources = refreshed_agent_sources or set()
    if refreshed_agent_sources and seen_nodes is not None:
        def node_owners(node: Any) -> set[str]:
            value = node.created_by
            if isinstance(value, str):
                return {value}
            return {str(owner) for owner in (value or [])}

        candidates = [
            node
            for node in symbols
            if (owners := node_owners(node))
            and owners.issubset(refreshed_agent_sources)
        ]
        emitted_ids = (
            seen_nodes.contains_nodes(node.id for node in candidates)
            if isinstance(seen_nodes, SeenFactIndex)
            else seen_nodes
        )
        stale_ids = {
            node.id for node in candidates if node.id not in emitted_ids
        }
        # A changed Agent file can delete a Symbol before the bitemporal sweep
        # runs.  Do not relink that stale current row and then preserve its
        # derived edge as freshly emitted.
        symbols = [node for node in symbols if node.id not in stale_ids]

    by_name: dict[str, list[str]] = {}
    callers: list[Any] = []
    for n in symbols:
        nm = (n.data or {}).get("name")
        if nm:
            by_name.setdefault(nm, []).append(n.id)
        if (n.data or {}).get("calls_out"):
            callers.append(n)

    linked = 0
    for n in callers:
        for callee in (n.data or {}).get("calls_out") or []:
            targets = [i for i in by_name.get(callee, []) if i != n.id]
            if len(targets) != 1:
                continue  # unresolved or ambiguous — skip for precision
            before = sum(totals.values())
            await _record_payload(
                session,
                project_id=project_id,
                run_id=run_id,
                payload={
                    "record_type": "edge",
                    "source_name": "link_calls",
                    "data": {
                        "source_id": n.id,
                        "target_id": targets[0],
                        "kind": "CALLS",
                        "certainty": "inferred",
                        "metadata": {"extractor": "claude_code", "resolved": "cross_file"},
                    },
                },
                accept_kinds={"edge"},
                totals=totals,
                seen_edges=seen_edges,
            )
            if sum(totals.values()) > before:
                linked += 1
    return linked


async def _run_db_live_schema_stages(
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    position: int,
    totals: dict[str, int],
    authoritative_sources: set[str] | None = None,
) -> int:
    """One ``live_schema`` stage per registered ProjectDB.

    Each ProjectDB row carries a ``kind`` (mssql/oracle) which selects
    the matching analyzer binary. The plaintext connection string is
    decrypted from the linked Secret and passed via ``--conn-ref`` so
    the analyzer (not the platform) opens the DB connection — the
    spec §7.3/§7.4 isolation invariant.
    """
    from app.data_sampler.maintenance import is_within_windows
    from app.models.projects import ProjectDB
    from app.safety.crypto import decrypt
    from app.secret_scope import SecretScopeNotFound, resolve_project_secret
    from sqlalchemy import select

    async with SessionLocal() as session:
        result = await session.execute(
            select(ProjectDB).where(ProjectDB.project_id == project_id)
        )
        pdbs = result.scalars().all()

    producer_results: dict[str, list[bool]] = {}
    for pdb in pdbs:
        producer = binary_for(pdb.kind)
        if producer is not None:
            producer_results.setdefault(producer, [])
        position += 1
        # Honour the per-DB maintenance window if one is configured;
        # the spec's "DMV-heavy verbs only at quiet hours" guarantee.
        if not is_within_windows(list(pdb.maintenance_windows or [])):
            async with StageTracker(
                bus, run_id, project_id,
                f"live_schema:{pdb.kind}:{pdb.display_name}",
                language=pdb.kind, position=position,
            ) as stage:
                stage.set_stats({"skipped": True, "reason": "outside_maintenance_window"})
            if producer is not None:
                producer_results[producer].append(False)
            continue

        runner = runner_for(pdb.kind)
        if runner is None:
            async with StageTracker(
                bus, run_id, project_id,
                f"live_schema:{pdb.kind}:{pdb.display_name}",
                language=pdb.kind, position=position,
            ) as stage:
                stage.set_stats({"skipped": True, "reason": "no_analyzer"})
            if producer is not None:
                producer_results[producer].append(False)
            continue

        # Decrypt the connection string out-of-band; never log it.
        conn = None
        if pdb.secret_id is not None:
            async with SessionLocal() as session:
                try:
                    secret = await resolve_project_secret(
                        session,
                        project_id=project_id,
                        secret_id=pdb.secret_id,
                    )
                except SecretScopeNotFound:
                    secret = None
                if secret is not None:
                    try:
                        conn = decrypt(secret.ciphertext, secret.iv)
                    except Exception:
                        conn = None
        if conn is None:
            async with StageTracker(
                bus, run_id, project_id,
                f"live_schema:{pdb.kind}:{pdb.display_name}",
                language=pdb.kind, position=position,
            ) as stage:
                stage.set_stats({"skipped": True, "reason": "no_secret_or_decrypt_failed"})
            if producer is not None:
                producer_results[producer].append(False)
            continue

        async with StageTracker(
            bus, run_id, project_id,
            f"live_schema:{pdb.kind}:{pdb.display_name}",
            language=pdb.kind, position=position, time_budget_sec=900,
        ) as stage:
            accept = _VERB_ACCEPT.get("live_schema", set())
            stage_authoritative = True
            async with SessionLocal() as session:
                pending_progress = 0
                # Pass the connection string via env, not argv, so
                # ``ps``/process listings never expose the credential.
                async for rec in runner.run(
                    "live_schema",
                    pdb.component_id,
                    env={"MNEMOS_DB_CONN": conn},
                ):
                    if rec.stream == "stderr":
                        totals["errors"] += 1
                        stage_authoritative = False
                        continue
                    before = sum(totals.values())
                    accepted = await _record_payload(
                        session,
                        project_id=project_id,
                        run_id=run_id,
                        payload=rec.payload,
                        accept_kinds=accept,
                        totals=totals,
                        expected_source_name=binary_for(pdb.kind),
                    )
                    if not accepted:
                        totals["errors"] += 1
                        stage_authoritative = False
                    after = sum(totals.values())
                    if after > before:
                        pending_progress += after - before
                    # Same commit-before-progress invariant as source stages:
                    # StageTracker writes through a separate session and would
                    # otherwise self-deadlock SQLite at its 25-item flush.
                    if pending_progress >= 50:
                        await session.commit()
                        await stage.increment(pending_progress)
                        pending_progress = 0
                await session.commit()
                if pending_progress:
                    await stage.increment(pending_progress)
            stage.set_stats({"data_entities": totals.get("data_entities", 0)})
            if not stage_authoritative:
                stage.mark_partial("live_schema_output_incomplete")
            if producer is not None:
                producer_results[producer].append(stage_authoritative)

    if authoritative_sources is not None:
        authoritative_sources.update(
            producer
            for producer, results in producer_results.items()
            if results and all(results)
        )

    return position


async def _graph_inventory(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, int]:
    """Distinct current-graph entity counts (rows with ``valid_to IS NULL``).

    ``run.stats`` historically carried ``totals`` — the number of ingest
    *records* — which double-counts any symbol/contract/data-entity that is
    referenced from several sites. A table named in six SQL statements
    ingests six ``data_entity`` records but is a single node; a route handler
    re-emitted per call site inflates the contract count. A dogfood run on a
    real Next.js app reported 66 data entities / 63 contracts where the true
    distinct graph held 34 / 47 (data entities ~2x inflated). Report the
    distinct current nodes/edges so the operator-facing headline matches the
    graph that queries actually traverse.
    """
    inv: dict[str, int] = {}
    for kind, key in (
        ("Symbol", "symbols"),
        ("Contract", "contracts"),
        ("DataEntity", "data_entities"),
    ):
        inv[key] = int(
            (
                await session.execute(
                    select(func.count(func.distinct(Node.id))).where(
                        Node.project_id == project_id,
                        Node.kind == kind,
                        Node.valid_to.is_(None),
                    )
                )
            ).scalar()
            or 0
        )
    inv["edges"] = int(
        (
            await session.execute(
                select(func.count())
                .select_from(Edge)
                .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            )
        ).scalar()
        or 0
    )
    return inv


def _compose_analysis_stats(
    *,
    project_id: uuid.UUID,
    totals: dict[str, int],
    inventory: dict[str, int] | None,
    inherited_source_snapshot: dict[str, Any] | None,
    source_manifest: SourceManifest | None,
    prepared_source: PreparedSource | None,
    source_ref: str | None,
    run_git_sha: str,
    producer_coverage: dict[str, dict[str, Any]],
    legacy_database_languages: list[str],
    scope: str,
    requested_scope: str,
    selected_analyzers: set[str],
    removed_analyzers: set[str],
    sweep_sources: set[str],
    deletion_sweep_deferred_reason: str | None,
    previous_stats: dict[str, Any] | None,
    summarize: bool,
    agent_limit: int,
    llm_run_budget: LLMRunBudget | None,
    seen_index: SeenFactIndex | None,
) -> dict[str, Any]:
    """Build persisted run metadata without reading mutable graph state.

    The same deterministic builder is used immediately before atomic
    publication and once more after derived products finish.  A crash after
    publication therefore still leaves source provenance and coverage on the
    published run; later enrichment only adds inventory/derived counters.
    """

    final_stats: dict[str, Any] = dict(totals)
    if inventory is not None:
        final_stats.update(inventory)
    if inherited_source_snapshot is not None:
        final_stats["source_snapshot"] = inherited_source_snapshot
    if source_manifest is not None:
        final_stats["source_manifest"] = source_manifest.as_dict()
        final_stats["source_provenance"] = {
            "kind": prepared_source.source_kind if prepared_source else None,
            "root_id": prepared_source.root_id if prepared_source else None,
            "ref": prepared_source.source_ref if prepared_source else source_ref,
            "revision": prepared_source.revision if prepared_source else run_git_sha,
            "authoritative_root": (
                prepared_source.authoritative_root if prepared_source else None
            ),
            "mutable": prepared_source.mutable if prepared_source else None,
            "tree_prefix": prepared_source.tree_prefix if prepared_source else "",
        }
        if prepared_source and prepared_source.revision:
            source_snapshot: dict[str, Any] = {
                "contract_version": SOURCE_SNAPSHOT_CONTRACT,
                "kind": "git_commit",
                "revision": prepared_source.revision,
                "repository_key": str(project_id),
                "tree_prefix": prepared_source.tree_prefix,
                "immutable": True,
            }
            if prepared_source.repository_locator is not None:
                source_snapshot["repository_locator"] = (
                    prepared_source.repository_locator
                )
            final_stats["source_snapshot"] = source_snapshot
        final_stats["producer_coverage"] = producer_coverage
        coverage_gaps = sorted(
            producer
            for producer, coverage in producer_coverage.items()
            if coverage.get("authoritative") is not True
            and source_manifest.file_counts.get(producer, 0) > 0
        )
        final_stats["coverage"] = {
            "status": "partial" if coverage_gaps else "complete",
            "gaps": coverage_gaps,
            "legacy_database_languages_ignored": legacy_database_languages,
        }
        final_stats["refresh"] = {
            "mode": scope,
            "requested_mode": requested_scope,
            "changed_analyzers": sorted(selected_analyzers),
            "removed_analyzers": sorted(removed_analyzers),
            "unchanged_analyzers": sorted(
                set(source_manifest.fingerprints).difference(selected_analyzers)
            ),
            "content_noop": scope == "incremental"
            and not selected_analyzers
            and not removed_analyzers,
            "authoritative_root": (
                prepared_source.authoritative_root
                if prepared_source is not None
                else None
            ),
            "deletion_sweep_sources": sorted(sweep_sources),
            "deletion_sweep_deferred_reason": deletion_sweep_deferred_reason,
        }
    elif scope == "continuation" and previous_stats is not None:
        for key in (
            "source_manifest",
            "source_provenance",
            "producer_coverage",
            "coverage",
            "source_snapshot",
        ):
            value = previous_stats.get(key)
            if value is not None:
                final_stats[key] = value
        final_stats["refresh"] = {
            "mode": "continuation",
            "requested_mode": requested_scope,
            "content_noop": True,
            "source_baseline_inherited": True,
            "deletion_sweep_sources": [],
            "deletion_sweep_deferred_reason": "continuation_does_not_index",
        }
    final_stats["ai_narration"] = {
        "enabled": summarize,
        "default_mode": "deterministic_index",
        "initial_llm_tokens_when_disabled": 0,
        "agent_extraction_limit": agent_limit,
        "run_budget": llm_run_budget.stats() if llm_run_budget is not None else None,
    }
    final_stats["seen_index"] = _seen_stats(seen_index)
    final_stats["history_retention"] = {
        "automatic_prune": False,
        "reason": "bitemporal_compare_contract",
    }
    return final_stats


def _bounded_postprocess_message(value: Any) -> str:
    text = str(value).strip() or type(value).__name__
    if len(text) <= _MAX_POSTPROCESS_ERROR_CHARS:
        return text
    return text[: _MAX_POSTPROCESS_ERROR_CHARS - 16] + "\n...[truncated]"


def _postprocess_error(
    stage: str,
    exc: BaseException,
    *,
    cancelled: bool = False,
) -> dict[str, Any]:
    """Return the bounded, non-sensitive error envelope persisted and emitted."""

    return {
        "stage": stage,
        "type": type(exc).__name__,
        "message": _bounded_postprocess_message(exc),
        "cancelled": cancelled,
        "at": datetime.now(tz=timezone.utc).isoformat(),
    }


def _append_postprocess_error(
    errors: list[dict[str, Any]],
    stage: str,
    exc: BaseException,
) -> None:
    if len(errors) < _MAX_POSTPROCESS_ERRORS:
        errors.append(_postprocess_error(stage, exc))


async def _load_current_published_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate the receipt/head pair before reading the published graph."""

    async with SessionLocal() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
    if (
        run is None
        or run.project_id != project_id
        or run.status != "published"
        or head is None
    ):
        raise GraphPublicationInvariantError(
            "published postprocess requires the current receipt/head pair"
        )
    receipt = validate_graph_publication_receipt(run, head=head)
    return dict(run.stats or {}), receipt.to_payload()


async def _close_published_run(
    bus: ProgressBus,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    final_status: str,
    derived_stats: dict[str, Any],
    postprocess_errors: list[dict[str, Any]],
) -> str:
    """Close ``published`` as completed/partial without mutating GraphHead.

    GraphHead is locked before AnalysisRun, matching promotion lock order. If
    another publication won after derived reads, this run cannot honestly
    claim completed; it is closed as partial with a bounded race receipt.
    """

    if final_status not in {"completed", "partial"}:
        raise ValueError(f"invalid postprocess terminal status: {final_status}")
    completed_at = datetime.now(tz=timezone.utc)
    async with SessionLocal() as session:
        head = (
            await session.execute(
                select(GraphHead)
                .where(GraphHead.project_id == project_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        run = (
            await session.execute(
                select(AnalysisRun)
                .where(
                    AnalysisRun.id == run_id,
                    AnalysisRun.project_id == project_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if run is None:
            raise GraphPublicationInvariantError(
                "published AnalysisRun disappeared during postprocess"
            )
        if run.status in {"completed", "partial"}:
            return run.status
        if run.status != "published":
            raise GraphPublicationInvariantError(
                "postprocess can close only a run with a publication receipt"
            )
        publication = validate_graph_publication_receipt(run)
        receipt = publication.to_payload()
        prior_stats = dict(run.stats or {})
        derived_stats = dict(derived_stats)
        bounded_errors = [
            dict(item) for item in postprocess_errors[:_MAX_POSTPROCESS_ERRORS]
        ]
        if head is None:
            head_matches = False
        else:
            try:
                validate_graph_publication_receipt(run, head=head)
            except GraphPublicationInvariantError:
                head_matches = False
            else:
                head_matches = True
        if not head_matches:
            head_error = _postprocess_error(
                "finalize",
                GraphPublicationInvariantError(
                    "publication head changed before postprocess close"
                ),
            )
            bounded_errors = [head_error, *bounded_errors][
                :_MAX_POSTPROCESS_ERRORS
            ]
            final_status = "partial"
            prior_postprocess = derived_stats.get("postprocess")
            postprocess_update = (
                dict(prior_postprocess)
                if isinstance(prior_postprocess, dict)
                else {}
            )
            postprocess_update["findings_freshness"] = {
                "status": "unknown",
                "reason": "publication_head_changed_before_close",
            }
            derived_stats["postprocess"] = postprocess_update
        prior_postprocess = prior_stats.get("postprocess")
        derived_postprocess = derived_stats.get("postprocess")
        postprocess = {
            **(
                prior_postprocess
                if isinstance(prior_postprocess, dict)
                else {}
            ),
            **(
                derived_postprocess
                if isinstance(derived_postprocess, dict)
                else {}
            ),
            "status": final_status,
            "completed_at": completed_at.isoformat(),
            "errors": bounded_errors,
        }
        final_stats = {
            **prior_stats,
            **derived_stats,
            "graph_publication": receipt,
            "postprocess": postprocess,
        }
        if bounded_errors:
            final_stats["postprocess_error"] = bounded_errors[0]
        else:
            final_stats.pop("postprocess_error", None)
        run.status = final_status
        run.completed_at = completed_at
        run.stats = final_stats
        run.error_log = (
            bounded_errors[0]["message"] if bounded_errors else None
        )
        await session.commit()

    audit_details: dict[str, Any] = {
        "status": final_status,
        "graph_generation": receipt.get("generation"),
        "postprocess": postprocess,
    }
    try:
        await audit_record(
            actor="system",
            action=f"analysis.{final_status}",
            target=str(run_id),
            project_id=project_id,
            details=audit_details,
        )
    except Exception:  # noqa: BLE001 — terminal DB receipt remains authoritative
        log.exception("analysis terminal audit failed run_id=%s", run_id)
    try:
        from app.obs.metrics import analysis_runs_total

        analysis_runs_total.labels(status=final_status).inc()
    except Exception:
        pass

    event: dict[str, Any] = {
        "event": f"run_{final_status}",
        "status": final_status,
        "graph_generation": receipt.get("generation"),
        "findings_freshness": postprocess.get("findings_freshness"),
    }
    if bounded_errors:
        event["postprocess_error"] = bounded_errors[0]
        event["error"] = bounded_errors[0]["message"]
    try:
        await bus.publish(run_id, event)
    except Exception:  # noqa: BLE001 — SSE is a projection of durable state
        log.exception("analysis terminal progress publish failed run_id=%s", run_id)
    return final_status


async def _partialize_published_run(
    bus: ProgressBus,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    stage: str,
    exc: BaseException,
    cancelled: bool = False,
) -> str:
    error = _postprocess_error(stage, exc, cancelled=cancelled)
    return await _close_published_run(
        bus,
        project_id=project_id,
        run_id=run_id,
        final_status="partial",
        derived_stats={
            "postprocess": {
                "findings_freshness": {
                    "status": "unknown",
                    "reason": f"postprocess_{stage}_did_not_finish",
                }
            }
        },
        postprocess_errors=[error],
    )


async def _run_published_postprocess(
    bus: ProgressBus,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    summarize: bool,
    l1_limit: int,
    l2_limit: int,
    l3_limit: int,
    position: int,
) -> str:
    """Build derived products after the source graph receipt is durable."""

    base_stats, receipt = await _load_current_published_run(project_id, run_id)
    await bus.publish(
        run_id,
        {
            "event": "run_published",
            "status": "published",
            "graph_generation": receipt.get("generation"),
            "published_at": receipt.get("published_at"),
        },
    )
    errors: list[dict[str, Any]] = []
    stage_stats: dict[str, Any] = {}
    derived: dict[str, Any] = {
        "graph_generation": receipt.get("generation"),
        "history_retention": {
            "automatic_prune": False,
            "reason": "bitemporal_compare_contract",
        },
    }
    counts = receipt.get("counts")
    if isinstance(counts, dict):
        for key in ("stale_nodes_closed", "stale_edges_closed"):
            value = counts.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                derived[key] = value
    current_stage = "runtime_reconcile"
    retained_summary_ids: set[uuid.UUID] = set()
    findings_freshness: dict[str, str] = {
        "status": "unknown",
        "reason": "findings_not_started",
    }

    try:
        await _ensure_postprocess_active(run_id)
        runtime_ok = False
        position += 1
        try:
            async with StageTracker(
                bus,
                run_id,
                project_id,
                "runtime_reconcile",
                position=position,
                time_budget_sec=600,
                suppress_budget_exceeded=False,
            ) as stage:
                async with SessionLocal() as session:
                    project = await session.get(Project, project_id)
                    if project is None:
                        raise RuntimeError("postprocess_project_not_found")
                    runtime_stats = await reconcile_observations(
                        session,
                        project_id=project_id,
                        organization_id=project.organization_id,
                    )
                    await session.commit()
                runtime_ok = True
                stage_stats["runtime_reconcile"] = {
                    "status": "completed",
                    **runtime_stats,
                }
                stage.set_stats(stage_stats["runtime_reconcile"])
                for key in ("matched", "unmatched"):
                    value = runtime_stats.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        derived[f"runtime_{key}"] = value
        except (AnalysisCancelled, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — findings may still be useful
            _append_postprocess_error(errors, "runtime_reconcile", exc)
            stage_stats["runtime_reconcile"] = {
                "status": "failed",
                "error": errors[-1] if errors else None,
            }

        current_stage = "findings"
        await _ensure_postprocess_active(run_id)
        position += 1
        try:
            async with StageTracker(
                bus,
                run_id,
                project_id,
                "findings",
                position=position,
                time_budget_sec=600,
                suppress_budget_exceeded=False,
            ) as stage:
                async with SessionLocal() as session:
                    finding_stats = await rebuild_findings(session, project_id)
                    await session.commit()
                findings_freshness = (
                    {"status": "fresh", "reason": "runtime_reconciled_first"}
                    if runtime_ok
                    else {
                        "status": "partial",
                        "reason": "runtime_reconcile_failed",
                    }
                )
                stage_stats["findings"] = {
                    "status": "completed",
                    "freshness": findings_freshness,
                    **finding_stats,
                }
                stage.set_stats(stage_stats["findings"])
                derived["findings"] = sum(
                    value
                    for value in finding_stats.values()
                    if isinstance(value, int) and not isinstance(value, bool)
                )
        except (AnalysisCancelled, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 — narration remains independent
            _append_postprocess_error(errors, "findings", exc)
            findings_freshness = {
                "status": "unknown",
                "reason": "findings_rebuild_failed",
            }
            stage_stats["findings"] = {
                "status": "failed",
                "freshness": findings_freshness,
                "error": errors[-1] if errors else None,
            }

        if summarize:
            extractor = Extractor()
            run_budget = LLMRunBudget.from_env()
            for _level, label, fn, limit in (
                (1, "l1_summaries", summarise_l1, l1_limit),
                (2, "l2_summaries", summarise_l2, l2_limit),
                (3, "l3_summaries", summarise_l3, l3_limit),
            ):
                current_stage = label
                await _ensure_postprocess_active(run_id)
                position += 1
                try:
                    async with StageTracker(
                        bus,
                        run_id,
                        project_id,
                        label,
                        position=position,
                        time_budget_sec=1200,
                        suppress_budget_exceeded=False,
                    ) as stage:

                        async def _summary_progress() -> None:
                            await _ensure_postprocess_active(run_id)
                            await stage.increment()

                        async with SessionLocal() as session:
                            produced = await fn(
                                session,
                                extractor,
                                project_id=project_id,
                                limit=limit,
                                progress_cb=_summary_progress,
                                analysis_run_id=run_id,
                                run_budget=run_budget,
                                retained_summary_ids=retained_summary_ids,
                            )
                        derived[label] = produced
                        stage_stats[label] = {
                            "status": "completed",
                            label: produced,
                            "limit": limit,
                        }
                        stage.set_stats(
                            {
                                **stage_stats[label],
                                "run_budget": run_budget.stats(),
                            }
                        )
                except (AnalysisCancelled, asyncio.CancelledError):
                    raise
                except Exception as exc:  # noqa: BLE001 — record each bounded failure
                    _append_postprocess_error(errors, label, exc)
                    stage_stats[label] = {
                        "status": "failed",
                        "error": errors[-1] if errors else None,
                    }
            narration = dict(base_stats.get("ai_narration") or {})
            narration["run_budget"] = run_budget.stats()
            derived["ai_narration"] = narration
        else:
            current_stage = "ai_summaries"
            await _ensure_postprocess_active(run_id)
            position += 1
            async with StageTracker(
                bus,
                run_id,
                project_id,
                "ai_summaries",
                position=position,
            ) as stage:
                skipped = {
                    "status": "completed",
                    "skipped": True,
                    "reason": "deterministic_index_default",
                    "llm_tokens": 0,
                }
                stage_stats["ai_summaries"] = skipped
                stage.set_stats(skipped)
            derived.update(
                {"l1_summaries": 0, "l2_summaries": 0, "l3_summaries": 0}
            )

        current_stage = "summary_freshness"
        await _ensure_postprocess_active(run_id)
        try:
            async with SessionLocal() as session:
                retired = await _supersede_stale_graph_summaries(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    retained_summary_ids=retained_summary_ids,
                )
                await session.commit()
            if retired:
                derived["stale_summaries_closed"] = retired
            stage_stats["summary_freshness"] = {
                "status": "completed",
                "retired": retired,
            }
        except Exception as exc:  # noqa: BLE001
            _append_postprocess_error(errors, "summary_freshness", exc)
            stage_stats["summary_freshness"] = {
                "status": "failed",
                "error": errors[-1] if errors else None,
            }

        current_stage = "inventory"
        await _ensure_postprocess_active(run_id)
        try:
            async with SessionLocal() as session:
                derived.update(await _graph_inventory(session, project_id))
            stage_stats["inventory"] = {"status": "completed"}
        except Exception as exc:  # noqa: BLE001
            _append_postprocess_error(errors, "inventory", exc)
            stage_stats["inventory"] = {
                "status": "failed",
                "error": errors[-1] if errors else None,
            }

        derived["postprocess"] = {
            "findings_freshness": findings_freshness,
            "stages": stage_stats,
        }
        return await _close_published_run(
            bus,
            project_id=project_id,
            run_id=run_id,
            final_status="partial" if errors else "completed",
            derived_stats=derived,
            postprocess_errors=errors,
        )
    except AnalysisCancelled as exc:
        derived["postprocess"] = {
            "findings_freshness": findings_freshness,
            "stages": stage_stats,
        }
        error = _postprocess_error(current_stage, exc, cancelled=True)
        return await _close_published_run(
            bus,
            project_id=project_id,
            run_id=run_id,
            final_status="partial",
            derived_stats=derived,
            postprocess_errors=[*errors, error],
        )
    except asyncio.CancelledError as exc:
        derived["postprocess"] = {
            "findings_freshness": findings_freshness,
            "stages": stage_stats,
        }
        error = _postprocess_error(current_stage, exc, cancelled=True)
        close_task = asyncio.create_task(
            _close_published_run(
                bus,
                project_id=project_id,
                run_id=run_id,
                final_status="partial",
                derived_stats=derived,
                postprocess_errors=[*errors, error],
            )
        )
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            await close_task
        raise


async def run_ingest(
    ctx: dict,
    project_id_str: str,
    run_id_str: str,
    path: str,
    options: dict[str, Any] | None = None,
) -> None:
    project_id = uuid.UUID(project_id_str)
    run_id = uuid.UUID(run_id_str)
    bus: ProgressBus = ctx["progress"]
    now = datetime.now(tz=timezone.utc)
    opts = dict(options or {})
    # Match the API/UI contract for legacy or directly-enqueued jobs. With no
    # completed manifest, incremental still selects every producer; after the
    # first baseline it avoids an accidental full repository reconciliation.
    scope = opts.get("scope", "incremental")
    requested_scope = scope
    opts["scope"] = scope
    summarize = bool(opts.get("summarize", False))
    l1_limit = int(opts.get("l1_limit", 25))
    l2_limit = int(opts.get("l2_limit", 25))
    l3_limit = int(opts.get("l3_limit", 25))
    # AI file extraction is a separate, explicit opt-in.  It is not part of
    # the deterministic zero-token indexing default.
    agent_limit = int(opts.get("agent_extract_limit", 0))
    run_git_sha = str(opts.get("git_sha") or "")
    source_ref = str(opts.get("ref") or "") or None
    source_mode_error: str | None = None

    async with SessionLocal() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            await _set_run_status(
                session,
                run_id,
                status="failed",
                completed_at=now,
                error_log="analysis_project_not_found",
            )
            await bus.publish(
                run_id,
                {"event": "run_failed", "error": "analysis_project_not_found"},
            )
            return
        project_languages = list(project.languages or [])
        source_languages = [
            language
            for language in project_languages
            if language not in DB_ANALYZER_KINDS
            and (
                language in SOURCE_ANALYZER_LANGUAGES
                or binary_for(language) is None
            )
        ]
        legacy_database_languages = sorted(
            set(project_languages).intersection(DB_ANALYZER_KINDS)
        )
        # Observe ingest lag — gap between AnalysisRun row creation
        # (queued by webhook or `/analyze`) and worker pickup. Spec §2.7
        # cares about end-to-end freshness; this is the single most
        # actionable latency metric.
        run_row = (
            await session.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
        ).scalar_one_or_none()
        if run_row is not None:
            run_git_sha = run_row.git_sha
            if run_row.project_id != project_id:
                source_mode_error = "analysis_run_project_mismatch"
            else:
                webhook_run = (run_row.triggered_by or "").startswith("webhook:")
                if webhook_run and path.strip():
                    source_mode_error = "webhook_source_path_must_be_empty"
                elif not webhook_run and not path.strip():
                    source_mode_error = "manual_source_path_required"
        if source_mode_error is not None and run_row is not None:
            await _set_run_status(
                session,
                run_id,
                status="failed",
                completed_at=now,
                error_log=source_mode_error,
            )
        superseded = False
        if (
            run_row is not None
            and run_row.status == "queued"
            and (run_row.triggered_by or "").startswith("webhook:")
            and run_row.created_at is not None
        ):
            newer = (
                await session.execute(
                    select(AnalysisRun.id)
                    .where(
                        AnalysisRun.project_id == project_id,
                        AnalysisRun.id != run_id,
                        AnalysisRun.triggered_by.like("webhook:%"),
                        AnalysisRun.created_at > run_row.created_at,
                        AnalysisRun.status.in_(
                            {"queued", "running", "published", "completed", "partial"}
                        ),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if newer is not None:
                superseded = await _set_run_status(
                    session,
                    run_id,
                    status="cancelled",
                    completed_at=now,
                    error_log=f"superseded_by_newer_webhook:{newer}",
                )
        eligible_to_start = (
            run_row is not None
            and run_row.status in {"queued", "published"}
            and not superseded
            and source_mode_error is None
        )

    if not eligible_to_start:
        await _publish_current_terminal(bus, run_id)
        return

    try:
        project_lock_acquired = await _acquire_project_lock(project_id, run_id)
        if (
            not project_lock_acquired
            and await _reclaim_terminal_project_lock(project_id)
        ):
            project_lock_acquired = await _acquire_project_lock(project_id, run_id)
    except asyncio.CancelledError:
        # Redis may have applied SET NX before task cancellation reached the
        # client. Compare-and-delete is safe even when this run never owned it.
        with_context = asyncio.shield(_release_project_lock(project_id, run_id))
        try:
            await with_context
        except Exception:  # noqa: BLE001 — the lease still has a hard TTL
            log.exception(
                "analysis uncertain project lock cleanup failed run_id=%s", run_id
            )
        await asyncio.shield(
            _recover_prelock_failure(
                bus,
                project_id=project_id,
                run_id=run_id,
                stage="project_lock_acquire",
                exc=AnalysisCancelled("worker_cancelled_before_project_lock"),
                cancelled=True,
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("analysis project lock unavailable")
        try:
            await _release_project_lock(project_id, run_id)
        except Exception:  # noqa: BLE001 — compare-delete + TTL remain fail-safe
            log.exception(
                "analysis uncertain project lock cleanup failed run_id=%s", run_id
            )
        await _recover_prelock_failure(
            bus,
            project_id=project_id,
            run_id=run_id,
            stage="project_lock_acquire",
            exc=RuntimeError(
                f"analysis_project_lock_unavailable:{type(exc).__name__}"
            ),
        )
        return
    if not project_lock_acquired:
        await _defer_project_lock_retry(
            ctx,
            bus=bus,
            project_id=project_id,
            run_id=run_id,
            path=path,
            options=opts,
        )
        return

    totals: dict[str, int] = {
        "symbols": 0,
        "edges": 0,
        "contracts": 0,
        "data_entities": 0,
        "errors": 0,
    }
    position = 0
    prepared_source: PreparedSource | None = None
    source_manifest: SourceManifest | None = None
    previous_stats: dict[str, Any] | None = None
    selected_analyzers: set[str] = set()
    removed_analyzers: set[str] = set()
    sweep_sources: set[str] = set()
    producer_outcomes: dict[str, list[AnalyzerStageOutcome]] = {}
    producer_coverage: dict[str, dict[str, Any]] = {}
    inherited_source_snapshot: dict[str, Any] | None = None
    deletion_sweep_deferred_reason: str | None = None
    llm_run_budget: LLMRunBudget | None = None
    refreshed_agent_sources: set[str] = set()
    retired_agent_sources: set[str] = set()
    link_calls_refreshed = False
    source_graph_refreshed = False
    retained_summary_ids: set[uuid.UUID] = set()
    source_published = False
    initial_graph_head_state: str | None = None
    live_schema_sources: set[str] = set()

    # PR-200 — fresh-graph write fast path. When the project's current graph is
    # empty, the supersede-UPDATE in every upsert matches zero rows; skipping it
    # roughly halves the write statements on a large first pass (measured: the
    # cpp calls stage was ~93% DB-ingest, docs/04-eval/throughput-analysis-
    # 2026-07-08.md). Guarded per-run by seen-sets so an id emitted twice (e.g.
    # a contract node both Java and HTML produce) still supersedes correctly.
    fresh_graph = False
    seen_index: SeenFactIndex | None = None
    # Node and edge namespaces share one spill threshold and temporary DB.
    seen_nodes: SeenFactIndex | None = None
    seen_edges: SeenFactIndex | None = None
    try:
        started_at = datetime.now(tz=timezone.utc)
        async with SessionLocal() as session:
            started = await _set_run_status(
                session, run_id, status="running", started_at=started_at
            )
        if not started:
            if await _run_status(run_id) == "published":
                source_published = True
                await _run_published_postprocess(
                    bus,
                    project_id=project_id,
                    run_id=run_id,
                    summarize=summarize,
                    l1_limit=l1_limit,
                    l2_limit=l2_limit,
                    l3_limit=l3_limit,
                    position=position,
                )
                return
            await _publish_current_terminal(bus, run_id)
            return
        if scope != "continuation":
            # Capture exactly one publication generation before the first
            # stage write.  Missing heads are bootstrapped fail-closed; a
            # legacy or new graph then receives one complete staged rebuild.
            async with SessionLocal() as session:
                head = await bootstrap_graph_head(session, project_id=project_id)
                initial_graph_head_state = head.state
                persisted_run = await session.get(AnalysisRun, run_id)
                if persisted_run is None or persisted_run.project_id != project_id:
                    raise GraphPublicationInvariantError(
                        "AnalysisRun does not belong to the graph project"
                    )
                # Queue payload and persisted row normally agree. Direct/local
                # enqueuers are normalized here before base capture so retry
                # and promotion consume one durable scope decision.
                persisted_run.scope = scope
                if head.state == GRAPH_HEAD_NEEDS_REBUILD and scope == "incremental":
                    # The API's incremental default still selects every
                    # producer when no trusted baseline exists. Persist the
                    # effective scope so promotion and provenance tell the
                    # truth about the mandatory repair.
                    scope = "full"
                    persisted_run.scope = "full"
                await capture_graph_base_generation(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                )
                await session.commit()
        else:
            async with SessionLocal() as session:
                head = await session.get(GraphHead, project_id)
                if head is None or head.state != GRAPH_HEAD_READY:
                    raise RuntimeError(
                        "continuation_requires_published_graph: run a full source "
                        "analysis before optional narration"
                    )
        if run_row is not None and run_row.created_at is not None:
            from app.obs.metrics import ingest_lag_seconds

            created_aware = run_row.created_at
            if created_aware.tzinfo is None:
                created_aware = created_aware.replace(tzinfo=timezone.utc)
            lag = (started_at - created_aware).total_seconds()
            source = (
                "webhook"
                if (run_row.triggered_by or "").startswith("webhook:")
                else "api"
            )
            ingest_lag_seconds.labels(source=source).observe(max(0.0, lag))

        # This may allocate a temporary SQLite spill index.  It belongs inside
        # the lock-owning try/finally: disk/open failures must terminalize the
        # run and release the project lease instead of wedging refreshes for
        # the nine-hour lock TTL.
        seen_index = SeenFactIndex(memory_limit=configured_memory_limit())
        seen_nodes = seen_index
        seen_edges = seen_index
        await bus.publish(
            run_id, {"event": "run_started", "at": started_at.isoformat()}
        )
        await _ensure_run_active(run_id)
        if scope == "continuation":
            previous_stats = await _current_published_stats(project_id, run_id)
            inherited = (previous_stats or {}).get("source_snapshot")
            if isinstance(inherited, dict):
                inherited_source_snapshot = dict(inherited)
                inherited_revision = inherited_source_snapshot.get("revision")
                if isinstance(inherited_revision, str):
                    run_git_sha = inherited_revision
                    async with SessionLocal() as session:
                        await session.execute(
                            update(AnalysisRun)
                            .where(AnalysisRun.id == run_id)
                            .values(git_sha=run_git_sha)
                        )
                        await session.commit()
        if scope != "continuation":
            async with SessionLocal() as session:
                fresh_graph = await _graph_is_empty(session, project_id)
        if scope != "continuation":
            prepared_source = await prepare_source(
                path,
                project_id=project_id,
                run_id=run_id,
                git_sha=run_git_sha,
                source_ref=source_ref,
            )
            path = str(prepared_source.path)
            if prepared_source.revision:
                run_git_sha = prepared_source.revision
                async with SessionLocal() as session:
                    await session.execute(
                        update(AnalysisRun)
                        .where(AnalysisRun.id == run_id)
                        .values(git_sha=run_git_sha)
                    )
                    await session.commit()
            manifest_git_options = _git_manifest_options(prepared_source)
            source_manifest = await asyncio.to_thread(
                build_source_manifest,
                path,
                source_languages,
                producer_options={"agent_extract_limit": agent_limit},
                **manifest_git_options,
            )
            previous_stats = await _current_published_stats(project_id, run_id)
            previous_provenance = (previous_stats or {}).get("source_provenance")
            if isinstance(previous_provenance, dict):
                prior_root = previous_provenance.get("root_id")
                if prior_root and prior_root != prepared_source.root_id:
                    raise RuntimeError(
                        "source_root_mismatch: this project is already bound "
                        "to a different source root"
                    )
                prior_ref = previous_provenance.get("ref")
                current_ref = prepared_source.source_ref or source_ref
                if prior_ref and current_ref and prior_ref != current_ref:
                    raise RuntimeError(
                        "source_ref_mismatch: branch-scoped graphs are not supported"
                    )
                prior_prefix = previous_provenance.get("tree_prefix")
                if (
                    prior_prefix is not None
                    and prior_prefix != prepared_source.tree_prefix
                ):
                    raise RuntimeError(
                        "source_tree_prefix_mismatch: project analysis root changed"
                    )
                prior_kind = previous_provenance.get("kind")
                if prior_kind and prior_kind != prepared_source.source_kind:
                    raise RuntimeError(
                        "source_kind_mismatch: project source mode changed"
                    )
            changed, removed_analyzers = changed_analyzers(
                previous_stats, source_manifest
            )
            current_binaries = {
                binary
                for language in source_languages
                if (binary := binary_for(language)) is not None
            }
            available_binaries = {
                binary_for(language)
                for language in source_languages
                if analyzer_available(language)
            }
            available_binaries.discard(None)
            if scope == "incremental":
                selected_analyzers = changed
                sweep_sources = (
                    selected_analyzers.intersection(available_binaries)
                    | removed_analyzers
                ).intersection(deterministic_source_names())
            else:
                selected_analyzers = set(source_manifest.fingerprints)
                # A full run is authoritative for every registered static
                # analyzer that actually ran, plus languages removed from
                # project coverage.  An unavailable current binary retains its
                # last known facts instead of turning "not installed" into
                # "source was deleted".
                sweep_sources = set(current_binaries) | removed_analyzers
            source_graph_refreshed = (
                scope != "incremental"
                or bool(selected_analyzers or removed_analyzers)
            )
            if not prepared_source.authoritative_root:
                # A subtree run can add/update its facts but cannot infer that
                # facts elsewhere in the repository were deleted.
                sweep_sources.clear()

        if scope != "continuation":
            # Stages L0: per-language, per-verb extraction. Two languages can
            # map to the same analyzer binary (typescript+javascript → ggoss-ts
            # walks both file sets); running it twice over the same tree only
            # duplicates work, so the second language records a skipped stage
            # with the covering language as the reason (coverage_report shows
            # this instead of a misleading ``no_analyzer``).
            seen_binaries: dict[str, str] = {}
            for language in source_languages:
                binary = binary_for(language)
                covered_by = seen_binaries.get(binary) if binary else None
                if binary and covered_by is None:
                    seen_binaries[binary] = language
                for verb in ("symbols", "contracts", "calls", "data_access"):
                    position += 1
                    if covered_by is not None:
                        async with StageTracker(
                            bus, run_id, project_id, f"{verb}:{language}",
                            language=language, position=position,
                        ) as stage:
                            stage.set_stats({
                                "skipped": True,
                                "reason": f"covered_by:{covered_by}",
                            })
                        continue
                    if (
                        scope == "incremental"
                        and binary is not None
                        and binary not in selected_analyzers
                    ):
                        async with StageTracker(
                            bus, run_id, project_id, f"{verb}:{language}",
                            language=language, position=position,
                        ) as stage:
                            stage.set_stats({
                                "skipped": True,
                                "reason": "unchanged_source_content",
                            })
                        continue
                    outcome = await _run_analyzer_stage(
                        bus, project_id, run_id, language, verb, path, position,
                        totals, fresh=fresh_graph, seen_nodes=seen_nodes,
                        seen_edges=seen_edges,
                    )
                    if outcome.producer is not None:
                        producer_outcomes.setdefault(outcome.producer, []).append(
                            outcome
                        )

            # Stage L0-Agent: delegate extraction to the operator's Claude
            # Code subscription (spec principle #4) for any language the
            # deterministic analyzers can't handle — either no ggoss analyzer
            # is registered (C++) OR one is registered but its binary isn't
            # installed (the docker-free case, PR-144). Without the latter,
            # a docker-free Python/C#/TS project extracted nothing and Q&A
            # had an empty graph. Bounded by ``agent_extract_limit``.
            ran_agent = False
            if agent_limit > 0 and (
                scope != "incremental" or bool(selected_analyzers or removed_analyzers)
            ):
                if llm_run_budget is None:
                    # Agent extraction and L1-L3 narration spend from one
                    # provider-independent budget.  Enabling both features
                    # must not multiply the run's call/input/time ceilings.
                    llm_run_budget = LLMRunBudget.from_env()
                for language in source_languages:
                    if not analyzer_available(language):
                        agent_key = f"agent:{language}"
                        manifest_key = (
                            agent_key
                            if source_manifest is not None
                            and agent_key in source_manifest.fingerprints
                            else binary_for(language) or agent_key
                        )
                        if (
                            scope == "incremental"
                            and manifest_key not in selected_analyzers
                        ):
                            position += 1
                            async with StageTracker(
                                bus,
                                run_id,
                                project_id,
                                f"agent_extract:{language}",
                                language=language,
                                position=position,
                            ) as stage:
                                stage.set_stats({
                                    "skipped": True,
                                    "reason": "unchanged_source_content",
                                })
                            continue
                        position += 1
                        agent_outcome = await _run_agent_extraction_stage(
                            bus, project_id, run_id, language, path,
                            position, totals, agent_limit,
                            run_budget=llm_run_budget,
                            seen_nodes=seen_nodes,
                            seen_edges=seen_edges,
                        )
                        if agent_outcome.producer is not None:
                            producer_outcomes.setdefault(
                                manifest_key, []
                            ).append(agent_outcome)
                        ran_agent = True
                refreshed_agent_sources.update(
                    _authoritative_agent_refresh_sources(
                        source_manifest,
                        producer_outcomes,
                    )
                )
            prior_coverage_for_link = (
                (previous_stats or {}).get("producer_coverage") or {}
            )
            retiring_keys = set(removed_analyzers)
            retiring_keys.update(
                producer
                for producer, outcomes in producer_outcomes.items()
                if isinstance(prior_coverage_for_link.get(producer), dict)
                and prior_coverage_for_link[producer].get("mode")
                == "agent_fallback"
                and any(
                    outcome.authoritative
                    and not str(outcome.producer or "").startswith("agent:")
                    for outcome in outcomes
                )
            )
            for producer in retiring_keys:
                prior = prior_coverage_for_link.get(producer)
                actual = (
                    prior.get("actual_producers")
                    if isinstance(prior, dict)
                    else None
                )
                if isinstance(actual, list):
                    retired_agent_sources.update(
                        value
                        for value in actual
                        if isinstance(value, str) and value.startswith("agent:")
                    )

            # Stage L0-Link: resolve Agent-extracted cross-file calls. It also
            # reruns when an Agent producer is retired, so stale derived edges
            # are omitted from ``seen_edges`` and closed by the later sweep.
            if ran_agent or retired_agent_sources:
                position += 1
                await _run_call_linking_stage(
                    bus,
                    project_id,
                    run_id,
                    position,
                    totals,
                    seen_nodes=seen_nodes,
                    seen_edges=seen_edges,
                    refreshed_agent_sources=(
                        refreshed_agent_sources | retired_agent_sources
                    ),
                )
                link_calls_refreshed = True

            if prepared_source.mutable and source_manifest is not None:
                final_manifest = await asyncio.to_thread(
                    build_source_manifest,
                    path,
                    source_languages,
                    producer_options={"agent_extract_limit": agent_limit},
                )
                if final_manifest.as_dict() != source_manifest.as_dict():
                    raise RuntimeError(
                        "source_changed_during_analysis: mutable source cannot "
                        "publish or delete facts"
                    )

            # A content fingerprint authorizes a future skip only when this
            # run actually achieved complete producer coverage.  Likewise,
            # deletion/rename sweep authority is granted per producer only
            # after all four deterministic verbs completed without stderr,
            # malformed JSON, timeout, or a missing executable.
            required_verbs = set(_VERB_ACCEPT).difference({"live_schema"})
            for producer in (source_manifest.fingerprints if source_manifest else {}):
                if scope == "incremental" and producer not in selected_analyzers:
                    previous_coverage = (
                        (previous_stats or {}).get("producer_coverage") or {}
                    ).get(producer)
                    if (
                        isinstance(previous_coverage, dict)
                        and previous_coverage.get("authoritative") is True
                    ):
                        producer_coverage[producer] = {
                            **previous_coverage,
                            "reused_from_previous": True,
                        }
                        continue
                outcomes = producer_outcomes.get(producer, [])
                fallback_outcomes = [
                    outcome for outcome in outcomes if outcome.verb == "extract"
                ]
                if producer.startswith("agent:") or fallback_outcomes:
                    effective = fallback_outcomes or outcomes
                    usable = len(effective) == 1 and effective[0].records > 0
                    authoritative = (
                        len(effective) == 1
                        and effective[0].authoritative
                        and source_manifest is not None
                        and effective[0].records
                        >= source_manifest.file_counts.get(producer, 0)
                    )
                    completed_verbs = sorted(
                        outcome.verb for outcome in effective if outcome.authoritative
                    )
                    coverage_mode = "agent_fallback"
                else:
                    completed_verbs = sorted(
                        {outcome.verb for outcome in outcomes if outcome.authoritative}
                    )
                    authoritative = set(completed_verbs) == required_verbs
                    usable = authoritative
                    coverage_mode = "deterministic"
                producer_coverage[producer] = {
                    "authoritative": authoritative,
                    "usable": usable,
                    "mode": coverage_mode,
                    "actual_producers": sorted({
                        outcome.producer
                        for outcome in outcomes
                        if outcome.producer is not None
                    }),
                    "completed_verbs": completed_verbs,
                    "file_count": (
                        source_manifest.file_counts.get(producer, 0)
                        if source_manifest is not None
                        else 0
                    ),
                    "errors": [
                        error
                        for outcome in outcomes
                        for error in outcome.errors
                    ][:16],
                }

            authoritative_sources = {
                producer
                for producer, coverage in producer_coverage.items()
                if coverage.get("authoritative") is True
                and coverage.get("mode") == "deterministic"
                and coverage.get("reused_from_previous") is not True
                and not producer.startswith("agent:")
            }
            previous_manifest = (
                (previous_stats or {}).get("source_manifest")
                if isinstance((previous_stats or {}).get("source_manifest"), dict)
                else {}
            )
            previous_fingerprints = previous_manifest.get("fingerprints") or {}
            previous_deterministic = set(previous_fingerprints).intersection(
                deterministic_source_names()
            )
            current_deterministic = set(
                source_manifest.fingerprints if source_manifest else {}
            ).intersection(deterministic_source_names())
            sweep_universe = previous_deterministic | current_deterministic
            refreshed_or_removed = (
                authoritative_sources
                | removed_analyzers.intersection(deterministic_source_names())
            )
            # created_by currently stores only the most recent producer, not a
            # durable contribution set. A per-family sweep can therefore
            # delete a shared normalized Contract still emitted by an
            # unchanged family. Until contribution tables exist, delete only
            # when every previous/current deterministic family was refreshed
            # authoritatively (or explicitly removed) in this run.
            if prepared_source is None or not prepared_source.authoritative_root:
                sweep_sources = set()
                deletion_sweep_deferred_reason = "source_subtree_not_authoritative"
            elif not sweep_universe.issubset(refreshed_or_removed):
                sweep_sources = set()
                deletion_sweep_deferred_reason = (
                    "all_deterministic_producers_must_refresh_for_safe_ownership"
                )
            else:
                sweep_sources = sweep_universe

            # Agent facts use isolated ``agent:<language>`` ownership, so a
            # complete provider pass can safely close that producer's omitted
            # inferred rows without waiting for every deterministic family.
            # When a manifest producer disappears, recover its actual Agent
            # source name from the prior coverage receipt. ``link_calls`` is
            # a full recomputation over the current symbol set whenever its
            # stage succeeds, and therefore owns its own omission sweep.
            previous_coverage_map = (
                (previous_stats or {}).get("producer_coverage") or {}
            )
            removed_agent_sources: set[str] = set(retired_agent_sources)
            retired_agent_producers = set(removed_analyzers)
            retired_agent_producers.update(
                producer
                for producer, coverage in producer_coverage.items()
                if coverage.get("authoritative") is True
                and coverage.get("mode") == "deterministic"
                and isinstance(previous_coverage_map.get(producer), dict)
                and previous_coverage_map[producer].get("mode")
                == "agent_fallback"
            )
            for producer in retired_agent_producers:
                prior = previous_coverage_map.get(producer)
                if (
                    not isinstance(prior, dict)
                    or prior.get("mode") != "agent_fallback"
                ):
                    continue
                actual = prior.get("actual_producers")
                if isinstance(actual, list):
                    removed_agent_sources.update(
                        str(value)
                        for value in actual
                        if isinstance(value, str) and value.startswith("agent:")
                    )
            agent_sweep_sources = (
                refreshed_agent_sources | removed_agent_sources
            )
            if link_calls_refreshed:
                agent_sweep_sources.add("link_calls")
            if prepared_source is None or not prepared_source.authoritative_root:
                agent_sweep_sources = set()
            sweep_sources.update(agent_sweep_sources)
            requested_producers = (
                set(selected_analyzers)
                if scope == "incremental"
                else set(source_manifest.fingerprints if source_manifest else {})
            )
            relevant_requested = {
                producer
                for producer in requested_producers
                if source_manifest is not None
                and source_manifest.file_counts.get(producer, 0) > 0
            }
            successful_requested = {
                producer
                for producer in relevant_requested
                if producer_coverage.get(producer, {}).get("usable") is True
            }
            if relevant_requested and not successful_requested:
                unavailable = ",".join(sorted(relevant_requested))
                raise RuntimeError(
                    "no_usable_source_analyzer: no changed source producer "
                    f"completed authoritatively ({unavailable})"
                )

            # Stage L0-DB: live database schema for every registered
            # ProjectDB. Skipped silently when no DBs are bound to the
            # project so single-language projects still work.
            # Incremental is the API/UI default, including the first run. A
            # missing baseline selects every source producer and must retain
            # the old first-run behaviour of probing configured live schemas.
            # Later incrementals leave live DB state alone unless the operator
            # explicitly requests a full reconciliation.
            if scope == "full" or previous_stats is None:
                position = await _run_db_live_schema_stages(
                    bus,
                    project_id,
                    run_id,
                    position,
                    totals,
                    authoritative_sources=live_schema_sources,
                )
                sweep_sources.update(live_schema_sources)

            # Freeze deletion authority after every producer transaction has
            # committed, then atomically reconcile candidates, omissions, the
            # graph head, receipt, and AnalysisRun ``published`` state. No
            # analyzer/LLM/network
            # work occurs inside the publication transaction.
            prepublication_stats = _compose_analysis_stats(
                project_id=project_id,
                totals=totals,
                inventory=None,
                inherited_source_snapshot=inherited_source_snapshot,
                source_manifest=source_manifest,
                prepared_source=prepared_source,
                source_ref=source_ref,
                run_git_sha=run_git_sha,
                producer_coverage=producer_coverage,
                legacy_database_languages=legacy_database_languages,
                scope=scope,
                requested_scope=requested_scope,
                selected_analyzers=selected_analyzers,
                removed_analyzers=removed_analyzers,
                sweep_sources=sweep_sources,
                deletion_sweep_deferred_reason=deletion_sweep_deferred_reason,
                previous_stats=previous_stats,
                summarize=summarize,
                agent_limit=agent_limit,
                llm_run_budget=llm_run_budget,
                seen_index=seen_index,
            )
            if (
                initial_graph_head_state == GRAPH_HEAD_NEEDS_REBUILD
                and prepublication_stats.get("coverage", {}).get("status")
                != "complete"
            ):
                raise RuntimeError(
                    "full_rebuild_requires_complete_producer_coverage"
                )
            async with SessionLocal() as session:
                await seal_graph_coverage(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    authoritative_sources=sweep_sources,
                )
                await session.commit()
            async with SessionLocal() as session:
                promotion = await promote_staged_graph(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    allow_full_rebuild=(
                        initial_graph_head_state == GRAPH_HEAD_NEEDS_REBUILD
                    ),
                    final_stats=prepublication_stats,
                )
            source_published = True
            totals["stale_nodes_closed"] = (
                promotion.counts.stale_nodes_closed
            )
            totals["stale_edges_closed"] = (
                promotion.counts.stale_edges_closed
            )
            totals["graph_generation"] = promotion.generation
            await _run_published_postprocess(
                bus,
                project_id=project_id,
                run_id=run_id,
                summarize=summarize,
                l1_limit=l1_limit,
                l2_limit=l2_limit,
                l3_limit=l3_limit,
                position=position,
            )
            return

        # L1-L3 prose is optional narration over the graph, never a condition
        # for a usable source index.  The default path constructs no Extractor
        # at all and therefore has a hard initial LLM-token cost of zero.
        if summarize:
            extractor = Extractor()
            if llm_run_budget is None:
                llm_run_budget = LLMRunBudget.from_env()
            for level, label, fn, lim in (
                (1, "l1_summaries", summarise_l1, l1_limit),
                (2, "l2_summaries", summarise_l2, l2_limit),
                (3, "l3_summaries", summarise_l3, l3_limit),
            ):
                position += 1

                async def _summary_progress() -> None:
                    # Source publication is already final at this point. A
                    # cancellation may stop optional narration but cannot
                    # rewrite the completed graph run or roll the head back.
                    if not source_published:
                        await _ensure_run_active(run_id)
                    await stage.increment()

                async with StageTracker(
                    bus,
                    run_id,
                    project_id,
                    label,
                    position=position,
                    time_budget_sec=1200,
                ) as stage:
                    async with SessionLocal() as session:
                        produced = await fn(
                            session,
                            extractor,
                            project_id=project_id,
                            limit=lim,
                            progress_cb=_summary_progress,
                            analysis_run_id=run_id,
                            run_budget=llm_run_budget,
                            retained_summary_ids=retained_summary_ids,
                        )
                    totals[label] = produced
                    stage.set_stats({
                        label: produced,
                        "limit": lim,
                        "run_budget": llm_run_budget.stats(),
                    })
        else:
            position += 1
            async with StageTracker(
                bus,
                run_id,
                project_id,
                "ai_summaries",
                position=position,
            ) as stage:
                stage.set_stats({
                    "skipped": True,
                    "reason": "deterministic_index_default",
                    "llm_tokens": 0,
                    "enable_with": "summarize=true",
                })
            totals.update({
                "l1_summaries": 0,
                "l2_summaries": 0,
                "l3_summaries": 0,
            })

        completed = datetime.now(tz=timezone.utc)
        if not source_published:
            await _ensure_run_active(run_id)
        async with SessionLocal() as session:
            if source_graph_refreshed:
                retired_summaries = await _supersede_stale_graph_summaries(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    retained_summary_ids=retained_summary_ids,
                )
                if retired_summaries:
                    totals["stale_summaries_closed"] = retired_summaries
            await session.commit()
            inventory = await _graph_inventory(session, project_id)
            final_stats = _compose_analysis_stats(
                project_id=project_id,
                totals=totals,
                inventory=inventory,
                inherited_source_snapshot=inherited_source_snapshot,
                source_manifest=source_manifest,
                prepared_source=prepared_source,
                source_ref=source_ref,
                run_git_sha=run_git_sha,
                producer_coverage=producer_coverage,
                legacy_database_languages=legacy_database_languages,
                scope=scope,
                requested_scope=requested_scope,
                selected_analyzers=selected_analyzers,
                removed_analyzers=removed_analyzers,
                sweep_sources=sweep_sources,
                deletion_sweep_deferred_reason=deletion_sweep_deferred_reason,
                previous_stats=previous_stats,
                summarize=summarize,
                agent_limit=agent_limit,
                llm_run_budget=llm_run_budget,
                seen_index=seen_index,
            )
            # Source-indexing runs return through _run_published_postprocess.
            # This direct running->completed path is only for continuation
            # narration, which does not own or advance GraphHead.
            completed_update = await _set_run_status(
                session,
                run_id,
                status="completed",
                completed_at=completed,
                stats=final_stats,
            )
        if not completed_update:
            terminal_status = await _publish_current_terminal(bus, run_id)
            if terminal_status == "cancelled":
                raise AnalysisCancelled("analysis_cancelled")
            return
        await audit_record(
            actor="system",
            action="analysis.completed",
            target=str(run_id),
            project_id=project_id,
            details=final_stats,
        )
        try:
            from app.obs.metrics import analysis_runs_total

            analysis_runs_total.labels(status="completed").inc()
        except Exception:
            pass
        await bus.publish(run_id, {"event": "run_completed", **final_stats})
    except AnalysisCancelled as exc:
        if source_published:
            if await _run_status(run_id) == "published":
                await _partialize_published_run(
                    bus,
                    project_id=project_id,
                    run_id=run_id,
                    stage="postprocess",
                    exc=exc,
                    cancelled=True,
                )
            else:
                await _publish_current_terminal(bus, run_id)
            return
        async with SessionLocal() as session:
            await _set_run_status(
                session,
                run_id,
                status="cancelled",
                completed_at=datetime.now(tz=timezone.utc),
                stats={**totals, "seen_index": _seen_stats(seen_index)},
            )
        await bus.publish(run_id, {"event": "run_cancelled"})
    except asyncio.CancelledError as exc:
        current_status = await _run_status(run_id)
        if current_status == "published":
            close_task = asyncio.create_task(
                _partialize_published_run(
                    bus,
                    project_id=project_id,
                    run_id=run_id,
                    stage="postprocess",
                    exc=exc,
                    cancelled=True,
                )
            )
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError:
                await close_task
        elif current_status == "cancelled":
            await _publish_current_terminal(bus, run_id)
        elif current_status in {"completed", "partial"}:
            pass
        else:
            async with SessionLocal() as session:
                failed = await _set_run_status(
                    session,
                    run_id,
                    status="failed",
                    completed_at=datetime.now(tz=timezone.utc),
                    error_log="worker_cancelled_or_job_timeout",
                    stats={**totals, "seen_index": _seen_stats(seen_index)},
                )
            if failed:
                await bus.publish(
                    run_id,
                    {"event": "run_failed", "error": "worker_cancelled_or_job_timeout"},
                )
            else:
                await _publish_current_terminal(bus, run_id)
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("run_ingest failed")
        current_status = await _run_status(run_id)
        if source_published or current_status == "published":
            if current_status == "published":
                await _partialize_published_run(
                    bus,
                    project_id=project_id,
                    run_id=run_id,
                    stage="postprocess",
                    exc=exc,
                )
            else:
                await _publish_current_terminal(bus, run_id)
            return
        if current_status in {"completed", "partial"}:
            await _publish_current_terminal(bus, run_id)
            return
        async with SessionLocal() as session:
            failed_update = await _set_run_status(
                session,
                run_id,
                status="failed",
                completed_at=datetime.now(tz=timezone.utc),
                error_log=str(exc),
                stats={**totals, "seen_index": _seen_stats(seen_index)},
            )
        if not failed_update:
            await _publish_current_terminal(bus, run_id)
            return
        try:
            from app.obs.metrics import analysis_runs_total

            analysis_runs_total.labels(status="failed").inc()
        except Exception:
            pass
        await bus.publish(run_id, {"event": "run_failed", "error": str(exc)})
    finally:
        # Failed/cancelled staging is disposable only after the terminal state
        # is durable.  Promotion already removes successful rows; repeating
        # cleanup there is harmless and keeps retry/reaper behavior idempotent.
        try:
            terminal_status = await _run_status(run_id)
            if terminal_status in {
                "published",
                "completed",
                "partial",
                "failed",
                "cancelled",
            }:
                async with SessionLocal() as session:
                    await cleanup_staged_graph(
                        session,
                        project_id=project_id,
                        run_id=run_id,
                    )
                    await session.commit()
        except Exception:  # noqa: BLE001 — cleanup cannot hide run outcome
            log.exception("analysis staging cleanup failed run_id=%s", run_id)
        if seen_index is not None:
            seen_index.close()
        if prepared_source is not None:
            try:
                await prepared_source.cleanup()
            except Exception:  # noqa: BLE001
                log.exception("source worktree cleanup failed for run %s", run_id)
        try:
            await _release_project_lock(project_id, run_id)
        except Exception:  # noqa: BLE001
            log.exception("analysis project lock release failed run_id=%s", run_id)


_HEARTBEAT_KEY = "mnemos:worker:heartbeat"
_HEARTBEAT_INTERVAL_SEC = 15


async def _heartbeat_loop(ctx: dict) -> None:
    """Writes ``now()`` to a redis key every N seconds so the platform's
    ``/health/ready`` endpoint can flag a dead worker.
    """
    from app.orchestrator.redis_pool import get_redis

    redis = await get_redis()
    try:
        while True:
            await redis.set(_HEARTBEAT_KEY, str(time.time()), ex=_HEARTBEAT_INTERVAL_SEC * 4)
            await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
    except asyncio.CancelledError:
        return


async def _startup(ctx: dict) -> None:
    ctx["progress"] = ProgressBus()
    ctx["heartbeat_task"] = asyncio.create_task(_heartbeat_loop(ctx))


async def _shutdown(ctx: dict) -> None:
    task = ctx.get("heartbeat_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class WorkerSettings:
    """Reference configuration for ``arq server.app.orchestrator.jobs.WorkerSettings``.

    ``cron_jobs`` runs the periodic background work introduced in PR-4:
    ``break_glass_expiry`` every 5 minutes, ``probe_recheck`` and
    ``retention_purge`` once a day. Multi-worker deployments stay safe
    because each entry holds a Postgres advisory lock for the duration
    of its run — losers no-op.
    """
    from arq.cron import cron  # imported here so importing this module
    # does not require arq when only the job functions are needed.

    from app.orchestrator.cron_jobs import (
        run_break_glass_expiry,
        run_probe_recheck,
        run_reset_stale_runs,
        run_retention_purge,
    )

    functions = [run_ingest]
    cron_jobs = [
        cron(run_break_glass_expiry, minute=set(range(0, 60, 5))),
        cron(run_probe_recheck, hour={3}, minute={0}),
        cron(run_retention_purge, hour={4}, minute={0}),
        # Every 15 minutes so a wedged run doesn't sit in the GUI
        # for half a day before the operator notices.
        cron(run_reset_stale_runs, minute=set(range(0, 60, 15))),
    ]
    on_startup = _startup
    on_shutdown = _shutdown
    redis_settings = None  # populated at runtime from get_settings().redis_url
    keep_result = 3600
    # Repository analysis is a long, stateful job.  ARQ's defaults (10-way
    # concurrency, 5-minute timeout, five retries, abort disabled) can run the
    # same graph mutation repeatedly and exhaust memory.  Start conservatively;
    # scale with more workers after project-level sharding, not more concurrent
    # mutations inside one worker.
    max_jobs = 1
    job_timeout = 8 * 60 * 60
    max_tries = 1
    retry_jobs = False
    allow_abort_jobs = True
