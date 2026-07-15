import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org, resolve_project_org, same_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.finding_currentness import (
    FINDING_NOT_CURRENT_CODE,
    FindingNotCurrent,
    lock_current_finding_for_action,
)
from app.mcp.queries import impact_analysis
from app.models.findings import Finding
from app.models.plans import Plan
from app.plan_provenance import (
    PLAN_BASE_REVISION_MISMATCH_CODE,
    PLAN_SOURCE_REVISION_STALE_CODE,
    PLAN_SOURCE_REVISION_UNAVAILABLE_CODE,
    PlanBaseRevisionMismatch,
    PlanSourceRevision,
    PlanSourceRevisionStale,
    PlanSourceRevisionUnavailable,
    bind_plan_source_revision,
    lock_current_plan_for_action,
    lock_current_plan_source_revision,
)
from app.sandbox.worktree import (
    WorktreeRevisionError,
    WorktreeRevisionMismatch,
    create_worktree,
    destroy_worktree,
    verify_worktree_revision,
    worktree_path,
)

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
    # PR-133 — DoS bound. A plan with 100+ tasks is almost certainly
    # generated noise; large submissions multiply through every
    # downstream stage (worktree creation, MR description, etc).
    tasks: list[PlanTask] = Field(..., max_length=100)
    target_component_id: str = Field(..., max_length=300)
    requester: str = Field(..., max_length=128)
    # Optional compatibility input. None resolves to the exact canonical
    # publication SHA; an explicit value must equal that SHA byte-for-byte.
    base_sha: str | None = Field(default=None, max_length=64)


class PlanOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    status: str
    spec: dict[str, Any]
    tasks: list[dict[str, Any]]
    impact_report: dict[str, Any] | None
    requester: str
    worktree_path: str | None
    source_run_id: uuid.UUID | None
    source_git_sha: str | None
    source_graph_generation: int | None
    source_overlay_generation: int | None
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
        source_run_id=plan.source_run_id,
        source_git_sha=plan.source_git_sha,
        source_graph_generation=plan.source_graph_generation,
        source_overlay_generation=plan.source_overlay_generation,
        created_at=plan.created_at,
        approved_at=plan.approved_at,
        approved_by=plan.approved_by,
    )


def _revision_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PlanBaseRevisionMismatch):
        detail = PLAN_BASE_REVISION_MISMATCH_CODE
    elif isinstance(exc, PlanSourceRevisionStale):
        detail = PLAN_SOURCE_REVISION_STALE_CODE
    else:
        detail = PLAN_SOURCE_REVISION_UNAVAILABLE_CODE
    return HTTPException(status_code=409, detail=detail)


def _worktree_http_error(exc: WorktreeRevisionError) -> HTTPException:
    detail = (
        "plan_worktree_revision_mismatch"
        if isinstance(exc, WorktreeRevisionMismatch)
        else "plan_worktree_revision_unavailable"
    )
    return HTTPException(status_code=409, detail=detail)


