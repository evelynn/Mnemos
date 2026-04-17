import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.db import get_session
from app.gitlab_client.mr import create_mr_from_worktree
from app.models.plans import DiffSubmission, Plan
from app.safety.self_review import review_diff, to_jsonable

router = APIRouter(tags=["diffs"])


class DiffSubmit(BaseModel):
    plan_id: uuid.UUID
    task_id: str
    diff: str
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
    )


@router.post("/api/v1/diff_submissions")
async def submit_diff(
    body: DiffSubmit,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DiffOut:
    plan = (await db.execute(select(Plan).where(Plan.id == body.plan_id))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")

    review = review_diff(body.diff)
    submission = DiffSubmission(
        plan_id=body.plan_id,
        task_id=body.task_id,
        diff=body.diff,
        test_results=body.test_results,
        self_review_notes=body.self_review_notes,
        auto_review_findings=to_jsonable(review),
    )
    db.add(submission)
    await db.commit()
    await db.refresh(submission)
    await audit_record(
        actor=f"user:{user.id}",
        action="diff.submit",
        target=str(submission.id),
        project_id=plan.project_id,
        details={"findings": len(review.findings)},
    )
    return _out(submission)


@router.get("/api/v1/diff_submissions/{submission_id}")
async def get_submission(
    submission_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DiffOut:
    submission = (
        await db.execute(select(DiffSubmission).where(DiffSubmission.id == submission_id))
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _out(submission)


@router.post("/api/v1/diff_submissions/{submission_id}/approve")
async def approve_submission(
    submission_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DiffOut:
    submission = (
        await db.execute(select(DiffSubmission).where(DiffSubmission.id == submission_id))
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="not_found")
    plan = (
        await db.execute(select(Plan).where(Plan.id == submission.plan_id))
    ).scalar_one_or_none()
    if plan is None or plan.worktree_path is None:
        raise HTTPException(status_code=400, detail="plan_or_worktree_missing")

    mr = await create_mr_from_worktree(
        db,
        project_id=plan.project_id,
        worktree=Path(plan.worktree_path),
        plan_title=(plan.spec or {}).get("title", "Mnemos change"),
        task_id=submission.task_id,
        description=(plan.spec or {}).get("motivation", ""),
    )

    submission.status = "approved" if mr.ok else "approved_no_mr"
    submission.approved_at = datetime.utcnow()
    submission.approved_by = f"user:{user.id}"
    submission.gitlab_mr_iid = mr.iid
    submission.gitlab_mr_url = mr.url
    await db.commit()
    await db.refresh(submission)

    await audit_record(
        actor=f"user:{user.id}",
        action="diff.approve",
        target=str(submission.id),
        project_id=plan.project_id,
        details={"mr_iid": mr.iid, "mr_url": mr.url, "mr_message": mr.message},
    )
    return _out(submission)


@router.post("/api/v1/diff_submissions/{submission_id}/reject")
async def reject_submission(
    submission_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> DiffOut:
    submission = (
        await db.execute(select(DiffSubmission).where(DiffSubmission.id == submission_id))
    ).scalar_one_or_none()
    if submission is None:
        raise HTTPException(status_code=404, detail="not_found")
    submission.status = "rejected"
    submission.approved_at = datetime.utcnow()
    submission.approved_by = f"user:{user.id}"
    await db.commit()
    await db.refresh(submission)
    await audit_record(
        actor=f"user:{user.id}",
        action="diff.reject",
        target=str(submission.id),
    )
    return _out(submission)


@router.get("/api/v1/plans/{plan_id}/submissions")
async def list_plan_submissions(
    plan_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[DiffOut]:
    rows = (
        await db.execute(
            select(DiffSubmission)
            .where(DiffSubmission.plan_id == plan_id)
            .order_by(DiffSubmission.submitted_at.desc())
        )
    ).scalars().all()
    return [_out(r) for r in rows]
