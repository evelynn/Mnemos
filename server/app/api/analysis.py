import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.db import get_session
from app.models.graph import AnalysisRun, Edge, Node
from app.models.projects import Project
from app.models.stages import AnalysisStage
from app.orchestrator.progress import ProgressBus
from app.orchestrator.queue import get_queue

router = APIRouter(prefix="/api/v1", tags=["analysis"])


class AnalysisTriggerRequest(BaseModel):
    git_sha: str = Field(default="HEAD")
    scope: str = Field(default="full", pattern="^(full|incremental|continuation)$")
    source_path: str = Field(description="Absolute path visible to the worker")
    l1_limit: int = Field(default=25, ge=1, le=1000)
    l2_limit: int = Field(default=25, ge=1, le=1000)
    l3_limit: int = Field(default=25, ge=1, le=1000)


class AnalysisRunOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    triggered_by: str
    git_sha: str
    scope: str
    started_at: datetime | None
    completed_at: datetime | None
    stats: dict[str, Any] | None
    created_at: datetime


def _to_out(r: AnalysisRun) -> AnalysisRunOut:
    return AnalysisRunOut(
        id=r.id,
        project_id=r.project_id,
        status=r.status,
        triggered_by=r.triggered_by,
        git_sha=r.git_sha,
        scope=r.scope,
        started_at=r.started_at,
        completed_at=r.completed_at,
        stats=r.stats,
        created_at=r.created_at,
    )


