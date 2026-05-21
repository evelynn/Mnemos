import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.db import get_session
from app.mcp.queries import impact_analysis
from app.models.findings import Finding
from app.models.plans import Plan
from app.sandbox.worktree import create_worktree

router = APIRouter(tags=["plans"])


class PlanSpec(BaseModel):
    title: str
    motivation: str
    non_goals: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)


class PlanTask(BaseModel):
    id: str
    title: str
    description: str = ""
    affects: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class PlanSubmit(BaseModel):
    spec: PlanSpec
    tasks: list[PlanTask]
    target_component_id: str
    requester: str
    # Optional reproducibility pin. None → worktree is created at the
    # mirror's current HEAD; the resolved SHA is recorded in
    # worktree_meta either way so re-runs can reference it.
    base_sha: str | None = None


class PlanOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    spec: dict[str, Any]
    tasks: list[dict[str, Any]]
    impact_report: dict[str, Any] | None
    requester: str
    worktree_path: str | None
    created_at: datetime
    approved_at: datetime | None
    approved_by: str | None


def _out(plan: Plan) -> PlanOut:
    return PlanOut(
        id=plan.id,
        project_id=plan.project_id,
        status=plan.status,
        spec=plan.spec,
        tasks=plan.tasks,
        impact_report=plan.impact_report,
        requester=plan.requester,
        worktree_path=plan.worktree_path,
        created_at=plan.created_at,
        approved_at=plan.approved_at,
        approved_by=plan.approved_by,
    )


@router.post(
    "/api/v1/projects/{project_id}/plans",
    dependencies=[Depends(require_project_org())],
)
async def submit_plan(
    project_id: uuid.UUID,
    body: PlanSubmit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    impacts = await impact_analysis(
        db, project_id=project_id, symbol_id=body.target_component_id, max_depth=3
    )
    plan = Plan(
        project_id=project_id,
        spec=body.spec.model_dump(),
        tasks=[t.model_dump() for t in body.tasks],
        impact_report={
            "directly_affected": impacts["directly_affected"],
            "transitively_affected": impacts["transitively_affected"],
            "opaque_components_touched": impacts["opaque_components_touched"],
        },
        requester=body.requester,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    worktree = await create_worktree(plan.id, project_id, base_sha=body.base_sha)
    plan.worktree_path = str(worktree)
    plan.worktree_meta = {
        "base_sha": body.base_sha,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    await db.commit()
    await db.refresh(plan)

    await audit_record(
        actor=f"user:{user.id}",
        action="plan.submit",
        target=str(plan.id),
        project_id=project_id,
        details={"title": body.spec.title, "tasks": len(body.tasks)},
    )
    return _out(plan)


@router.post("/api/v1/findings/{finding_id}/plan")
async def plan_from_finding(
    finding_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    """One-click: turn a finding into a draft Plan (PR-52, audit B4).

    The value audit's B-area gap was that the operator had to
    *manually* re-type a Plan spec from a finding — there was no
    automated path from "here's a problem" to "here's a task to
    fix it". This endpoint pre-fills a Plan from the finding's
    kind, subject, severity, risk score, and the deterministic
    remediation hint PR-50 already attached.

    The Plan lands in ``status="pending_approval"`` exactly like a
    hand-written one — the operator still reviews + approves, and
    the ultrareview pipeline still gates the eventual diff. This
    just removes the transcription step.
    """
    finding = (
        await db.execute(select(Finding).where(Finding.id == finding_id))
    ).scalar_one_or_none()
    if finding is None:
        raise HTTPException(status_code=404, detail="finding_not_found")
    # Org isolation — the finding's project must be in the caller's org.
    from app.models.projects import Project

    project = (
        await db.execute(
            select(Project).where(Project.id == finding.project_id)
        )
    ).scalar_one_or_none()
    if project is None or project.organization_id != user.organization_id:
        raise HTTPException(status_code=404, detail="finding_not_found")

    subject = finding.subject_node_id or (
        str(finding.subject_edge_id) if finding.subject_edge_id else ""
    )
    remediation = finding.remediation or (
        "Review the finding detail and decide on a fix."
    )
    spec = {
        "title": f"Fix: {finding.kind} on {subject or '(unscoped)'}",
        "motivation": (
            f"Finding {finding.kind} (severity {finding.severity}, "
            f"risk {finding.risk_score}/100) flagged "
            f"{subject or 'the project'}. {remediation}"
        ),
        "non_goals": [],
        "success_criteria": [
            f"The {finding.kind} finding on {subject or 'the subject'} "
            "no longer reproduces after re-analysis.",
        ],
    }
    tasks = [
        {
            "id": "task-1",
            "title": f"Address {finding.kind}",
            "description": remediation,
            "affects": [subject] if subject else [],
            "depends_on": [],
        }
    ]
    impacts = await impact_analysis(
        db,
        project_id=finding.project_id,
        symbol_id=subject or "",
        max_depth=3,
    )
    plan = Plan(
        project_id=finding.project_id,
        spec=spec,
        tasks=tasks,
        impact_report={
            "directly_affected": impacts["directly_affected"],
            "transitively_affected": impacts["transitively_affected"],
            "opaque_components_touched": impacts["opaque_components_touched"],
            "source_finding_id": str(finding.id),
        },
        requester=f"user:{user.id}",
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)

    worktree = await create_worktree(plan.id, finding.project_id, base_sha=None)
    plan.worktree_path = str(worktree)
    plan.worktree_meta = {
        "base_sha": None,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "from_finding": str(finding.id),
    }
    await db.commit()
    await db.refresh(plan)

    await audit_record(
        actor=f"user:{user.id}",
        action="plan.from_finding",
        target=str(plan.id),
        project_id=finding.project_id,
        details={"finding_id": str(finding.id), "kind": finding.kind},
    )
    return _out(plan)


@router.get(
    "/api/v1/projects/{project_id}/plans",
    dependencies=[Depends(require_project_org())],
)
async def list_plans(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[PlanOut]:
    rows = (
        await db.execute(
            select(Plan)
            .where(Plan.project_id == project_id)
            .order_by(Plan.created_at.desc())
            .limit(50)
        )
    ).scalars().all()
    return [_out(r) for r in rows]


@router.get("/api/v1/plans/{plan_id}")
async def get_plan(
    plan_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = (await db.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _out(plan)


class PlanDecision(BaseModel):
    status: Literal["approve", "reject", "regenerate"]
    feedback: str | None = None


@router.post("/api/v1/plans/{plan_id}/decide")
async def decide_plan(
    plan_id: uuid.UUID,
    body: PlanDecision,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = (await db.execute(select(Plan).where(Plan.id == plan_id))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="not_found")
    status_map = {"approve": "approved", "reject": "rejected", "regenerate": "pending_approval"}
    plan.status = status_map[body.status]
    if body.status == "approve":
        plan.approved_at = datetime.utcnow()
        plan.approved_by = f"user:{user.id}"
    plan.feedback = body.feedback
    await db.commit()
    await db.refresh(plan)
    await audit_record(
        actor=f"user:{user.id}",
        action=f"plan.{body.status}",
        target=str(plan.id),
        project_id=plan.project_id,
    )
    return _out(plan)
