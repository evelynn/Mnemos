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
from sqlalchemy.ext.asyncio import AsyncSession

# Re-use the canonical resolver from the diffs router. The previous
# version of this module duplicated `_resolve_submission_in_user_org`
# verbatim, which is the kind of drift Team B flagged — same
# behaviour today, divergent tomorrow.
from app.api.diffs import _resolve_submission_in_user_org
from app.audit.logger import record as audit_record
from app.auth.rbac import require_admin
from app.db import get_session
from app.models.auth import User
from app.models.plans import DiffBreakGlassGrant
from app.obs.metrics import break_glass_grants_total
from app.safety.review import run_pipeline
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

    # Re-run the ultrareview pipeline before granting. The grant is only
    # valid if the *current* diff state passes — we never hand out a
    # bypass for a diff that ultrareview still considers blocked.
    report = await run_pipeline(
        db,
        project_id=plan.project_id,
        plan_id=plan.id,
        diff=submission.diff,
    )
    if report.verdict == "blocked":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "rerun_still_blocked",
                "verdict": report.verdict,
                "passes": [p.name for p in report.passes],
            },
        )

    # Persist the new ultrareview result on the submission so the audit
    # trail shows what the admin actually saw at issue time.
    rerun_payload: dict[str, Any] = report.as_jsonable()
    submission.auto_review_findings = rerun_payload

    token = secrets.token_urlsafe(32)
    break_glass_grants_total.labels(action="issued").inc()
    grant = DiffBreakGlassGrant(
        submission_id=submission.id,
        token_hash=_hash_token(token),
        issued_by=f"user:{user.id}",
        rationale=body.rationale,
        rerun_review_payload=rerun_payload,
        expires_at=datetime.now(tz=timezone.utc) + GRANT_TTL,
    )
    db.add(grant)
    await db.commit()
    await db.refresh(grant)

    await audit_record(
        actor=f"user:{user.id}",
        action="diff.break_glass.issue",
        target=str(grant.id),
        project_id=plan.project_id,
        details={
            "submission_id": str(submission.id),
            "rerun_verdict": report.verdict,
            "expires_at": grant.expires_at.isoformat(),
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
