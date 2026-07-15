import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts import (
    build_agents_md,
    build_mcp_config,
    build_project_index,
    build_task_context_pack,
)
from app.api.graph_guard import require_readable_current_graph
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.db import get_session
from app.models.projects import Project

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/artifacts",
    tags=["artifacts"],
    dependencies=[Depends(require_project_org())],
)


async def _require_project(project_id: uuid.UUID, db: AsyncSession) -> Project:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="project_not_found")
    return project


@router.get("")
async def list_artifacts(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    await _require_project(project_id, db)
    base = f"/api/v1/projects/{project_id}/artifacts"
    return {
        "artifacts": [
            {"name": "mcp.json", "path": f"{base}/mcp.json"},
            {"name": "AGENTS.md", "path": f"{base}/AGENTS.md"},
            {"name": "mnemos-project-index.json", "path": f"{base}/project-index.json"},
            {
                "name": "mnemos-task-context-pack.json",
                "path": f"{base}/task-context-pack.json?target_id=<node-or-finding-id>",
            },
        ]
    }


@router.get("/mcp.json")
async def get_mcp_json(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> JSONResponse:
    await _require_project(project_id, db)
    return JSONResponse(build_mcp_config(project_id))


@router.get("/AGENTS.md")
async def get_agents_md(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlainTextResponse:
    await _require_project(project_id, db)
    md = await build_agents_md(db, project_id=project_id)
    return PlainTextResponse(md, media_type="text/markdown")


@router.get("/project-index.json")
async def get_project_index_json(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
    top_k: int = Query(default=25, ge=1, le=100),
) -> JSONResponse:
    await _require_project(project_id, db)
    return JSONResponse(await build_project_index(db, project_id=project_id, top_k=top_k))


@router.get(
    "/task-context-pack.json",
    dependencies=[Depends(require_readable_current_graph, scope="function")],
)
async def get_task_context_pack_json(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
    target_id: str = Query(min_length=1, max_length=512),
    target_kind: str = Query(default="auto", pattern="^(auto|node|symbol|contract|data_entity|edge|finding)$"),
    intent: str | None = Query(default=None, max_length=500),
    budget_items: int = Query(default=50, ge=5, le=200),
) -> JSONResponse:
    await _require_project(project_id, db)
    return JSONResponse(
        await build_task_context_pack(
            db,
            project_id=project_id,
            target_id=target_id,
            target_kind=target_kind,
            intent=intent,
            budget_items=budget_items,
        )
    )