@router.post(
    "/projects/{project_id}/analyze",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_project_org())],
)
async def trigger_analysis(
    project_id: uuid.UUID,
    body: AnalysisTriggerRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AnalysisRunOut:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")

    run = AnalysisRun(
        project_id=project_id,
        status="queued",
        triggered_by=f"user:{user.id}",
        git_sha=body.git_sha,
        scope=body.scope,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)

    queue = await get_queue()
    await queue.enqueue_job(
        "run_ingest",
        str(project_id),
        str(run.id),
        body.source_path,
        {
            "scope": body.scope,
            "l1_limit": body.l1_limit,
            "l2_limit": body.l2_limit,
            "l3_limit": body.l3_limit,
        },
    )
    await audit_record(
        actor=f"user:{user.id}",
        action="analysis.enqueue",
        target=str(run.id),
        project_id=project_id,
        details={"git_sha": body.git_sha, "scope": body.scope},
    )
    return _to_out(run)


@router.get("/analysis_runs/{run_id}")
async def get_run(
    run_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> AnalysisRunOut:
    run = (
        await db.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _to_out(run)


@router.get(
    "/projects/{project_id}/analysis_runs",
    dependencies=[Depends(require_project_org())],
)
async def list_runs(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[AnalysisRunOut]:
    rows = (
        await db.execute(
            select(AnalysisRun)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return [_to_out(r) for r in rows]


@router.get("/analysis_runs/{run_id}/events")
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    _: CurrentUser,
) -> StreamingResponse:
    bus = ProgressBus()

    async def _sse() -> Any:
        yield f"event: open\ndata: {json.dumps({'run_id': str(run_id)})}\n\n"
        async for event in bus.subscribe(run_id):
            if await request.is_disconnected():
                break
            yield f"event: progress\ndata: {json.dumps(event)}\n\n"
            if event.get("phase") in {"completed", "failed"}:
                break

    return StreamingResponse(_sse(), media_type="text/event-stream")


@router.post("/analysis_runs/{run_id}/cancel")
async def cancel_run(
    run_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    run = (
        await db.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=404, detail="not_found")
    if run.status in {"completed", "failed", "cancelled"}:
        return {"status": run.status}
    run.status = "cancelled"
    run.completed_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="analysis.cancel",
        target=str(run_id),
        project_id=run.project_id,
    )
    return {"status": "cancelled"}


@router.get("/analysis_runs/{run_id}/stages")
async def get_run_stages(
    run_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(AnalysisStage)
            .where(AnalysisStage.run_id == run_id)
            .order_by(AnalysisStage.position, AnalysisStage.created_at)
        )
    ).scalars().all()
    return [
        {
            "id": str(s.id),
            "position": s.position,
            "name": s.name,
            "language": s.language,
            "status": s.status,
            "items_total": s.items_total,
            "items_done": s.items_done,
            "started_at": s.started_at.isoformat() if s.started_at else None,
            "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            "time_budget_sec": s.time_budget_sec,
            "stats": s.stats,
            "error_log": s.error_log,
        }
        for s in rows
    ]


@router.get(
    "/projects/{project_id}/graph/stats",
    dependencies=[Depends(require_project_org())],
)
async def graph_stats(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Simple stats used by the Analysis tab."""
    result = await db.execute(
        select(func.count())
        .select_from(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
    )
    return {"nodes_current": int(result.scalar() or 0)}


@router.get(
    "/projects/{project_id}/graph/search",
    dependencies=[Depends(require_project_org())],
)
async def graph_search(
    project_id: uuid.UUID,
    _: CurrentUser,
    q: str = "",
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    stmt = (
        select(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
        .order_by(Node.id)
        .limit(limit)
    )
    if q:
        stmt = stmt.where(Node.id.ilike(f"%{q}%"))
    rows = (await db.execute(stmt)).scalars().all()
    return [{"id": r.id, "kind": r.kind, "data": r.data, "certainty": r.certainty} for r in rows]


@router.get(
    "/projects/{project_id}/graph/component_map",
    dependencies=[Depends(require_project_org())],
)
async def graph_component_map(
    project_id: uuid.UUID,
    _: CurrentUser,
    kind: str | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Light-weight payload for the dashboard's graph visualizer
    (PR-49). Returns up to ``limit`` nodes + the edges between them
    in a shape that fits a force-directed layout straight away:

      {
        "nodes": [{"id", "kind", "label", "certainty", "exercised"}],
        "edges": [{"source", "target", "kind", "certainty", "exercised"}],
      }

    The audit team's biggest "본질 가치" gap was that the platform
    stored 100K+ nodes but never *showed* the graph to a human.
    This endpoint is the data plane behind the new ``/graph`` tab.

    ``kind=Component`` is the canonical first view — operators
    almost always want the high-level component map. ``kind=null``
    returns the whole truncated graph (useful for small projects
    or for the "everything" overview).
    """
    limit = max(10, min(limit, 1000))
    node_stmt = select(Node).where(
        Node.project_id == project_id, Node.valid_to.is_(None)
    )
    if kind:
        node_stmt = node_stmt.where(Node.kind == kind)
    node_stmt = node_stmt.order_by(Node.id).limit(limit)
    nodes = (await db.execute(node_stmt)).scalars().all()
    node_ids = {n.id for n in nodes}

    # Only edges where BOTH endpoints are inside the truncated
    # node set — otherwise the visualizer would draw arrows into
    # void.
    edge_rows = (
        await db.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.valid_to.is_(None),
                Edge.source_id.in_(node_ids),
                Edge.target_id.in_(node_ids),
            )
        )
    ).scalars().all()

    def _label(n: Node) -> str:
        data = n.data or {}
        return str(data.get("name") or data.get("title") or n.id)

    def _exercised(data: dict) -> bool:
        # PR-25 OTLP Tier 2 marks live edges/nodes with this flag.
        return str((data or {}).get("exercised", "")).lower() == "true"

    return {
        "nodes": [
            {
                "id": n.id,
                "kind": n.kind,
                "label": _label(n),
                "certainty": n.certainty,
                "exercised": _exercised(n.data or {}),
            }
            for n in nodes
        ],
        "edges": [
            {
                "source": e.source_id,
                "target": e.target_id,
                "kind": e.kind,
                "certainty": e.certainty,
                "exercised": _exercised(e.data or {}),
            }
            for e in edge_rows
        ],
        "truncated": len(nodes) >= limit,
    }


@router.get(
    "/projects/{project_id}/graph/certainty_breakdown",
    dependencies=[Depends(require_project_org())],
)
async def graph_certainty_breakdown(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, dict[str, int]]:
    """Coverage metric (audit C3 — "how trustworthy is our graph?").

    Returns the count of nodes + edges per certainty value
    (``verified`` / ``asserted`` / ``inferred``), so the dashboard
    can show "12% of edges are still inferred — push more
    analyzers to lift confidence" instead of a single opaque
    "1 234 nodes" number.
    """
    node_rows = await db.execute(
        select(Node.certainty, func.count(Node.id))
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
        .group_by(Node.certainty)
    )
    edge_rows = await db.execute(
        select(Edge.certainty, func.count(Edge.id))
        .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
        .group_by(Edge.certainty)
    )
    return {
        "nodes": {c: int(n) for c, n in node_rows.all()},
        "edges": {c: int(n) for c, n in edge_rows.all()},
    }


@router.get(
    "/projects/{project_id}/pipeline_latency",
    dependencies=[Depends(require_project_org())],
)
async def pipeline_latency(
    project_id: uuid.UUID,
    _: CurrentUser,
    runs: int = 10,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Per-stage latency for the project's recent runs (PR-58,
    audit A8).

    The value audit's A8 finding: "Always-on latency 미측정 —
    각 stage 타이밍은 analysis_stages 에 기록되지만 운영자에게
    가시화 안 됨". Spec §1.5 promises "first full analysis in
    ≤ 8 hours" — but an operator had no way to *see* where the
    time goes.

    Returns:

      {
        "runs_analysed": int,
        "stages": [
          {"name": str,
           "mean_sec": float,
           "max_sec": float,
           "p95_sec": float,
           "samples": int},
          …
        ],
        "mean_total_sec": float | None,   # webhook→done wall clock
        "slowest_stage": str | None,
      }

    The slowest-stage callout tells an operator which analyzer to
    tune first.
    """
    # Most-recent N completed runs for the project.
    recent_runs = (
        await db.execute(
            select(AnalysisRun.id, AnalysisRun.started_at, AnalysisRun.completed_at)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(max(1, min(runs, 100)))
        )
    ).all()
    run_ids = [r[0] for r in recent_runs]

    total_durations: list[float] = []
    for _id, started, completed in recent_runs:
        if started is not None and completed is not None:
            secs = (completed - started).total_seconds()
            if secs >= 0:
                total_durations.append(secs)

    per_stage: dict[str, list[float]] = {}
    if run_ids:
        stage_rows = (
            await db.execute(
                select(
                    AnalysisStage.name,
                    AnalysisStage.started_at,
                    AnalysisStage.completed_at,
                ).where(AnalysisStage.run_id.in_(run_ids))
            )
        ).all()
        for name, started, completed in stage_rows:
            if started is None or completed is None:
                continue
            secs = (completed - started).total_seconds()
            if secs < 0:
                continue
            per_stage.setdefault(name, []).append(secs)

    def _p95(values: list[float]) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
        return round(ordered[idx], 2)

    stages = []
    for name, vals in sorted(
        per_stage.items(), key=lambda kv: -sum(kv[1]) / max(1, len(kv[1]))
    ):
        stages.append(
            {
                "name": name,
                "mean_sec": round(sum(vals) / len(vals), 2),
                "max_sec": round(max(vals), 2),
                "p95_sec": _p95(vals),
                "samples": len(vals),
            }
        )

    return {
        "runs_analysed": len(recent_runs),
        "stages": stages,
        "mean_total_sec": (
            round(sum(total_durations) / len(total_durations), 1)
            if total_durations
            else None
        ),
        "slowest_stage": stages[0]["name"] if stages else None,
    }


@router.get(
    "/projects/{project_id}/summaries",
    dependencies=[Depends(require_project_org())],
)
async def list_summaries(
    project_id: uuid.UUID,
    _: CurrentUser,
    level: int | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Hierarchical L1-L3 summaries for the project (PR-53).

    The value audit's C4 gap: L1-L3 summaries were *produced* by
    the analysis pipeline but never *surfaced* — no API, no GUI.
    The executive-report page reads ``?level=3`` for the
    system-level narrative; ``?level=`` (all) drives a drill-down.

    Only current summaries (``superseded_by IS NULL``) are
    returned so an operator never reads a stale narrative.
    """
    from app.models.findings import Summary

    stmt = (
        select(Summary)
        .where(
            Summary.project_id == project_id,
            Summary.superseded_by.is_(None),
        )
        .order_by(Summary.level.desc(), Summary.generated_at.desc())
    )
    if level is not None:
        stmt = stmt.where(Summary.level == level)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(s.id),
            "target_id": s.target_id,
            "level": s.level,
            "summary": s.summary,
            "detailed": s.detailed,
            "claims": s.claims or [],
            "open_questions": s.open_questions or [],
            "model_used": s.model_used,
            "tokens_used": s.tokens_used,
            "generated_at": s.generated_at.isoformat() if s.generated_at else None,
        }
        for s in rows
    ]


@router.get(
    "/projects/{project_id}/llm_cost",
    dependencies=[Depends(require_project_org())],
)
async def project_llm_cost(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """LLM token spend for the project (PR-53, audit C5).

    The audit flagged that ``ANTHROPIC_API_KEY`` usage was
    invisible — no way to estimate the monthly bill. Every
    ``Summary`` row records ``tokens_used``; this sums them and
    applies a coarse per-million-token rate so the dashboard can
    show an order-of-magnitude cost. The rate is configurable via
    ``MNEMOS_LLM_USD_PER_MTOK`` (default 3.0, ~Sonnet input).
    """
    import os

    from app.models.findings import Summary

    rows = (
        await db.execute(
            select(Summary.tokens_used, Summary.model_used).where(
                Summary.project_id == project_id,
                Summary.superseded_by.is_(None),
            )
        )
    ).all()
    total_tokens = sum(int(t or 0) for t, _ in rows)
    summary_count = len(rows)
    rate = float(os.environ.get("MNEMOS_LLM_USD_PER_MTOK", "3.0"))
    est_usd = round((total_tokens / 1_000_000.0) * rate, 4)
    return {
        "summary_count": summary_count,
        "total_tokens": total_tokens,
        "rate_usd_per_mtok": rate,
        "estimated_usd": est_usd,
    }
