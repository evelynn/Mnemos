"""PR-138 — LLM budget guard.

Real-execution tests for ``app/extractor/cost.py``. The MCP/LLM audit
flagged that token counts are tracked but costs are not capped — a runaway
extractor loop could spend without limit. ``cost.py`` adds a real
DB-backed budget guard; this suite exercises every path against a
polyglot SQLite session (no mocks of the spend logic itself).
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone
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
from app.models.findings import LLMCall, Summary  # noqa: E402
from app.models.projects import Project  # noqa: E402

from app.extractor.cost import (  # noqa: E402
    BudgetExceeded,
    BudgetStatus,
    check_budget,
    project_spend,
    rate_usd_per_mtok,
    require_budget,
    tokens_to_usd,
)


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
        id=_uuid.uuid4(), name="cost-test", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main",
        languages=["python"],
    )
    s.add(p)
    await s.commit()
    return p


async def _add_summary(
    s: AsyncSession, project: Project, tokens: int,
    *, age_sec: int = 0,
) -> Summary:
    when = datetime.now(tz=timezone.utc) - timedelta(seconds=age_sec)
    row = Summary(
        id=_uuid.uuid4(),
        project_id=project.id,
        level=1,
        target_id=f"sym:{_uuid.uuid4().hex[:8]}",
        summary="s", detailed="d",
        claims=[], open_questions=[],
        model_used="claude-sonnet-4-6",
        tokens_used=tokens,
        generated_at=when,
    )
    s.add(row)
    s.add(
        LLMCall(
            id=row.id,
            project_id=project.id,
            analysis_run_id=None,
            target_id=row.target_id,
            level=row.level,
            model_used=row.model_used,
            tokens_used=tokens,
            status="legacy_summary",
            generated_at=when,
        )
    )
    await s.commit()
    return row


# ─── rate + conversion ────────────────────────────────────────────


def test_rate_default_is_three_dollars_per_mtok(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    assert rate_usd_per_mtok() == 3.0


def test_rate_honours_env_override(monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "15.0")
    assert rate_usd_per_mtok() == 15.0


def test_rate_falls_back_on_garbage_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "not-a-number")
    assert rate_usd_per_mtok() == 3.0


def test_tokens_to_usd_at_default_rate(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    # 1 Mtok at $3/Mtok = $3.0000
    assert tokens_to_usd(1_000_000) == 3.0
    # 1.5 Mtok = $4.5000
    assert tokens_to_usd(1_500_000) == 4.5
    # half-cent precision: 100 tokens at $3/Mtok is $0.0003, rounds
    # to 0.0003 (4 dp).
    assert tokens_to_usd(100) == 0.0003


def test_tokens_to_usd_treats_none_as_zero(monkeypatch):
    """Agent-SDK path returns None token count (Claude Code
    subscription billing — not Anthropic API). Avoid double-counting."""
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    assert tokens_to_usd(None) == 0.0
    assert tokens_to_usd(0) == 0.0
    assert tokens_to_usd(-5) == 0.0


# ─── DB-backed spend computation ──────────────────────────────────


@pytest.mark.asyncio
async def test_project_spend_is_zero_for_empty_project(session, monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    project = await _seed_project(session)
    assert await project_spend(session, project.id) == 0.0


@pytest.mark.asyncio
async def test_project_spend_sums_tokens_within_window(
    session, monkeypatch,
):
    """Real DB query: insert summaries, assert sum matches the
    expected USD computation."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "86400")
    project = await _seed_project(session)
    # 1M + 500k + 250k = 1.75M tokens → $5.25.
    await _add_summary(session, project, 1_000_000)
    await _add_summary(session, project, 500_000)
    await _add_summary(session, project, 250_000)
    spent = await project_spend(session, project.id)
    assert spent == pytest.approx(5.25)


@pytest.mark.asyncio
async def test_project_spend_excludes_rows_outside_window(
    session, monkeypatch,
):
    """Spend window is rolling — old rows must not be counted."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")  # 1h
    project = await _seed_project(session)
    await _add_summary(session, project, 1_000_000, age_sec=10)        # in
    await _add_summary(session, project, 1_000_000, age_sec=7200)      # out
    spent = await project_spend(session, project.id)
    # Only the recent 1M tokens count → $3, not $6.
    assert spent == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_project_spend_isolated_per_project(session, monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    a = await _seed_project(session)
    # second project; need a unique gitlab id.
    b = Project(
        id=_uuid.uuid4(), name="other", gitlab_project_id=2,
        gitlab_url="https://y", default_branch="main", languages=[],
    )
    session.add(b)
    await session.commit()
    await _add_summary(session, a, 1_000_000)
    await _add_summary(session, b, 500_000)
    assert await project_spend(session, a.id) == pytest.approx(3.0)
    assert await project_spend(session, b.id) == pytest.approx(1.5)


# ─── budget guard ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_budget_disabled_when_cap_zero(session, monkeypatch):
    """Default ``MNEMOS_LLM_BUDGET_USD_PER_PROJECT=0`` means budget
    is opt-in — ``enabled=False``, ``exceeded=False`` regardless of
    spend."""
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    project = await _seed_project(session)
    await _add_summary(session, project, 10_000_000)  # $30
    status = await check_budget(session, project.id)
    assert isinstance(status, BudgetStatus)
    assert not status.enabled
    assert not status.exceeded


@pytest.mark.asyncio
async def test_disabled_budget_does_not_scan_summary_history(session, monkeypatch):
    import app.extractor.cost as cost

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0")

    async def unexpected_scan(*_args, **_kwargs):
        raise AssertionError("disabled budget queried project_spend")

    monkeypatch.setattr(cost, "project_spend", unexpected_scan)
    status = await cost.check_budget(session, _uuid.uuid4())
    assert status.enabled is False
    assert status.spent_usd == 0.0


@pytest.mark.asyncio
async def test_check_budget_reports_status_when_enabled(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10.0")
    project = await _seed_project(session)
    await _add_summary(session, project, 2_000_000)  # $6 spent
    status = await check_budget(session, project.id)
    assert status.enabled
    assert status.cap_usd == 10.0
    assert status.spent_usd == pytest.approx(6.0)
    assert status.remaining_usd == pytest.approx(4.0)
    assert not status.exceeded


@pytest.mark.asyncio
async def test_require_budget_raises_when_over_cap(session, monkeypatch):
    """The guard hook callers (extractor.runner) use."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "5.0")
    project = await _seed_project(session)
    await _add_summary(session, project, 3_000_000)  # $9 — over $5
    with pytest.raises(BudgetExceeded) as exc:
        await require_budget(session, project.id)
    msg = str(exc.value)
    assert "9.00" in msg
    assert "5.00" in msg
    assert str(project.id) in msg


@pytest.mark.asyncio
async def test_require_budget_returns_status_when_under_cap(
    session, monkeypatch,
):
    """Happy path: under cap, no raise, status returned for caller
    to inspect (logging, headers, etc.)."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10.0")
    project = await _seed_project(session)
    await _add_summary(session, project, 1_000_000)
    status = await require_budget(session, project.id)
    assert status.remaining_usd == pytest.approx(7.0)