def _revision_worktree_meta(
    revision: PlanSourceRevision,
    **extra: str,
) -> dict[str, Any]:
    return {
        "base_sha": revision.git_sha,
        "source_run_id": str(revision.run_id),
        "source_graph_generation": revision.source_generation,
        "source_overlay_generation": revision.overlay_generation,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


@router.post(
    "/api/v1/projects/{project_id}/plans",
    dependencies=[
        Depends(require_project_org()),
        Depends(require_operator),
    ],
)
async def submit_plan(
    project_id: uuid.UUID,
    body: PlanSubmit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = Plan(
        id=uuid.uuid4(),
        project_id=project_id,
        spec=body.spec.model_dump(),
        tasks=[t.model_dump() for t in body.tasks],
        requester=body.requester,
    )
    worktree_created = False
    try:
        revision = await lock_current_plan_source_revision(
            db,
            project_id=project_id,
            requested_base_sha=body.base_sha,
        )
        impacts = await impact_analysis(
            db,
            project_id=project_id,
            symbol_id=body.target_component_id,
            max_depth=3,
        )
        plan.impact_report = {
            "directly_affected": impacts["directly_affected"],
            "transitively_affected": impacts["transitively_affected"],
            "opaque_components_touched": impacts["opaque_components_touched"],
        }
        bind_plan_source_revision(plan, revision)
        db.add(plan)
        await db.flush()

        worktree = await create_worktree(
            plan.id,
            project_id,
            base_sha=revision.git_sha,
        )
        worktree_created = True
        plan.worktree_path = str(worktree)
        plan.worktree_meta = _revision_worktree_meta(revision)
        await audit_record(
            actor=f"user:{user.id}",
            action="plan.submit",
            target=str(plan.id),
            project_id=project_id,
            details={
                "title": body.spec.title,
                "tasks": len(body.tasks),
                "source_run_id": str(revision.run_id),
                "source_git_sha": revision.git_sha,
                "source_generation": revision.source_generation,
                "overlay_generation": revision.overlay_generation,
            },
            session=db,
        )
        # The GraphHead + AnalysisRun locks are intentionally held through
        # this commit, so publication cannot move between review evidence,
        # worktree verification, and durable Plan provenance.
        await db.commit()
    except (
        PlanBaseRevisionMismatch,
        PlanSourceRevisionUnavailable,
    ) as exc:
        await db.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        raise _revision_http_error(exc) from exc
    except WorktreeRevisionError as exc:
        await db.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        raise _worktree_http_error(exc) from exc
    except Exception:
        await db.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        raise
    await db.refresh(plan)
    return _out(plan)


@router.post(
    "/api/v1/findings/{finding_id}/plan",
    dependencies=[Depends(require_operator)],
)
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
    finding_ref = (
        await db.execute(
            select(Finding.project_id).where(Finding.id == finding_id)
        )
    ).one_or_none()
    if finding_ref is None:
        raise HTTPException(status_code=404, detail="finding_not_found")
    # Org isolation — the finding's project must be in the caller's org.
    from app.models.projects import Project

    project = (
        await db.execute(
            select(Project.id, Project.organization_id).where(
                Project.id == finding_ref.project_id
            )
        )
    ).one_or_none()
    if project is None or not same_org(user, project.organization_id):
        raise HTTPException(status_code=404, detail="finding_not_found")
    try:
        finding = await lock_current_finding_for_action(
            db,
            project_id=finding_ref.project_id,
            finding_id=finding_id,
        )
        revision = await lock_current_plan_source_revision(
            db,
            project_id=finding_ref.project_id,
        )
    except FindingNotCurrent as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=FINDING_NOT_CURRENT_CODE,
        ) from exc
    except PlanSourceRevisionUnavailable as exc:
        await db.rollback()
        raise _revision_http_error(exc) from exc

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
        id=uuid.uuid4(),
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
    bind_plan_source_revision(plan, revision)
    worktree_created = False
    try:
        db.add(plan)
        await db.flush()
        worktree = await create_worktree(
            plan.id,
            finding.project_id,
            base_sha=revision.git_sha,
        )
        worktree_created = True
        plan.worktree_path = str(worktree)
        plan.worktree_meta = {
            **_revision_worktree_meta(revision),
            "from_finding": str(finding.id),
        }
        await audit_record(
            actor=f"user:{user.id}",
            action="plan.from_finding",
            target=str(plan.id),
            project_id=finding.project_id,
            details={
                "finding_id": str(finding.id),
                "kind": finding.kind,
                "source_run_id": str(revision.run_id),
                "source_git_sha": revision.git_sha,
                "source_generation": revision.source_generation,
                "overlay_generation": revision.overlay_generation,
            },
            session=db,
        )
        await db.commit()
    except WorktreeRevisionError as exc:
        await db.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, finding.project_id)
        raise _worktree_http_error(exc) from exc
    except Exception:
        await db.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, finding.project_id)
        raise
    await db.refresh(plan)
    return _out(plan)


