import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.audit import AuditLog
from app.models.projects import Project

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


class AuditEntry(BaseModel):
    id: int
    actor: str
    action: str
    target: str | None
    project_id: uuid.UUID | None
    details: dict[str, Any] | None
    occurred_at: datetime


@router.get("")
async def list_audit(
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=1000),
    actor: str | None = None,
    action: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[AuditEntry]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
    # Tenancy boundary: a user only sees entries that either name no
    # project (system-level events) or that belong to a project in their
    # organisation. Without this, a viewer in org A could enumerate
    # actions in org B by passing a guessed actor= filter.
    if user.organization_id is not None:
        org_projects = select(Project.id).where(
            Project.organization_id == user.organization_id
        )
        stmt = stmt.where(
            or_(
                AuditLog.project_id.is_(None),
                AuditLog.project_id.in_(org_projects),
            )
        )
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    result = await db.execute(stmt)
    return [
        AuditEntry(
            id=row.id,
            actor=row.actor,
            action=row.action,
            target=row.target,
            project_id=row.project_id,
            details=row.details,
            occurred_at=row.occurred_at,
        )
        for row in result.scalars().all()
    ]
