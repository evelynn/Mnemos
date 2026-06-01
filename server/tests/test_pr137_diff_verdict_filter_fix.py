"""PR-137 — Verify the /diff_submissions verdict filter actually works.

Before this PR ``app/api/diffs.py:317`` referenced ``DiffSubmission.verdict``
— a column that doesn't exist in the model. Any operator clicking the
dashboard's "Break-glass active: N" stat card hit a 500. Fixed by
querying the JSONB ``auto_review_findings["verdict"]`` path.
"""

from __future__ import annotations

import uuid as _uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.plans import DiffSubmission, Plan  # noqa: E402
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


async def _seed(session: AsyncSession) -> tuple[Project, Plan]:
    proj = Project(
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main", languages=["py"],
    )
    plan = Plan(
        id=_uuid.uuid4(), project_id=proj.id, status="approved",
        spec={"title": "t"}, tasks=[], requester="u",
    )
    session.add_all([proj, plan])
    await session.commit()
    return proj, plan


def _make_sub(plan_id, verdict: str) -> DiffSubmission:
    return DiffSubmission(
        id=_uuid.uuid4(), plan_id=plan_id, task_id="t1", status="blocked",
        diff="--- a\n+++ b\n",
        auto_review_findings={"verdict": verdict, "findings": []},
    )


@pytest.mark.asyncio
async def test_verdict_filter_matches_jsonb_path(session):
    """The fix replaces the missing scalar column with a JSONB-path
    query. Both PG and the SQLite polyglot return the value as a JSON
    string (``"blocked"`` including quotes), so the test compiles the
    exact compare the API uses."""
    _, plan = await _seed(session)
    session.add_all([
        _make_sub(plan.id, "blocked"),
        _make_sub(plan.id, "warn"),
        _make_sub(plan.id, "clean"),
        _make_sub(plan.id, "blocked"),
    ])
    await session.commit()

    # Same compare as the patched route in app/api/diffs.py:316-322.
    stmt = select(DiffSubmission).where(
        cast(DiffSubmission.auto_review_findings["verdict"], String)
        == '"blocked"'
    )
    rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 2, f"expected 2 blocked rows, got {len(rows)}"

    stmt = select(DiffSubmission).where(
        cast(DiffSubmission.auto_review_findings["verdict"], String)
        == '"clean"'
    )
    rows = (await session.execute(stmt)).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_verdict_filter_returns_empty_when_no_match(session):
    """Sanity: a verdict with zero rows returns empty, not 500."""
    _, plan = await _seed(session)
    session.add(_make_sub(plan.id, "warn"))
    await session.commit()

    stmt = select(DiffSubmission).where(
        cast(DiffSubmission.auto_review_findings["verdict"], String)
        == '"blocked"'
    )
    rows = (await session.execute(stmt)).scalars().all()
    assert rows == []


def test_diffs_router_uses_jsonb_path_not_missing_column():
    """Forward-guard: ensure no one re-introduces ``DiffSubmission.verdict``.
    The model has no such column (plans.py:47-81); the route must filter
    via the JSONB key path."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "app" / "api" / "diffs.py").read_text(encoding="utf-8")
    # The fix introduces a cast on the JSON key.
    assert 'auto_review_findings["verdict"]' in src, (
        "verdict filter must use the JSONB key path"
    )
    # Forbid the broken pattern.
    assert "DiffSubmission.verdict ==" not in src, (
        "DiffSubmission.verdict scalar column does not exist"
    )
