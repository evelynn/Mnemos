import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.analyzers.registry import SOURCE_ANALYZER_LANGUAGES
from app.extractor.agent_extract import AGENT_LANGUAGE_EXTENSIONS

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_admin, require_operator
from app.db import get_session
from app.graph_publication import bootstrap_graph_head
from app.models.auth import User
from app.models.projects import Project

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

# A language is accepted if a deterministic ggoss analyzer ships for it
# OR it is eligible for Claude-Code agent extraction (PR-140). Hardcoding
# {csharp, typescript} previously made it impossible to even create a
# project for a C++ / Go / Python / Oracle codebase.
SUPPORTED_LANGUAGES = SOURCE_ANALYZER_LANGUAGES | frozenset(AGENT_LANGUAGE_EXTENSIONS)


def _validate_languages(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    unsupported = sorted({lang for lang in value if lang not in SUPPORTED_LANGUAGES})
    if unsupported:
        raise ValueError(
            f"unsupported language(s): {unsupported}. supported: "
            f"{sorted(SUPPORTED_LANGUAGES)}"
        )
    return value


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    gitlab_project_id: int = Field(gt=0)
    gitlab_url: HttpUrl
    default_branch: str = Field(default="main", min_length=1)
    languages: list[str] = Field(min_length=1)

    _check_langs = field_validator("languages")(_validate_languages)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    default_branch: str | None = Field(default=None, min_length=1)
    languages: list[str] | None = Field(default=None, min_length=1)

    _check_langs = field_validator("languages")(_validate_languages)


class ProjectOut(BaseModel):
    id: uuid.UUID
    name: str
    gitlab_project_id: int
    gitlab_url: str
    default_branch: str
    languages: list[str]
    created_at: datetime
    updated_at: datetime


def _to_out(p: Project) -> ProjectOut:
    return ProjectOut(
        id=p.id,
        name=p.name,
        gitlab_project_id=p.gitlab_project_id,
        gitlab_url=p.gitlab_url,
        default_branch=p.default_branch,
        languages=list(p.languages),
        created_at=p.created_at,
        updated_at=p.updated_at,
    )


@router.get("")
async def list_projects(
    user: CurrentUser, db: AsyncSession = Depends(get_session)
) -> list[ProjectOut]:
    # NULL ownership is fail-closed: org deletion can create org-less users
    # and projects, so treating either as global would collapse tenancy.
    if user.organization_id is None:
        return []
    stmt = (
        select(Project)
        .where(Project.organization_id == user.organization_id)
        .order_by(Project.created_at.desc())
    )
    result = await db.execute(stmt)
    return [_to_out(p) for p in result.scalars().all()]


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_operator)],
)
async def create_project(
    body: ProjectCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> ProjectOut:
    if user.organization_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="organization_required",
        )
    project = Project(
        name=body.name,
        gitlab_project_id=body.gitlab_project_id,
        gitlab_url=str(body.gitlab_url),
        default_branch=body.default_branch,
        languages=list(body.languages),
        # Inherit org from the creator so the tenancy boundary is set
        # at row-birth; cross-org admins can reassign via PATCH.
        organization_id=user.organization_id,
    )
    db.add(project)
    await db.flush()
    await bootstrap_graph_head(db, project_id=project.id)
    await db.commit()
    await db.refresh(project)
    await audit_record(
        actor=f"user:{user.id}",
        action="project.create",
        target=str(project.id),
        project_id=project.id,
        details={"name": project.name, "languages": list(project.languages)},
    )
    return _to_out(project)


@router.get("/{project_id}", dependencies=[Depends(require_project_org())])
async def get_project(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _to_out(project)


@router.patch(
    "/{project_id}",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_operator),
    ],
)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> ProjectOut:
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="not_found")
    if body.name is not None:
        project.name = body.name
    if body.default_branch is not None:
        project.default_branch = body.default_branch
    if body.languages is not None:
        project.languages = list(body.languages)
    await db.commit()
    await db.refresh(project)
    await audit_record(
        actor=f"user:{user.id}",
        action="project.update",
        target=str(project.id),
        project_id=project.id,
    )
    return _to_out(project)


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_project_org())],
)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    result = await db.execute(delete(Project).where(Project.id == project_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="not_found")
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="project.delete",
        target=str(project_id),
        project_id=project_id,
    )
