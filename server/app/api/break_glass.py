"""Break-glass grant issuance for blocked diff submissions.

Spec §2.5 ("운영 시스템은 신성하다") requires that the Gate B veto cannot
be skipped by a runtime switch. The legacy `override=true` flag did
exactly that. This module replaces it with a workflow that demands
**both** of:

1. The admin issuing the grant re-runs the ultrareview pipeline against
   the current diff and observes a non-blocked verdict. If the verdict is
   still ``blocked`` the grant is refused — there is no path that allows
   approval while ultrareview says no.
2. The approver who consumes the grant is **not** the admin who issued
   it (initiator ≠ approver). The grant carries a 15-minute TTL and is
   single-use; consumption is enforced atomically by the approve
   endpoint with one ``UPDATE ... RETURNING`` so concurrent consumers
   cannot race.

The raw token is returned to the issuer once. Only its sha256 hash is
stored, so a database leak does not yield usable break-glass tokens.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Re-use the canonical resolver from the diffs router. The previous
# version of this module duplicated `_resolve_submission_in_user_org`
# verbatim, which is the kind of drift Team B flagged — same
# behaviour today, divergent tomorrow.
from app.api.diffs import _resolve_submission_in_user_org
from app.audit.logger import record as audit_record
from app.auth.rbac import require_admin
from app.db import get_session
from app.graph_publication import GraphGenerationChanged, GraphPublicationError
from app.models.auth import User
from app.models.plans import DiffBreakGlassGrant, DiffSubmission
from app.obs.metrics import break_glass_grants_total
from app.plan_provenance import (
    PlanSourceRevisionError,
    lock_current_plan_for_action,
)
from app.review_revision import (
    bind_review_revision,
    lock_review_graph_revision,
    read_review_graph_stamp,
    resolve_review_revision_from_stamp,
)
from app.safety.review import DiffInputTooLarge, run_pipeline
from app.safety.tokens import hash_token

router = APIRouter(tags=["break_glass"])

GRANT_TTL = timedelta(minutes=15)
RATIONALE_MIN_CHARS = 200


# Compatibility alias — keeps `from app.api.break_glass import _hash_token`
# working for the (now copied-down) test suite while the canonical home
# is `app.safety.tokens.hash_token`.
_hash_token = hash_token


class BreakGlassRequest(BaseModel):
    rationale: str = Field(
        ...,
        min_length=RATIONALE_MIN_CHARS,
        # PR-133 — upper bound. The rationale lives in plain text on
        # the grant row; 10 KB is plenty for the longest justification
        # an operator can write while bounding payload size.
        max_length=10_000,
        description=(
            f"Free-form justification ({RATIONALE_MIN_CHARS}+ chars). Stored "
            "in plain text alongside the rerun review payload for audit."
        ),
    )


class ShareContext(BaseModel):
    """Just enough context for the *consumer* to find this submission.

    Does **not** carry the token. The 3rd-round scenario audit
    pointed out that admin → operator hand-off was entirely manual
    (Slack/email) and the operator still had to remember which plan
    the submission belonged to. The token continues to travel
    out-of-band; this struct lets the GUI auto-populate the rest.
    """

    submission_id: uuid.UUID
    plan_id: uuid.UUID
    grant_id_short: str  # sha256 prefix of the token hash (6 chars)


class BreakGlassResponse(BaseModel):
    grant_id: uuid.UUID
    token: str
    expires_at: datetime
    rerun_verdict: str
    # Server-side clock anchor for the client-side countdown. Team B
    # 3rd-round must-fix: `expires_at - issued_at` is the TTL, but the
    # operator may open the dialog several seconds (or minutes) after
    # the response arrives. The client computes
    #     offset = Date.now() - server_now_unix * 1000
    # once and ticks down ``expires_at - (Date.now() - offset)`` so a
    # skewed laptop clock no longer mis-reports the remaining time.
    server_now: datetime
    issued_at: datetime
    share_context: ShareContext


@router.post("/api/v1/diff_submissions/{submission_id}/break_glass_grant")
async def issue_break_glass_grant(
    submission_id: uuid.UUID,
    body: BreakGlassRequest,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> BreakGlassResponse:
    submission, plan = await _resolve_submission_in_user_org(db, submission_id, user)
    if plan.worktree_path is None:
        raise HTTPException(status_code=400, detail="plan_or_worktree_missing")
    if submission.status != "blocked":
        raise HTTPException(status_code=409, detail="submission_not_blocked")

    reviewed_diff = submission.diff
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
                "message": "Break-glass review requires a published graph revision",
            },
        ) from exc

    # Re-run the ultrareview pipeline before granting. The grant is only
    # valid if the *current* diff state passes — we never hand out a
    # bypass for a diff that ultrareview still considers blocked.
    try:
        report = await run_pipeline(
            db,
            project_id=plan.project_id,
            plan_id=plan.id,
            diff=reviewed_diff,
            review_revision=product_revision,
        )
    except DiffInputTooLarge as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "diff_exceeds_input_ceiling",
                "message": "Stored diff exceeds the review input ceiling",
            },
        ) from exc
    if report.coverage != "complete":
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "rerun_review_incomplete",
                "coverage": report.coverage,
                "passes": [
                    p.name for p in report.passes if p.coverage != "complete"
                ],
            },
        )
    if report.verdict == "blocked":
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "rerun_still_blocked",
                "verdict": report.verdict,
                "passes": [p.name for p in report.passes],
            },
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
                "message": "The graph changed during review; rerun break-glass",
            },
        ) from exc
    except GraphPublicationError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "canonical_graph_revision_unavailable",
                "message": "Break-glass review requires a published graph revision",
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

    submission = (
        await db.execute(
            select(DiffSubmission)
            .where(DiffSubmission.id == submission_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if submission.status != "blocked" or submission.diff != reviewed_diff:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "submission_changed_during_review",
                "message": "The diff/status changed; rerun break-glass",
            },
        )

    # Persist the rerun findings in DiffOut's canonical list shape. The grant
    # retains the full report; both rows carry the same exact graph revision.
    rerun_payload: dict[str, Any] = report.as_jsonable()
    submission.auto_review_findings = [
        finding.as_jsonable() for finding in report.findings
    ]
    bind_review_revision(submission, review_revision)

    token = secrets.token_urlsafe(32)
    grant = DiffBreakGlassGrant(
        submission_id=submission.id,
        token_hash=_hash_token(token),
        issued_by=f"user:{user.id}",
        rationale=body.rationale,
        rerun_review_payload=rerun_payload,
        expires_at=datetime.now(tz=timezone.utc) + GRANT_TTL,
    )
    bind_review_revision(grant, review_revision)
    db.add(grant)
    # The canonical GraphHead/AnalysisRun locks remain held until this commit,
    # closing the post-compute publication/overlay race.
    await db.commit()
    await db.refresh(grant)
    break_glass_grants_total.labels(action="issued").inc()

    await audit_record(
        actor=f"user:{user.id}",
        action="diff.break_glass.issue",
        target=str(grant.id),
        project_id=plan.project_id,
        details={
            "submission_id": str(submission.id),
            "rerun_verdict": report.verdict,
            "expires_at": grant.expires_at.isoformat(),
            "review_run_id": str(review_revision.run_id),
            "review_source_generation": review_revision.source_generation,
            "review_overlay_generation": review_revision.overlay_generation,
            "review_git_sha": review_revision.git_sha,
        },
    )

    return BreakGlassResponse(
        grant_id=grant.id,
        token=token,
        expires_at=grant.expires_at,
        rerun_verdict=report.verdict,
        server_now=datetime.now(tz=timezone.utc),
        issued_at=grant.created_at,
        share_context=ShareContext(
            submission_id=submission.id,
            plan_id=plan.id,
            # 6-char prefix is enough to identify the grant in audit
            # logs without exposing anything that could be used to
            # consume it — the full token (32 bytes URL-safe) stays in
            # the response body and goes nowhere else.
            grant_id_short=grant.token_hash[:6],
        ),
    )


# Re-export for tests that want to compute the hash without spinning up
# the API. The diff approve endpoint also uses these.
__all__ = ["router", "_hash_token", "GRANT_TTL", "RATIONALE_MIN_CHARS"]
