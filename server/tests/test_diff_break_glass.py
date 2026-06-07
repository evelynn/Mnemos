"""Unit + integration coverage for the break-glass grant workflow.

Replaces the legacy `override=true` path that previously let a single
operator force-approve a `blocked` diff submission (spec §2.5 violation).
The new flow demands:

1. An admin issues a grant. Issuance re-runs the ultrareview pipeline
   and refuses if the verdict is still blocked.
2. The grant carries a 15-minute TTL, is single-use, and is consumed
   atomically.
3. The approver consuming the grant must not be the admin who issued it.
"""

from __future__ import annotations

import uuid

import pytest


# ---------------------------------------------------------------------------
# Pure-function tests (no DB / no FastAPI app required)
# ---------------------------------------------------------------------------


def test_hash_token_is_stable_and_distinct():
    from app.api.break_glass import _hash_token

    a = _hash_token("alpha")
    b = _hash_token("alpha")
    c = _hash_token("beta")
    assert a == b, "hash must be deterministic"
    assert a != c, "different tokens must hash differently"
    assert len(a) == 64, "sha256 hex digest is 64 chars"


def test_break_glass_request_rejects_short_rationale():
    from pydantic import ValidationError

    from app.api.break_glass import BreakGlassRequest, RATIONALE_MIN_CHARS

    # 199 chars — one short of the minimum — must be rejected.
    with pytest.raises(ValidationError):
        BreakGlassRequest(rationale="x" * (RATIONALE_MIN_CHARS - 1))


def test_break_glass_request_accepts_min_length_rationale():
    from app.api.break_glass import BreakGlassRequest, RATIONALE_MIN_CHARS

    obj = BreakGlassRequest(rationale="x" * RATIONALE_MIN_CHARS)
    assert len(obj.rationale) == RATIONALE_MIN_CHARS


def test_grant_ttl_is_fifteen_minutes():
    from app.api.break_glass import GRANT_TTL

    assert GRANT_TTL.total_seconds() == 15 * 60


# ---------------------------------------------------------------------------
# Integration tests — exercise the atomic SQL consumption and 2-eyes rule
# ---------------------------------------------------------------------------


pytestmark_integration = pytest.mark.integration


async def _make_org(db_session, slug: str):
    from app.models.organization import Organization

    org = Organization(slug=slug, display_name=slug.title())
    db_session.add(org)
    await db_session.flush()
    return org


