import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.db import get_session
from app.merge.findings import run_all
from app.models.findings import Finding

router = APIRouter(tags=["findings"])


class FindingOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    severity: str
    status: str
    subject_node_id: str | None
    subject_edge_id: uuid.UUID | None
    detail: dict[str, Any]
    # PR-50 — solution-analysis fields.
    risk_score: int
    priority: str
    remediation: str | None
    cwe_id: str | None
    first_seen_at: datetime
    last_seen_at: datetime
    acknowledged_at: datetime | None
    resolved_at: datetime | None


class FindingPatch(BaseModel):
    status: Literal["open", "acknowledged", "resolved", "false_positive"]


def _out(f: Finding) -> FindingOut:
    from app.merge.risk import priority_label

    return FindingOut(
        id=f.id,
        project_id=f.project_id,
        kind=f.kind,
        severity=f.severity,
        status=f.status,
        subject_node_id=f.subject_node_id,
        subject_edge_id=f.subject_edge_id,
        detail=f.detail,
        risk_score=f.risk_score,
        priority=priority_label(f.risk_score),
        remediation=f.remediation,
        cwe_id=f.cwe_id,
        first_seen_at=f.first_seen_at,
        last_seen_at=f.last_seen_at,
        acknowledged_at=f.acknowledged_at,
        resolved_at=f.resolved_at,
    )


@router.get(
    "/api/v1/projects/{project_id}/findings",
    dependencies=[Depends(require_project_org())],
)
async def list_findings(
    project_id: uuid.UUID,
    _: CurrentUser,
    severity: str | None = None,
    status: str | None = None,
    sort: str = "risk",
    limit: int = 50,
    db: AsyncSession = Depends(get_session),
) -> list[FindingOut]:
    """List findings, default-ordered by risk score descending.

    PR-50 — the dashboard's whole point is "what should I fix
    first?". Sorting by ``last_seen_at`` (the old default) answered
    "what changed most recently?" instead. ``sort=recent`` keeps
    the old behaviour for anyone who wants it.
    """
    stmt = (
        select(Finding)
        .where(Finding.project_id == project_id)
        .limit(max(1, min(limit, 500)))
    )
    if sort == "recent":
        stmt = stmt.order_by(Finding.last_seen_at.desc())
    else:
        # Risk-first, recency as the tie-breaker.
        stmt = stmt.order_by(
            Finding.risk_score.desc(), Finding.last_seen_at.desc()
        )
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if status:
        stmt = stmt.where(Finding.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(f) for f in rows]


@router.patch("/api/v1/findings/{finding_id}")
async def patch_finding(
    finding_id: uuid.UUID,
    body: FindingPatch,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> FindingOut:
    f = (
        await db.execute(select(Finding).where(Finding.id == finding_id))
    ).scalar_one_or_none()
    if f is None:
        raise HTTPException(status_code=404, detail="not_found")
    f.status = body.status
    now = datetime.now(tz=timezone.utc)
    # PR-50 — record the acknowledged → resolved lifecycle timestamps
    # so the dashboard can compute time-to-acknowledge / time-to-
    # resolve (audit B6).
    if body.status == "acknowledged" and f.acknowledged_at is None:
        f.acknowledged_at = now
    if body.status in {"resolved", "false_positive"}:
        f.resolved_at = now
        f.resolved_by = f"user:{user.id}"
    await db.commit()
    await db.refresh(f)
    await audit_record(
        actor=f"user:{user.id}",
        action="finding.update",
        target=str(finding_id),
        project_id=f.project_id,
        details={"status": body.status},
    )
    return _out(f)


@router.post(
    "/api/v1/projects/{project_id}/findings/rebuild",
    dependencies=[Depends(require_project_org())],
)
async def rebuild_findings(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    stats = await run_all(db, project_id)
    await audit_record(
        actor=f"user:{user.id}",
        action="finding.rebuild",
        target=str(project_id),
        project_id=project_id,
        details=stats,
    )
    return stats
