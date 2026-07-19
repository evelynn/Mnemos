import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import same_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.gitlab_client.mr import MRPayloadChanged, create_mr_from_worktree
from app.graph_publication import (
    GraphGenerationChanged,
    GraphPublicationError,
)
from app.models.auth import User
from app.models.plans import DiffBreakGlassGrant, DiffSubmission, Plan
from app.models.projects import Project
from app.obs.metrics import break_glass_grants_total
from app.plan_provenance import (
    PlanSourceRevisionError,
    lock_current_plan_for_action,
)
from app.review_revision import (
    ReviewRevisionStale,
    bind_review_revision,
    lock_review_graph_revision,
    read_review_graph_stamp,
    require_review_revision,
    resolve_review_revision_from_stamp,
)
from app.sandbox.worktree import (
    WorktreeRevisionError,
    WorktreeRevisionMismatch,
    compute_diff,
    verify_worktree_revision,
    worktree_path,
)
from app.safety.review import DIFF_INPUT_MAX_CHARS, run_pipeline
from app.safety.tokens import hash_token as _hash_token


async def _resolve_plan_in_user_org(
    db: AsyncSession, plan_id: uuid.UUID, user
) -> Plan:
    """Return the plan iff it sits in the caller's organisation.

    Raises 404 (not 403) on mismatch so we don't leak plan-id existence
    across tenants. Used by every diffs endpoint that touches a plan_id
    or a submission_id, since submissions inherit their plan's org.
    """
    plan = (
        await db.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="not_found")
    project_org = (
        await db.execute(
            select(Project.organization_id).where(Project.id == plan.project_id)
        )
    ).scalar_one_or_none()
    if not same_org(user, project_org):
        raise HTTPException(status_code=404, detail="not_found")
    return plan


async def _resolve_submission_in_user_org(
    db: AsyncSession, submission_id: uuid.UUID, user
) -> tuple[DiffSubmission, Plan]:
    submission = (
        await db.execute(
            select(DiffSubmission).where(DiffSubmission.id == submission_id)
        )
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="not_found")
    plan = await _resolve_plan_in_user_org(db, submission.plan_id, user)
    return submission, plan

router = APIRouter(tags=["diffs"])


class DiffSubmit(BaseModel):
    plan_id: uuid.UUID
    task_id: str
    # Rejected at ingress (422) so the deterministic Gate-B passes never
    # scan an unbounded string; run_pipeline enforces the same ceiling for
    # its non-HTTP callers.
    diff: str = Field(max_length=DIFF_INPUT_MAX_CHARS)
    test_results: dict[str, Any] | None = None
    self_review_notes: str | None = None


class DiffOut(BaseModel):
    id: uuid.UUID
    plan_id: uuid.UUID
    task_id: str
    status: str
    diff: str
    test_results: dict[str, Any] | None
    self_review_notes: str | None
    auto_review_findings: list[dict[str, Any]] | None
    submitted_at: datetime
    gitlab_mr_iid: int | None
    gitlab_mr_url: str | None
    review_run_id: uuid.UUID | None = None
    review_source_generation: int | None = None
    review_overlay_generation: int | None = None
    review_git_sha: str | None = None


def _out(d: DiffSubmission) -> DiffOut:
    return DiffOut(
        id=d.id,
        plan_id=d.plan_id,
        task_id=d.task_id,
        status=d.status,
        diff=d.diff,
        test_results=d.test_results,
        self_review_notes=d.self_review_notes,
        auto_review_findings=d.auto_review_findings,
        submitted_at=d.submitted_at,
        gitlab_mr_iid=d.gitlab_mr_iid,
        gitlab_mr_url=d.gitlab_mr_url,
        review_run_id=d.review_run_id,
        review_source_generation=d.review_source_generation,
        review_overlay_generation=d.review_overlay_generation,
        review_git_sha=d.review_git_sha,
    )


