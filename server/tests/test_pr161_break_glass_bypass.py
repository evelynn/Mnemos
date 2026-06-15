"""PR-161 — a ``blocked`` diff cannot be approved without a break-glass token.

The approve endpoint gated the Gate-B veto (spec §2.5) on
``submission.auto_review_findings.get("verdict")``. But ``submit_diff`` stores
``auto_review_findings`` as a **list** of findings, not a ``{"verdict": ...}``
dict — so ``isinstance(findings, dict)`` was False, ``verdict`` was always
None, and ``None == "blocked"`` skipped the gate entirely. Any operator could
approve a diff that ultrareview verdict'd ``blocked`` with no token, no
2-eyes, no TTL — the whole break-glass machinery bypassed.

Existing tests missed it because they never drove the real shape through the
endpoint: ``test_diff_break_glass`` hand-built ``auto_review_findings`` as a
``{"verdict": "blocked"}`` dict, and ``test_pr120`` flipped ``status`` by hand
without calling ``approve_submission``. This test runs the real
``submit_diff`` (list-shaped write) then the real ``approve_submission``.

The fix gates on ``submission.status``, the authoritative field submit sets.
"""

from __future__ import annotations

import os
import uuid as _uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr161")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

from app.models.base import Base  # noqa: E402
# Import every model so Base.metadata resolves cross-table FKs at create_all.
from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.auth import User  # noqa: E402
from app.models.plans import DiffSubmission, Plan  # noqa: E402
from app.models.projects import Project  # noqa: E402


def _blocked_report():
    """A real ReviewReport with a critical finding → verdict 'blocked'."""
    from app.safety.review.pipeline import PassResult, ReviewReport
    from app.safety.review.types import Finding

    f = Finding(
        pass_name="rules",
        severity="critical",
        rule="ultrareview.blocked",
        location="x.py:1",
        message="drops dbo.Customers — never approve casually",
    )
    return ReviewReport(
        verdict="blocked",
        passes=[PassResult(name="rules", position=0, findings=[f])],
        findings=[f],
    )


@pytest.mark.asyncio
async def test_blocked_diff_cannot_be_approved_without_break_glass(monkeypatch):
    import app.api.diffs as diffs

    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    # Hermetic: no real audit DB, no real ultrareview, no real GitLab.
    async def _noop_audit(**_kw):
        return None

    async def _fake_pipeline(*_a, **_k):
        return _blocked_report()

    mr_calls = {"n": 0}

    async def _fake_mr(*_a, **_k):
        mr_calls["n"] += 1

        class _MR:
            ok = True
            iid = 1
            url = "http://mr/1"
            message = "ok"

        return _MR()

    monkeypatch.setattr(diffs, "audit_record", _noop_audit)
    monkeypatch.setattr(diffs, "run_pipeline", _fake_pipeline)
    monkeypatch.setattr(diffs, "create_mr_from_worktree", _fake_mr)

    async with Sess() as session:
        org_id = _uuid.uuid4()
        project = Project(
            id=_uuid.uuid4(), name="p", gitlab_project_id=7,
            gitlab_url="http://x", default_branch="main",
            languages=["python"], organization_id=org_id,
        )
        session.add(project)
        plan = Plan(
            id=_uuid.uuid4(), project_id=project.id, status="approved",
            spec={"title": "t"}, tasks=[], requester="r",
            worktree_path="/tmp/wt",
        )
        session.add(plan)
        user = User(
            id=_uuid.uuid4(), username="op-1", password_hash="x",
            role="operator", organization_id=org_id,
        )
        session.add(user)
        await session.commit()

        # Real submit: a blocked pipeline → list-shaped findings + status 'blocked'.
        out = await diffs.submit_diff(
            diffs.DiffSubmit(plan_id=plan.id, task_id="t1", diff="--- a\n+++ b\n"),
            db=session,
            user=user,
        )
        assert out.status == "blocked"
        sub = (
            await session.execute(
                select(DiffSubmission).where(DiffSubmission.id == out.id)
            )
        ).scalar_one()
        # The production shape that defeated the old gate: a list, not a dict.
        assert isinstance(sub.auto_review_findings, list)

        # Approve with NO token must be refused — the Gate-B veto holds.
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await diffs.approve_submission(
                out.id, diffs.ApproveBody(), db=session, user=user
            )
        assert ei.value.status_code == 409
        assert "break_glass" in str(ei.value.detail)

        # Re-read: the diff stayed blocked, never reached MR creation.
        after = (
            await session.execute(
                select(DiffSubmission).where(DiffSubmission.id == out.id)
            )
        ).scalar_one()
        assert after.status == "blocked"
        assert mr_calls["n"] == 0, "a blocked diff must not reach MR creation without a grant"

    await eng.dispose()
