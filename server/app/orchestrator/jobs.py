"""ARQ job functions — staged execution with per-stage progress tracking.

Every stage is wrapped in :class:`app.orchestrator.stages.StageTracker` so
the GUI can show a pipeline view and each summariser pass can exit in
``status=partial`` when its budget is spent without losing progress.
Design rationale: ``docs/analysis-strategy.md``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.registry import runner_for
from app.audit.logger import record as audit_record
from app.config import get_settings
from app.db import SessionLocal
from app.extractor.agent import Extractor
from app.extractor.runner import summarise_l1, summarise_l2, summarise_l3
from app.merge.contract_id import http_contract_id
from app.merge.findings import run_all as rebuild_findings
from app.merge.writer import upsert_edge, upsert_node
from app.models.graph import AnalysisRun
from app.models.projects import Project
from app.orchestrator.progress import ProgressBus
from app.orchestrator.stages import StageTracker

log = logging.getLogger(__name__)
_settings = get_settings()


async def _set_run_status(
    session: AsyncSession,
    run_id: uuid.UUID,
    *,
    status: str,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    stats: dict[str, Any] | None = None,
    error_log: str | None = None,
) -> None:
    values: dict[str, Any] = {"status": status}
    if started_at is not None:
        values["started_at"] = started_at
    if completed_at is not None:
        values["completed_at"] = completed_at
    if stats is not None:
        values["stats"] = stats
    if error_log is not None:
        values["error_log"] = error_log
    await session.execute(update(AnalysisRun).where(AnalysisRun.id == run_id).values(**values))
    await session.commit()


async def _record_payload(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    payload: dict[str, Any],
    accept_kinds: set[str],
    totals: dict[str, int],
) -> None:
    """Apply one analyzer JSON record if its record_type is in ``accept_kinds``."""
    record_type = payload.get("record_type")
    if record_type not in accept_kinds:
        return
    data = payload.get("data", {}) or {}
    source_name = payload.get("source_name", "unknown")

    if record_type == "symbol":
        node_id = data.get("id")
        if not node_id:
            return
        await upsert_node(
            session,
            project_id=project_id,
            node_id=node_id,
            kind="Symbol",
            data=data,
            certainty=data.get("certainty", "asserted"),
            source_name=source_name,
        )
        totals["symbols"] += 1
    elif record_type == "contract":
        node_id = data.get("id")
        if not node_id:
            return
        spec = data.get("spec") or {}
        if data.get("kind") == "http_endpoint" and spec:
            method = spec.get("method", "GET")
            raw_path = spec.get("path", "/")
            node_id = http_contract_id(method, raw_path)
            data = {**data, "id": node_id}
        await upsert_node(
            session,
            project_id=project_id,
            node_id=node_id,
            kind="Contract",
            data=data,
            certainty=data.get("certainty", "inferred"),
            source_name=source_name,
        )
        totals["contracts"] += 1
    elif record_type == "data_entity":
        node_id = data.get("id")
        if not node_id:
            return
        await upsert_node(
            session,
            project_id=project_id,
            node_id=node_id,
            kind="DataEntity",
            data=data,
            certainty=data.get("certainty", "verified"),
            source_name=source_name,
        )
        totals["data_entities"] = totals.get("data_entities", 0) + 1
    elif record_type == "edge":
        src = data.get("source_id")
        tgt = data.get("target_id")
        kind = data.get("kind", "CALLS")
        if not src or not tgt:
            return
        if tgt.startswith("http.") and tgt.count(".") >= 2:
            method, _, rest = tgt.removeprefix("http.").partition(".")
            tgt = http_contract_id(method, rest)
        await upsert_edge(
            session,
            project_id=project_id,
            source_id=src,
            target_id=tgt,
            kind=kind,
            data=data.get("metadata", {}) or {},
            certainty=data.get("certainty", "asserted"),
            source_name=source_name,
        )
        totals["edges"] += 1


_VERB_ACCEPT = {
    "symbols": {"symbol", "data_entity"},
    "contracts": {"contract", "edge"},
    "calls": {"edge"},
    "data_access": {"edge"},
    "live_schema": {"data_entity"},
}


async def _run_analyzer_stage(
    bus: ProgressBus,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    language: str,
    verb: str,
    path: str,
    position: int,
    totals: dict[str, int],
) -> None:
    """One analyser verb (e.g. 'symbols' for 'csharp') as a tracked stage."""
    runner = runner_for(language)
    if runner is None:
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
        return

    async with StageTracker(
        bus,
        run_id,
        project_id,
        f"{verb}:{language}",
        language=language,
        position=position,
        time_budget_sec=1800,
    ) as stage:
        accept = _VERB_ACCEPT.get(verb, set())
        async with SessionLocal() as session:
            async for rec in runner.run(verb, path):
                if rec.stream == "stderr":
                    totals["errors"] += 1
                    continue
                before = sum(totals.values())
                await _record_payload(
                    session,
                    project_id=project_id,
                    payload=rec.payload,
                    accept_kinds=accept,
                    totals=totals,
                )
                after = sum(totals.values())
                if after > before:
                    await stage.increment(after - before)
            await session.commit()
        stage.set_stats({k: totals.get(k, 0) for k in ("symbols", "edges", "contracts", "data_entities")})


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
    opts = options or {}
    scope = opts.get("scope", "full")
    l1_limit = int(opts.get("l1_limit", 25))
    l2_limit = int(opts.get("l2_limit", 25))
    l3_limit = int(opts.get("l3_limit", 25))

    async with SessionLocal() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            return
        await _set_run_status(session, run_id, status="running", started_at=now)

    await bus.publish(run_id, {"event": "run_started", "at": now.isoformat()})
    totals: dict[str, int] = {
        "symbols": 0,
        "edges": 0,
        "contracts": 0,
        "data_entities": 0,
        "errors": 0,
    }
    position = 0

    try:
        if scope != "continuation":
            # Stages L0: per-language, per-verb extraction.
            for language in project.languages:
                for verb in ("symbols", "contracts", "calls", "data_access"):
                    position += 1
                    await _run_analyzer_stage(
                        bus, project_id, run_id, language, verb, path, position, totals
                    )

            # Stage: Findings reconciliation.
            position += 1
            async with StageTracker(
                bus,
                run_id,
                project_id,
                "findings",
                position=position,
                time_budget_sec=600,
            ) as stage:
                async with SessionLocal() as session:
                    finding_stats = await rebuild_findings(session, project_id)
                totals["findings"] = sum(finding_stats.values())
                for _ in range(totals["findings"]):
                    await stage.increment()
                stage.set_stats(finding_stats)

        # Stages L1-L3: hierarchical summarisation. Each summariser enforces
        # its own LLM budget; StageTracker adds a wall-clock ceiling.
        extractor = Extractor()

        for level, label, fn, lim in (
            (1, "l1_summaries", summarise_l1, l1_limit),
            (2, "l2_summaries", summarise_l2, l2_limit),
            (3, "l3_summaries", summarise_l3, l3_limit),
        ):
            position += 1
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
                        progress_cb=stage.increment,
                    )
                totals[label] = produced
                stage.set_stats({label: produced, "limit": lim})

        completed = datetime.now(tz=timezone.utc)
        async with SessionLocal() as session:
            await _set_run_status(
                session,
                run_id,
                status="completed",
                completed_at=completed,
                stats=totals,
            )
        await audit_record(
            actor="system",
            action="analysis.completed",
            target=str(run_id),
            project_id=project_id,
            details=totals,
        )
        await bus.publish(run_id, {"event": "run_completed", **totals})
    except Exception as exc:  # noqa: BLE001
        log.exception("run_ingest failed")
        async with SessionLocal() as session:
            await _set_run_status(
                session,
                run_id,
                status="failed",
                completed_at=datetime.now(tz=timezone.utc),
                error_log=str(exc),
                stats=totals,
            )
        await bus.publish(run_id, {"event": "run_failed", "error": str(exc)})


async def _startup(ctx: dict) -> None:
    ctx["progress"] = ProgressBus()


async def _shutdown(ctx: dict) -> None:
    pass


class WorkerSettings:
    """Reference configuration for ``arq server.app.orchestrator.jobs.WorkerSettings``."""

    functions = [run_ingest]
    on_startup = _startup
    on_shutdown = _shutdown
    redis_settings = None  # populated at runtime from get_settings().redis_url
    keep_result = 3600
