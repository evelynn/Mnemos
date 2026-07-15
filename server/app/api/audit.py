import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import false, select
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


def _apply_audit_org_scope(stmt, organization_id: uuid.UUID | None):
    """Apply the one canonical AuditLog tenant-visibility policy.

    An org-scoped caller sees only events attached to that org's projects.
    ``project_id IS NULL`` can contain platform/auth/Secret metadata and is
    therefore platform-level, not a wildcard visible to every tenant.

    There is no platform-admin role. An org-less principal can be produced by
    organisation deletion, so it fails closed rather than becoming global.
    """

    if organization_id is None:
        return stmt.where(false())
    org_projects = select(Project.id).where(
        Project.organization_id == organization_id
    )
    return stmt.where(AuditLog.project_id.in_(org_projects))


@router.get("")
async def list_audit(
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=1000),
    actor: str | None = None,
    action: str | None = None,
    db: AsyncSession = Depends(get_session),
) -> list[AuditEntry]:
    stmt = select(AuditLog).order_by(AuditLog.occurred_at.desc()).limit(limit)
    # Tenancy boundary: an org user only sees entries tied to projects in
    # that organisation. Platform-level NULL-project events are not tenant
    # data. Without this, a viewer in org A could enumerate org B by actor.
    stmt = _apply_audit_org_scope(stmt, user.organization_id)
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


# ---------------------------------------------------------------------------
# Spec §13.2 — MCP session listing (PR-93).
# ---------------------------------------------------------------------------

mcp_router = APIRouter(prefix="/api/v1", tags=["mcp"])


@mcp_router.get("/mcp_sessions")
async def list_mcp_sessions(
    user: CurrentUser,
    limit: int = Query(default=50, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Recent MCP tool calls grouped per actor — the spec §13.2
    "MCP sessions" read surface. There is no dedicated session table;
    we surface the audit-log view (actor + first / last call + count),
    which is what an operator needs to answer "who is talking to my
    server right now?".
    """
    from sqlalchemy import func as _func

    stmt = _apply_audit_org_scope(
        select(
            AuditLog.actor,
            _func.min(AuditLog.occurred_at).label("first_seen"),
            _func.max(AuditLog.occurred_at).label("last_seen"),
            _func.count(AuditLog.id).label("call_count"),
        )
        .where(AuditLog.action.like("mcp.tool.%")),
        user.organization_id,
    )
    stmt = (
        stmt
        .group_by(AuditLog.actor)
        .order_by(_func.max(AuditLog.occurred_at).desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        {
            "actor": row.actor,
            "first_seen": row.first_seen.isoformat() if row.first_seen else None,
            "last_seen": row.last_seen.isoformat() if row.last_seen else None,
            "call_count": int(row.call_count),
        }
        for row in rows
    ]


@mcp_router.get("/mcp_sessions/{actor}/requests")
async def mcp_session_requests(
    actor: str,
    user: CurrentUser,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    """Per-session request log — spec §13.2's drill-down. Returns
    recent ``mcp.tool.*`` audit entries for the given actor."""
    stmt = _apply_audit_org_scope(
        select(AuditLog)
        .where(AuditLog.actor == actor, AuditLog.action.like("mcp.tool.%")),
        user.organization_id,
    )
    stmt = (
        stmt
        .order_by(AuditLog.occurred_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "action": r.action,
            "target": r.target,
            "project_id": str(r.project_id) if r.project_id else None,
            "details": r.details,
            "occurred_at": r.occurred_at.isoformat(),
        }
        for r in rows
    ]
