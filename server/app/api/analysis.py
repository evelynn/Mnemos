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
