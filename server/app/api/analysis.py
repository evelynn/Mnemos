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
from app.db import get_session
from app.models.graph import AnalysisRun, Node
from app.models.projects import Project
from app.orchestrator.progress import ProgressBus
from app.orchestrator.queue import get_queue

router = APIRouter(prefix="/api/v1", tags=["analysis"])


class AnalysisTriggerRequest(BaseModel):
    git_sha: str = Field(default="HEAD")
    scope: str = Field(default="full", pattern="^(full|incremental)$")
    source_path: str = Field(description="Absolute path visible to the worker")


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


@router.get("/projects/{project_id}/analysis_runs")
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


@router.get("/projects/{project_id}/graph/stats")
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


@router.get("/projects/{project_id}/graph/search")
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
