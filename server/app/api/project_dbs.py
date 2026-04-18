"""CRUD for per-project database bindings (spec §12.2)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User
from app.models.projects import ProjectDB

router = APIRouter(prefix="/api/v1/projects/{project_id}/dbs", tags=["project_dbs"])

DBKind = Literal["mssql", "oracle"]


class ProjectDBCreate(BaseModel):
    kind: DBKind
    display_name: str = Field(min_length=1, max_length=200)
    component_id: str = Field(min_length=1, max_length=300)
    secret_id: uuid.UUID | None = None
    allow_awr: bool = False
    sensitive_tables: list[str] = Field(default_factory=list)
    masking_rules: dict = Field(default_factory=dict)
    maintenance_windows: list[str] = Field(default_factory=list)


class ProjectDBUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    secret_id: uuid.UUID | None = None
    allow_awr: bool | None = None
    sensitive_tables: list[str] | None = None
    masking_rules: dict | None = None
    maintenance_windows: list[str] | None = None


class ProjectDBOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    display_name: str
    component_id: str
    secret_id: uuid.UUID | None
    allow_awr: bool
    sensitive_tables: list[str]
    masking_rules: dict
    maintenance_windows: list[str]
    created_at: datetime


def _to_out(row: ProjectDB) -> ProjectDBOut:
    return ProjectDBOut(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        display_name=row.display_name,
        component_id=row.component_id,
        secret_id=row.secret_id,
        allow_awr=row.allow_awr,
        sensitive_tables=list(row.sensitive_tables or []),
        masking_rules=dict(row.masking_rules or {}),
        maintenance_windows=list(row.maintenance_windows or []),
        created_at=row.created_at,
    )


@router.get("")
async def list_project_dbs(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[ProjectDBOut]:
    result = await db.execute(
        select(ProjectDB)
        .where(ProjectDB.project_id == project_id)
        .order_by(ProjectDB.created_at.desc())
    )
    return [_to_out(r) for r in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project_db(
    project_id: uuid.UUID,
    body: ProjectDBCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> ProjectDBOut:
    row = ProjectDB(
        project_id=project_id,
        kind=body.kind,
        display_name=body.display_name,
        component_id=body.component_id,
        secret_id=body.secret_id,
        allow_awr=body.allow_awr,
        sensitive_tables=body.sensitive_tables,
        masking_rules=body.masking_rules,
        maintenance_windows=body.maintenance_windows,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="component_id_already_bound")
    await db.refresh(row)
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.create",
        target=str(row.id),
        project_id=project_id,
        details={"component_id": row.component_id, "kind": row.kind},
    )
    return _to_out(row)


@router.get("/{db_id}")
async def get_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> ProjectDBOut:
    row = (
        await db.execute(
            select(ProjectDB).where(
                ProjectDB.id == db_id, ProjectDB.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _to_out(row)


@router.patch("/{db_id}")
async def update_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    body: ProjectDBUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> ProjectDBOut:
    row = (
        await db.execute(
            select(ProjectDB).where(
                ProjectDB.id == db_id, ProjectDB.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    changes: dict = {}
    for field_name in (
        "display_name",
        "secret_id",
        "allow_awr",
        "sensitive_tables",
        "masking_rules",
        "maintenance_windows",
    ):
        value = getattr(body, field_name)
        if value is not None:
            setattr(row, field_name, value)
            changes[field_name] = True
    await db.commit()
    await db.refresh(row)
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.update",
        target=str(row.id),
        project_id=project_id,
        details={"fields": sorted(changes.keys())},
    )
    return _to_out(row)


@router.delete("/{db_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    result = await db.execute(
        delete(ProjectDB).where(
            ProjectDB.id == db_id, ProjectDB.project_id == project_id
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="not_found")
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.delete",
        target=str(db_id),
        project_id=project_id,
    )