@router.post("/api/v1/diff_submissions")
async def submit_diff(
    body: DiffSubmit,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_operator),
) -> DiffOut:
    # 6th-round audit C4: previously this endpoint accepted any
    # authenticated user (`CurrentUser`), which let a viewer post
    # arbitrary diffs and burn ultrareview cycles. Diff submission is
    # part of the development workflow (§2.4 — delegated to Claude
    # Code on behalf of an operator), so operator role is the right
    # minimum.
    plan = await _resolve_plan_in_user_org(db, body.plan_id, user)

    try:
        review_stamp = await read_review_graph_stamp(
            db,
            project_id=plan.project_id,
        )
        product_revision = await resolve_review_revision_from_stamp(
            db,
            project_id=plan.project_id,
            stamp=review_stamp,
        )
    except GraphPublicationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "canonical_graph_revision_unavailable",
                "message": "Gate-B review requires a published graph revision",
            },
        ) from exc
    report = await run_pipeline(
        db,
        project_id=plan.project_id,
        plan_id=plan.id,
        diff=body.diff,
        review_revision=product_revision,
    )
    try:
        review_revision = await lock_review_graph_revision(
            db,
            project_id=plan.project_id,
            expected_stamp=review_stamp,
        )
    except GraphGenerationChanged as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "graph_revision_changed_during_review",
                "message": "The graph changed during review; submit again",
            },
        ) from exc
    except GraphPublicationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "canonical_graph_revision_unavailable",
                "message": "Gate-B review requires a published graph revision",
            },
        ) from exc
    try:
        plan, _plan_revision = await lock_current_plan_for_action(
            db,
            plan_id=plan.id,
            project_id=plan.project_id,
        )
    except PlanSourceRevisionError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_source_revision_stale",
                "message": "Recreate the plan against the current graph revision",
            },
        ) from exc
    if plan.status not in {"approved", "executing"}:
        await db.rollback()
        raise HTTPException(status_code=409, detail="plan_not_approved")
    expected_worktree = worktree_path(plan.id)
    try:
        if plan.worktree_path != str(expected_worktree):
            raise WorktreeRevisionMismatch(
                "stored Plan worktree path is not the plan-scoped path"
            )
        await verify_worktree_revision(plan.id, plan.source_git_sha or "")
    except WorktreeRevisionError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_worktree_revision_mismatch",
                "message": "Recreate the Plan worktree at its authorized commit",
            },
        ) from exc
    if await compute_diff(plan.id) != body.diff:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worktree_diff_changed_since_review",
                "message": "Submit and review the current worktree diff again",
            },
        )
    # auto_review_findings is the LIST of findings (each {rule, severity,
    # message, …}) — the shape the model, the GUI and DiffOut all expect.
    # Storing report.as_jsonable() (the whole {verdict, passes, findings}
    # dict) instead made submit_diff 500 on response serialization, since
    # the HTTP path was never exercised end-to-end. Keep just the findings.
    review_findings = [f.as_jsonable() for f in report.findings]
    submission = DiffSubmission(
        plan_id=body.plan_id,
        task_id=body.task_id,
        diff=body.diff,
        test_results=body.test_results,
        self_review_notes=body.self_review_notes,
        auto_review_findings=review_findings,
        status="blocked" if report.verdict == "blocked" else "pending_approval",
    )
    bind_review_revision(submission, review_revision)
    db.add(submission)
    # lock_review_graph_revision holds GraphHead -> AnalysisRun through this
    # commit, so neither source publication nor an overlay writer can move the
    # revision between post-compute validation and verdict persistence.
    await db.commit()
    await db.refresh(submission)
    await audit_record(
        actor=f"user:{user.id}",
        action="diff.submit",
        target=str(submission.id),
        project_id=plan.project_id,
        details={
            "verdict": report.verdict,
            "findings": len(report.findings),
            "by_pass": {p.name: len(p.findings) for p in report.passes},
            "review_run_id": str(review_revision.run_id),
            "review_source_generation": review_revision.source_generation,
            "review_overlay_generation": review_revision.overlay_generation,
            "review_git_sha": review_revision.git_sha,
        },
    )
    return _out(submission)