async def _make_user(db_session, role: str, org_id, username: str | None = None):
    from app.auth.passwords import hash_password
    from app.models.auth import User

    user = User(
        username=username or f"{role}-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("pw-break-glass-1234"),
        role=role,
        organization_id=org_id,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_submission(db_session, *, verdict: str):
    """Insert a project/plan/submission triple with a chosen verdict."""
    from app.models.plans import DiffSubmission, Plan
    from app.models.projects import Project

    org = await _make_org(db_session, f"bgorg-{uuid.uuid4().hex[:6]}")
    project = Project(
        name=f"bgproj-{uuid.uuid4().hex[:6]}",
        # gitlab_project_id / gitlab_url / languages are NOT NULL on the
        # projects table (migration 0002); the fixture must supply them.
        gitlab_project_id=uuid.uuid4().int % 1_000_000_000,
        gitlab_url="http://gitlab/bgproj",
        languages=["python"],
        organization_id=org.id,
    )
    db_session.add(project)
    await db_session.flush()
    plan = Plan(
        project_id=project.id,
        status="approved",
        spec={"title": "t"},
        tasks=[],
        requester="user:test",
        worktree_path="/tmp/wt",
    )
    db_session.add(plan)
    await db_session.flush()
    submission = DiffSubmission(
        plan_id=plan.id,
        task_id="t1",
        diff="--- a/x\n+++ b/x\n",
        auto_review_findings={"verdict": verdict, "passes": [], "findings": []},
        status="blocked" if verdict == "blocked" else "pending_approval",
    )
    db_session.add(submission)
    await db_session.flush()
    return org, project, plan, submission


async def _insert_grant(
    db_session,
    *,
    submission_id,
    issued_by: str,
    token: str,
    ttl_minutes: int = 15,
    consumed: bool = False,
):
    from datetime import datetime, timedelta, timezone

    from app.api.break_glass import _hash_token
    from app.models.plans import DiffBreakGlassGrant

    grant = DiffBreakGlassGrant(
        submission_id=submission_id,
        token_hash=_hash_token(token),
        issued_by=issued_by,
        rationale="r" * 200,
        rerun_review_payload={"verdict": "warn"},
        expires_at=datetime.now(tz=timezone.utc) + timedelta(minutes=ttl_minutes),
        consumed_at=(datetime.now(tz=timezone.utc) if consumed else None),
        consumed_by=("user:other" if consumed else None),
    )
    db_session.add(grant)
    await db_session.flush()
    return grant


@pytestmark_integration
async def test_consume_succeeds_for_valid_grant_from_different_issuer(db_session):
    """Approver ≠ issuer + active grant + correct token → row consumed."""
    from sqlalchemy import text

    _, _, _, submission = await _make_submission(db_session, verdict="blocked")
    token = "tok-valid-different-issuer"
    await _insert_grant(
        db_session,
        submission_id=submission.id,
        issued_by="user:admin-1",
        token=token,
    )

    from app.api.break_glass import _hash_token

    res = await db_session.execute(
        text(
            "UPDATE diff_break_glass_grants "
            "SET consumed_at = now(), consumed_by = :approver "
            "WHERE token_hash = :h AND submission_id = :sid "
            "AND consumed_at IS NULL AND expires_at > now() "
            "AND issued_by <> :approver "
            "RETURNING id"
        ),
        {
            "approver": "user:operator-2",
            "h": _hash_token(token),
            "sid": submission.id,
        },
    )
    assert res.first() is not None


@pytestmark_integration
async def test_consume_blocks_when_approver_is_issuer(db_session):
    """initiator ≠ approver: SQL refuses self-issued grants atomically."""
    from sqlalchemy import text

    _, _, _, submission = await _make_submission(db_session, verdict="blocked")
    token = "tok-self-issued"
    actor = "user:same-1"
    await _insert_grant(
        db_session, submission_id=submission.id, issued_by=actor, token=token
    )

    from app.api.break_glass import _hash_token

    res = await db_session.execute(
        text(
            "UPDATE diff_break_glass_grants "
            "SET consumed_at = now(), consumed_by = :approver "
            "WHERE token_hash = :h AND submission_id = :sid "
            "AND consumed_at IS NULL AND expires_at > now() "
            "AND issued_by <> :approver "
            "RETURNING id"
        ),
        {
            "approver": actor,
            "h": _hash_token(token),
            "sid": submission.id,
        },
    )
    assert res.first() is None, "self-issued grant must not consume"


@pytestmark_integration
async def test_consume_blocks_already_consumed_grant(db_session):
    """A grant is single-use — second consume returns no row."""
    from sqlalchemy import text

    _, _, _, submission = await _make_submission(db_session, verdict="blocked")
    token = "tok-already-consumed"
    await _insert_grant(
        db_session,
        submission_id=submission.id,
        issued_by="user:admin",
        token=token,
        consumed=True,
    )

    from app.api.break_glass import _hash_token

    res = await db_session.execute(
        text(
            "UPDATE diff_break_glass_grants "
            "SET consumed_at = now(), consumed_by = :approver "
            "WHERE token_hash = :h AND submission_id = :sid "
            "AND consumed_at IS NULL AND expires_at > now() "
            "AND issued_by <> :approver "
            "RETURNING id"
        ),
        {
            "approver": "user:operator",
            "h": _hash_token(token),
            "sid": submission.id,
        },
    )
    assert res.first() is None


@pytestmark_integration
async def test_consume_blocks_expired_grant(db_session):
    """Past `expires_at` → consume returns no row."""
    from sqlalchemy import text

    _, _, _, submission = await _make_submission(db_session, verdict="blocked")
    token = "tok-expired"
    await _insert_grant(
        db_session,
        submission_id=submission.id,
        issued_by="user:admin",
        token=token,
        ttl_minutes=-5,  # already expired
    )

    from app.api.break_glass import _hash_token

    res = await db_session.execute(
        text(
            "UPDATE diff_break_glass_grants "
            "SET consumed_at = now(), consumed_by = :approver "
            "WHERE token_hash = :h AND submission_id = :sid "
            "AND consumed_at IS NULL AND expires_at > now() "
            "AND issued_by <> :approver "
            "RETURNING id"
        ),
        {
            "approver": "user:operator",
            "h": _hash_token(token),
            "sid": submission.id,
        },
    )
    assert res.first() is None


@pytestmark_integration
async def test_approve_endpoint_rejects_without_token_when_blocked(
    http_client, db_session
):
    """No `override=true` shortcut exists any more — 409 without a token."""
    org, _, _, submission = await _make_submission(db_session, verdict="blocked")
    operator = await _make_user(db_session, "operator", org.id)
    await db_session.commit()
    res = await http_client.post(
        "/api/v1/auth/login",
        json={"username": operator.username, "password": "pw-break-glass-1234"},
    )
    cookies = res.cookies

    r = await http_client.post(
        f"/api/v1/diff_submissions/{submission.id}/approve",
        json={},
        cookies=cookies,
    )
    assert r.status_code == 409
    body = r.json()
    assert "break_glass_grant" in (body.get("detail") or "")