@router.get("/api/v1/findings/{finding_id}/plans")
async def plans_for_finding(
    finding_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[PlanOut]:
    """Plans spawned from a finding (PR-56 — Finding↔Plan linkage).

    PR-52 recorded ``impact_report.source_finding_id`` when a Plan
    was created from a finding, but the link was write-only — the
    findings table couldn't show "you already opened a Plan for
    this". This endpoint closes the loop: given a finding, return
    every Plan whose impact_report names it.

    Org-isolated via the finding's project.
    """
    finding = (
        await db.execute(select(Finding).where(Finding.id == finding_id))
    ).scalar_one_or_none()
    if finding is None:
        raise HTTPException(status_code=404, detail="finding_not_found")
    from app.models.projects import Project

    project = (
        await db.execute(
            select(Project).where(Project.id == finding.project_id)
        )
    ).scalar_one_or_none()
    if project is None or not same_org(user, project.organization_id):
        raise HTTPException(status_code=404, detail="finding_not_found")

    rows = (
        await db.execute(
            select(Plan).where(Plan.project_id == finding.project_id)
        )
    ).scalars().all()
    target = str(finding_id)
    matched = [
        p
        for p in rows
        if (p.impact_report or {}).get("source_finding_id") == target
    ]
    return [_out(p) for p in matched]


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


async def _plan_in_user_org(db, user, plan_id):
    """Load a plan iff it belongs to the caller's org; 404 otherwise.
    Returns the Plan. Mirrors the pattern PR-60 used for findings —
    /plans routes key off plan_id, so require_project_org (which needs
    a project_id path param) can't gate them; the org check is inline."""
    plan = (
        await db.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="not_found")
    org_id = await resolve_project_org(db, plan.project_id)
    if not same_org(user, org_id):
        raise HTTPException(status_code=404, detail="not_found")
    return plan


@router.get("/api/v1/plans/{plan_id}")
async def get_plan(
    plan_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan = await _plan_in_user_org(db, user, plan_id)
    return _out(plan)


class PlanDecision(BaseModel):
    status: Literal["approve", "reject", "regenerate"]
    feedback: str | None = None


@router.post(
    "/api/v1/plans/{plan_id}/decide",
    dependencies=[Depends(require_operator)],
)
async def decide_plan(
    plan_id: uuid.UUID,
    body: PlanDecision,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> PlanOut:
    plan_ref = await _plan_in_user_org(db, user, plan_id)
    try:
        plan, revision = await lock_current_plan_for_action(
            db,
            plan_id=plan_id,
            project_id=plan_ref.project_id,
        )
    except (PlanSourceRevisionStale, PlanSourceRevisionUnavailable) as exc:
        await db.rollback()
        raise _revision_http_error(exc) from exc
    if body.status == "approve":
        try:
            expected_path = worktree_path(plan.id)
            if plan.worktree_path != str(expected_path):
                raise WorktreeRevisionMismatch(
                    "stored Plan worktree path is not the plan-scoped path"
                )
            await verify_worktree_revision(plan.id, revision.git_sha)
        except WorktreeRevisionError as exc:
            await db.rollback()
            raise _worktree_http_error(exc) from exc
    status_map = {"approve": "approved", "reject": "rejected", "regenerate": "pending_approval"}
    plan.status = status_map[body.status]
    if body.status == "approve":
        plan.approved_at = datetime.now(timezone.utc)
        plan.approved_by = f"user:{user.id}"
    plan.feedback = body.feedback
    await audit_record(
        actor=f"user:{user.id}",
        action=f"plan.{body.status}",
        target=str(plan.id),
        project_id=plan.project_id,
        details={
            "source_run_id": str(revision.run_id),
            "source_git_sha": revision.git_sha,
            "source_generation": revision.source_generation,
            "overlay_generation": revision.overlay_generation,
        },
        session=db,
    )
    await db.commit()
    await db.refresh(plan)
    return _out(plan)