@router.get("/api/v1/diff_submissions/{submission_id}")
async def get_submission(
    submission_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DiffOut:
    submission, _plan = await _resolve_submission_in_user_org(db, submission_id, user)
    return _out(submission)


class ApproveBody(BaseModel):
    # Spec §2.5: there is no `override=true` switch any more. To approve a
    # diff whose ultrareview verdict is `blocked`, an admin must first
    # POST `/diff_submissions/{id}/break_glass_grant` (which re-runs the
    # pipeline and refuses to issue a grant while the verdict is still
    # blocked), then a *different* user submits the returned token here.
    break_glass_token: str | None = None


@router.post("/api/v1/diff_submissions/{submission_id}/approve")
async def approve_submission(
    submission_id: uuid.UUID,
    body: ApproveBody | None = None,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_operator),
) -> DiffOut:
    # Approval kicks off a GitLab MR — operator role minimum so a
    # viewer can't push code into the source repo.
    submission, plan = await _resolve_submission_in_user_org(db, submission_id, user)
    if plan.worktree_path is None:
        raise HTTPException(status_code=400, detail="plan_or_worktree_missing")

    # Gate B veto (spec §2.5): a diff whose ultrareview verdict was
    # ``blocked`` may only be approved by consuming a break-glass grant.
    # ``status`` is the authoritative signal ``submit_diff`` sets; the
    # ``auto_review_findings`` column holds the *list* of findings, not a
    # ``{"verdict": ...}`` dict, so the old ``findings.get("verdict")``
    # always read None and silently skipped this gate — a blocked diff
    # could be approved with no token. Gate on status instead.
    if submission.status == "blocked":
        token = body.break_glass_token if body else None
        if not token:
            raise HTTPException(
                status_code=409,
                detail=(
                    "blocked_by_review: an admin must issue a break-glass "
                    "grant via /diff_submissions/{id}/break_glass_grant first"
                ),
            )

    try:
        review_revision = await lock_review_graph_revision(
            db,
            project_id=plan.project_id,
        )
    except GraphPublicationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "canonical_graph_revision_unavailable",
                "message": "Approval requires a published graph revision",
            },
        ) from exc

    try:
        plan, _plan_revision = await lock_current_plan_for_action(
            db,
            plan_id=plan.id,
            project_id=plan.project_id,
        )
    except PlanSourceRevisionError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_source_revision_stale",
                "message": "Recreate the plan against the current graph revision",
            },
        ) from exc
    if plan.worktree_path is None:
        await db.rollback()
        raise HTTPException(status_code=400, detail="plan_or_worktree_missing")

    # Refresh under a row lock after taking the canonical graph locks. This
    # prevents concurrent approval/grant issuance from changing status or
    # verdict provenance while the MR side effect is being authorized.
    submission = (
        await db.execute(
            select(DiffSubmission)
            .where(DiffSubmission.id == submission_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if submission.status not in {"blocked", "pending_approval"}:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="submission_not_approvable",
        )
    try:
        require_review_revision(submission, review_revision)
    except ReviewRevisionStale as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "review_graph_revision_stale",
                "message": "Run Gate-B review again against the current graph",
            },
        ) from exc

    consumed_grant: tuple[Any, Any] | None = None
    if submission.status == "blocked":
        # The pre-lock token check above guarantees this is present. Keep the
        # local assertion explicit so type narrowing cannot weaken the gate.
        token = body.break_glass_token if body else None
        if not token:
            await db.rollback()
            raise HTTPException(status_code=409, detail="break_glass_token_required")
        approver_actor = f"user:{user.id}"
        # Single SQL statement enforces:
        #   - matching token (sha256 hash)
        #   - belongs to *this* submission (prevents grant reuse across
        #     submissions in the same org)
        #   - not yet consumed
        #   - not expired
        #   - issued by someone other than the approver (2-eyes)
        # If any condition fails, RETURNING yields no row and we surface
        # a 409 without leaking which condition failed.
        consume = await db.execute(
            update(DiffBreakGlassGrant)
            .where(
                DiffBreakGlassGrant.token_hash == _hash_token(token),
                DiffBreakGlassGrant.submission_id == submission.id,
                DiffBreakGlassGrant.consumed_at.is_(None),
                DiffBreakGlassGrant.expires_at > func.now(),
                DiffBreakGlassGrant.issued_by != approver_actor,
                DiffBreakGlassGrant.review_run_id == review_revision.run_id,
                DiffBreakGlassGrant.review_source_generation
                == review_revision.source_generation,
                DiffBreakGlassGrant.review_overlay_generation
                == review_revision.overlay_generation,
                DiffBreakGlassGrant.review_git_sha == review_revision.git_sha,
            )
            .values(consumed_at=func.now(), consumed_by=approver_actor)
            .returning(
                DiffBreakGlassGrant.id,
                DiffBreakGlassGrant.issued_by,
            )
        )
        row = consume.first()
        if row is None:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="break_glass_invalid_or_stale_or_self_issued_or_expired",
            )
        consumed_grant = (row[0], row[1])

    # The reviewed payload and the files that create_mr_from_worktree will
    # commit must be byte-identical. The Plan row lock serializes this check
    # with MCP edits; a mismatch rolls back an uncommitted grant consumption.
    current_diff = await compute_diff(plan.id)
    if current_diff != submission.diff:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worktree_diff_changed_since_review",
                "message": "Submit and review the current worktree diff again",
            },
        )

    expected_worktree = worktree_path(plan.id)
    try:
        if plan.worktree_path != str(expected_worktree):
            raise WorktreeRevisionMismatch(
                "stored Plan worktree path is not the plan-scoped path"
            )
        await verify_worktree_revision(plan.id, plan.source_git_sha or "")
    except WorktreeRevisionError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "plan_worktree_revision_mismatch",
                "message": "Recreate the Plan worktree at its authorized commit",
            },
        ) from exc

    try:
        mr = await create_mr_from_worktree(
            db,
            project_id=plan.project_id,
            worktree=expected_worktree,
            plan_title=(plan.spec or {}).get("title", "Mnemos change"),
            task_id=submission.task_id,
            description=(plan.spec or {}).get("motivation", ""),
            expected_diff=submission.diff,
        )
    except MRPayloadChanged as exc:
        # The grant update and approval state are still uncommitted here.
        # Rollback makes a late filesystem/index race retryable without
        # consuming break-glass authority or recording a false approval.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "worktree_diff_changed_since_review",
                "message": "Submit and review the current worktree diff again",
            },
        ) from exc

    submission.status = "approved" if mr.ok else "approved_no_mr"
    submission.approved_at = datetime.now(tz=timezone.utc)
    submission.approved_by = f"user:{user.id}"
    submission.gitlab_mr_iid = mr.iid
    submission.gitlab_mr_url = mr.url
    await db.commit()
    await db.refresh(submission)
    if consumed_grant is not None:
        break_glass_grants_total.labels(action="consumed").inc()

    await audit_record(
        actor=f"user:{user.id}",
        action="diff.approve",
        target=str(submission.id),
        project_id=plan.project_id,
        details={
            "mr_iid": mr.iid,
            "mr_url": mr.url,
            "mr_message": mr.message,
            "review_run_id": str(review_revision.run_id),
            "review_source_generation": review_revision.source_generation,
            "review_overlay_generation": review_revision.overlay_generation,
            "review_git_sha": review_revision.git_sha,
        },
    )
    if consumed_grant is not None:
        await audit_record(
            actor=f"user:{user.id}",
            action="diff.break_glass.consume",
            target=str(submission.id),
            project_id=plan.project_id,
            details={
                "grant_id": str(consumed_grant[0]),
                "issued_by": consumed_grant[1],
                "consumed_at": datetime.now(tz=timezone.utc).isoformat(),
                "review_run_id": str(review_revision.run_id),
                "review_source_generation": review_revision.source_generation,
                "review_overlay_generation": review_revision.overlay_generation,
                "review_git_sha": review_revision.git_sha,
            },
        )
    return _out(submission)


