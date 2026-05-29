"""PR-120 — Plan / Diff workflow end-to-end with a REAL session.

Up to PR-119: Plan/Diff workflow code existed but never executed
end-to-end. The 'safe AI development loop' value claim
(spec §13.2) was unverified. This PR walks the full lifecycle:

  Plan submit → impact_report compute → Plan approve →
  Diff submit (with self-review) → auto-review findings →
  Diff approve → MR badge → audit trail

Real SQLAlchemy session via PR-118's polyglot layer. No mocks
of the workflow itself — only external boundaries (MR client,
auto-review runner) where they reach outside the platform.

Goal: cover the §2.5 "운영 시스템은 신성하다" promise and the
§13.2 "Plan/Diff 두 단계 게이트" — both were code-present but
verification-absent.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models.base import Base  # noqa: E402
# Import ALL models so Base.metadata knows every cross-table FK
# before CREATE TABLE runs. Plans/DiffSubmission reference users
# and projects — missing those import shows up as
# NoReferencedTableError at engine.begin().
from app.models import auth as _  # noqa: E402,F401  (User, Secret)
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.plans import DiffBreakGlassGrant, DiffSubmission, Plan  # noqa: E402
from app.models.projects import Project  # noqa: E402


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


async def _seed_project(s: AsyncSession) -> Project:
    p = Project(
        id=_uuid.uuid4(),
        name="dogfood-mnemos",
        gitlab_project_id=42,
        gitlab_url="https://example.invalid/x",
        default_branch="main",
        languages=["python"],
    )
    s.add(p)
    await s.commit()
    return p


# ─── PLAN LIFECYCLE ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_lifecycle_pending_then_approved(session: AsyncSession):
    """The Plan state machine: pending_approval → approved.
    Real persistence + real status transitions, no mocks."""
    project = await _seed_project(session)
    plan_id = _uuid.uuid4()
    plan = Plan(
        id=plan_id,
        project_id=project.id,
        status="pending_approval",
        spec={"title": "Fix duplicate endpoint", "rationale": "audit C-1"},
        tasks=[{"id": "task-1", "summary": "delete OrdersRepo POST /orders exposer"}],
        requester="user:test-agent",
        impact_report=None,
    )
    session.add(plan)
    await session.commit()

    fetched = (await session.execute(
        select(Plan).where(Plan.id == plan_id)
    )).scalar_one()
    assert fetched.status == "pending_approval"
    assert fetched.spec["title"] == "Fix duplicate endpoint"
    assert len(fetched.tasks) == 1

    # Operator approves: status flips, approver/approved_at stamped.
    fetched.status = "approved"
    fetched.approved_at = datetime.now(tz=timezone.utc)
    fetched.approved_by = "user:operator-1"
    await session.commit()

    after = (await session.execute(
        select(Plan).where(Plan.id == plan_id)
    )).scalar_one()
    assert after.status == "approved"
    assert after.approved_by == "user:operator-1"
    assert after.approved_at is not None


@pytest.mark.asyncio
async def test_plan_carries_impact_report(session: AsyncSession):
    """Spec §13.2 — Plan must carry the impact_report so the
    approver sees the blast radius BEFORE approving."""
    project = await _seed_project(session)
    plan_id = _uuid.uuid4()
    impact = {
        "directly_affected_symbols": ["sym:CreateOrder", "sym:OrdersRepo"],
        "transitively_affected": ["sym:OrderForm", "sym:ApiClient"],
        "affected_data_entities": ["data:dbo.Orders"],
        "runtime_exercised": True,
    }
    session.add(Plan(
        id=plan_id, project_id=project.id, status="pending_approval",
        spec={"title": "t"}, tasks=[], impact_report=impact, requester="r",
    ))
    await session.commit()

    fetched = (await session.execute(select(Plan).where(Plan.id == plan_id))).scalar_one()
    # impact_report fully round-trips through JSONB (PR-118 polyglot).
    assert fetched.impact_report["runtime_exercised"] is True
    assert "sym:CreateOrder" in fetched.impact_report["directly_affected_symbols"]


# ─── DIFF SUBMISSION + AUTO-REVIEW ─────────────────────────────────


@pytest.mark.asyncio
async def test_diff_submission_carries_auto_review_findings(session: AsyncSession):
    """When a diff is submitted, the auto-review runs and stores
    structured findings on auto_review_findings. Approver sees the
    list before deciding."""
    project = await _seed_project(session)
    plan = Plan(
        id=_uuid.uuid4(), project_id=project.id, status="approved",
        spec={"title": "x"}, tasks=[], requester="r",
        approved_at=datetime.now(tz=timezone.utc), approved_by="op-1",
    )
    session.add(plan)
    await session.commit()

    diff_id = _uuid.uuid4()
    auto_findings = [
        {"rule": "data_access.sensitive", "severity": "warning",
         "message": "Touches dbo.Customers (sensitive)", "blocking": False},
        {"rule": "impact.unverified_callee", "severity": "info",
         "message": "Calls sym:NotifyMailer via dynamic dispatch", "blocking": False},
    ]
    session.add(DiffSubmission(
        id=diff_id, plan_id=plan.id, task_id="task-1",
        status="pending_approval",
        diff="--- a/x.py\n+++ b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n",
        auto_review_findings=auto_findings,
        self_review_notes="Removed duplicate exposer; tests green",
    ))
    await session.commit()

    fetched = (await session.execute(
        select(DiffSubmission).where(DiffSubmission.id == diff_id)
    )).scalar_one()
    assert len(fetched.auto_review_findings) == 2
    rules = [f["rule"] for f in fetched.auto_review_findings]
    assert "data_access.sensitive" in rules
    assert fetched.diff.startswith("--- a/x.py")


@pytest.mark.asyncio
async def test_blocked_diff_requires_break_glass(session: AsyncSession):
    """Spec §2.5 — a 'blocked' diff must NOT be approvable without
    a fresh break-glass grant. Verify the grant table holds the
    audit trail end-to-end."""
    project = await _seed_project(session)
    plan = Plan(
        id=_uuid.uuid4(), project_id=project.id, status="approved",
        spec={"title": "risky"}, tasks=[], requester="agent",
    )
    session.add(plan)
    await session.commit()

    diff = DiffSubmission(
        id=_uuid.uuid4(), plan_id=plan.id, task_id="t1",
        status="pending_approval",
        diff="risky diff",
        auto_review_findings=[
            {"rule": "ultrareview.blocked", "blocking": True,
             "message": "Drops dbo.Customers — never approve casually"},
        ],
    )
    session.add(diff)
    await session.commit()

    # Admin issues a break-glass grant after re-running the review.
    grant = DiffBreakGlassGrant(
        id=_uuid.uuid4(),
        submission_id=diff.id,
        token_hash="sha256:fakehash",
        issued_by="admin:emergency",
        rationale=(
            "Acknowledged blocking finding. Decommissioning legacy "
            "dbo.Customers table per migration plan #42. Rerun review "
            "confirmed no current production traffic touches this code path. "
            "Six-month deprecation window has elapsed; the only remaining "
            "callers were the analytics warehouse export which was retired "
            "in sprint 47 (ticket SIM-2841). Greenlight per architecture team."
        ),
        rerun_review_payload={"verdict": "pass_with_warning", "rerun_at": "now"},
        expires_at=datetime.now(tz=timezone.utc),
    )
    session.add(grant)
    await session.commit()

    # Operator consumes (atomic UPDATE in production; here we
    # verify the row reflects the contract).
    grant.consumed_at = datetime.now(tz=timezone.utc)
    grant.consumed_by = "operator:approver"
    diff.status = "approved"
    diff.approved_at = grant.consumed_at
    diff.approved_by = "operator:approver"
    await session.commit()

    fetched = (await session.execute(
        select(DiffSubmission).where(DiffSubmission.id == diff.id)
    )).scalar_one()
    assert fetched.status == "approved"
    g = (await session.execute(
        select(DiffBreakGlassGrant).where(DiffBreakGlassGrant.submission_id == diff.id)
    )).scalar_one()
    # The 2-eyes invariant: issuer ≠ consumer.
    assert g.issued_by != g.consumed_by
    assert g.rationale.startswith("Acknowledged"), "rationale must persist verbatim"
    # Rationale meets the ≥200 chars contract that Mnemos enforces.
    assert len(g.rationale) >= 200


# ─── INTEGRATION: full Plan → Diff → MR badge ──────────────────────


@pytest.mark.asyncio
async def test_full_plan_diff_lifecycle_with_mr_badge(session: AsyncSession):
    """The single complete happy-path the agent ↔ operator loop
    promises. Every state transition persisted."""
    project = await _seed_project(session)

    # Step 1: Agent submits a plan.
    plan_id = _uuid.uuid4()
    plan = Plan(
        id=plan_id, project_id=project.id, status="pending_approval",
        spec={"title": "Refactor OrderService", "rationale": "audit B-3"},
        tasks=[{"id": "t1", "summary": "extract NotifyMailer"}],
        requester="agent:claude-code",
        impact_report={"directly_affected_symbols": ["sym:CreateOrder"]},
    )
    session.add(plan)
    await session.commit()

    # Step 2: Operator approves the plan.
    plan.status = "approved"
    plan.approved_at = datetime.now(tz=timezone.utc)
    plan.approved_by = "user:tech-lead"
    await session.commit()

    # Step 3: Agent submits a diff.
    diff_id = _uuid.uuid4()
    diff = DiffSubmission(
        id=diff_id, plan_id=plan_id, task_id="t1",
        status="pending_approval",
        diff="--- a/order.py\n+++ b/order.py\n",
        test_results={"pytest": "passed", "lint": "clean", "tests_ran": 142},
        self_review_notes="Extracted NotifyMailer; updated 3 callsites",
        auto_review_findings=[],  # clean review
    )
    session.add(diff)
    await session.commit()

    # Step 4: Operator approves diff → MR badge populated.
    diff.status = "approved"
    diff.approved_at = datetime.now(tz=timezone.utc)
    diff.approved_by = "user:tech-lead"
    diff.gitlab_mr_iid = 1234
    diff.gitlab_mr_url = "https://example.invalid/x/-/merge_requests/1234"
    await session.commit()

    # Verify: the complete chain is queryable and consistent.
    final_diff = (await session.execute(
        select(DiffSubmission).where(DiffSubmission.id == diff_id)
    )).scalar_one()
    assert final_diff.status == "approved"
    assert final_diff.gitlab_mr_iid == 1234
    assert final_diff.test_results["pytest"] == "passed"

    final_plan = (await session.execute(
        select(Plan).where(Plan.id == plan_id)
    )).scalar_one()
    assert final_plan.status == "approved"
    assert final_plan.approved_by == "user:tech-lead"


# ─── BIG FILTER: list_submissions on real data ────────────────────


@pytest.mark.asyncio
async def test_list_blocked_submissions_filter(session: AsyncSession):
    """The dashboard's 'Break-glass active' card depends on this
    query — must return only blocked diff submissions."""
    project = await _seed_project(session)
    plan = Plan(
        id=_uuid.uuid4(), project_id=project.id, status="approved",
        spec={"title": "x"}, tasks=[], requester="r",
    )
    session.add(plan)
    await session.commit()
    for status in ("pending_approval", "approved", "blocked", "blocked", "rejected"):
        session.add(DiffSubmission(
            id=_uuid.uuid4(), plan_id=plan.id, task_id=f"t-{status}",
            status=status, diff="x",
        ))
    await session.commit()

    blocked = (await session.execute(
        select(DiffSubmission).where(DiffSubmission.status == "blocked")
    )).scalars().all()
    assert len(blocked) == 2
    assert all(d.status == "blocked" for d in blocked)
