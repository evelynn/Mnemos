import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.audit import AuditLog

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
    _: CurrentUser,
    limit: int = Query(default=100, ge=1, le=1000),
    actor: str | None = None,
    action: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[AuditEntry]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
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