@router.post("/api/v1/diff_submissions/{submission_id}/reject")
async def reject_submission(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_operator),
) -> DiffOut:
    submission, _plan = await _resolve_submission_in_user_org(db, submission_id, user)
    submission.status = "rejected"
    submission.approved_at = datetime.now(tz=timezone.utc)
    submission.approved_by = f"user:{user.id}"
    await db.commit()
    await db.refresh(submission)
    await audit_record(
        actor=f"user:{user.id}",
        action="diff.reject",
        target=str(submission.id),
    )
    return _out(submission)


@router.get("/api/v1/diff_submissions")
async def list_submissions_filtered(
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
    verdict: str | None = None,
    limit: int = 100,
) -> list[DiffOut]:
    """Org-scoped diff-submission feed for the dashboard drill-downs (P2-7).

    The 7-round audit's stat-card UX needed an org-wide list (the
    operator clicks "Break-glass active: 5" without knowing which
    plan that 5 belongs to). Filters are intentionally narrow — just
    verdict + a cap — to keep the index path predictable.
    """
    if user.organization_id is None:
        return []
    stmt = (
        select(DiffSubmission)
        .join(Plan, Plan.id == DiffSubmission.plan_id)
        .join(Project, Project.id == Plan.project_id)
        .where(Project.organization_id == user.organization_id)
        .order_by(DiffSubmission.submitted_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if verdict in {"clean", "warn", "blocked"}:
        # PR-137 — ``DiffSubmission`` has no ``verdict`` scalar column;
        # the ultrareview verdict lives inside ``auto_review_findings``
        # JSONB. Filter via the JSON-text path so PG and the SQLite
        # polyglot layer (PR-118) both match — the latter falls back to
        # ``json_extract`` semantics, both surface ``"blocked"`` as the
        # raw string.
        from sqlalchemy import String, cast
        stmt = stmt.where(
            cast(DiffSubmission.auto_review_findings["verdict"], String)
            == f'"{verdict}"'
        )
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(r) for r in rows]


@router.get("/api/v1/plans/{plan_id}/submissions")
async def list_plan_submissions(
    plan_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[DiffOut]:
    # Resolve the parent first: submissions inherit the Plan's project/org
    # boundary, and a bare Plan UUID must never disclose cross-tenant diffs.
    await _resolve_plan_in_user_org(db, plan_id, user)
    rows = (
        await db.execute(
            select(DiffSubmission)
            .where(DiffSubmission.plan_id == plan_id)
            .order_by(DiffSubmission.submitted_at.desc())
        )
    ).scalars().all()
    return [_out(r) for r in rows]
