import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.artifacts import build_agents_md, build_mcp_config
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
