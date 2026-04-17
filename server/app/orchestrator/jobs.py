"""ARQ job functions.

Only the Week-2 subset is implemented: ``run_ingest`` bootstraps a run and
fans out to ``run_analyzer`` per language. Additional jobs (merge, summarise,
runtime correlator, etc.) land in later weeks per spec §13.3.
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
from app.extractor.runner import summarise_l1
from app.merge.contract_id import http_contract_id
from app.merge.findings import run_all as rebuild_findings
from app.merge.writer import upsert_edge, upsert_node
from app.models.graph import AnalysisRun
from app.models.projects import Project
from app.orchestrator.progress import ProgressBus

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


async def run_ingest(ctx: dict, project_id_str: str, run_id_str: str, path: str) -> None:
    """Drive a single analysis run end-to-end.

    Week 2 scope: spawn per-language analyzer, stream symbols into the graph.
    Merge/summarise stages are no-ops here and wired up in Weeks 3 & 6.
    """
    project_id = uuid.UUID(project_id_str)
    run_id = uuid.UUID(run_id_str)
    bus: ProgressBus = ctx["progress"]
    now = datetime.now(tz=timezone.utc)

    async with SessionLocal() as session:
        project = (
            await session.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            return
        await _set_run_status(session, run_id, status="running", started_at=now)

    await bus.publish(run_id, {"phase": "started", "at": now.isoformat()})
    totals = {"symbols": 0, "edges": 0, "contracts": 0, "errors": 0}

    try:
        for language in project.languages:
            runner = runner_for(language)
            if runner is None:
                await bus.publish(
                    run_id,
                    {"phase": "skipped", "language": language, "reason": "no_analyzer"},
                )
                continue
            await bus.publish(run_id, {"phase": "language_start", "language": language})

            async with SessionLocal() as session:
                for verb in ("symbols", "contracts", "calls"):
                    async for rec in runner.run(verb, path):
                        if rec.stream == "stderr":
                            totals["errors"] += 1
                            await bus.publish(run_id, {"phase": "analyzer_error", **rec.payload})
                            continue
                        payload = rec.payload
                        data = payload.get("data", {}) or {}
                        source_name = payload.get("source_name", "unknown")
                        match payload.get("record_type"):
                            case "symbol":
                                node_id = data.get("id")
                                if not node_id:
                                    continue
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
                            case "contract":
                                node_id = data.get("id")
                                if not node_id:
                                    continue
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
                            case "data_entity":
                                node_id = data.get("id")
                                if not node_id:
                                    continue
                                await upsert_node(
                                    session,
                                    project_id=project_id,
                                    node_id=node_id,
                                    kind="DataEntity",
                                    data=data,
                                    certainty=data.get("certainty", "verified"),
                                    source_name=source_name,
                                )
                            case "edge":
                                src = data.get("source_id")
                                tgt = data.get("target_id")
                                kind = data.get("kind", "CALLS")
                                if not src or not tgt:
                                    continue
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
                        if sum(totals.values()) % 200 == 0:
                            await session.commit()
                            await bus.publish(run_id, {"phase": "progress", **totals})
                await session.commit()

            await bus.publish(
                run_id,
                {"phase": "language_done", "language": language, **totals},
            )

        # Week-6 post-ingest stages: findings, L1 summaries.
        async with SessionLocal() as session:
            finding_stats = await rebuild_findings(session, project_id)
        totals["findings"] = sum(finding_stats.values())
        await bus.publish(run_id, {"phase": "findings", **finding_stats})

        extractor = Extractor()
        async with SessionLocal() as session:
            summarised = await summarise_l1(
                session, extractor, project_id=project_id, limit=25
            )
        totals["l1_summaries"] = summarised
        await bus.publish(run_id, {"phase": "l1_summaries", "count": summarised})

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
        await bus.publish(run_id, {"phase": "completed", **totals})
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
        await bus.publish(run_id, {"phase": "failed", "error": str(exc)})


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
