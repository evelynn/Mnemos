"""PR-138 — LLM budget guard.

Real-execution tests for ``app/extractor/cost.py``. The MCP/LLM audit
flagged that token counts are tracked but costs are not capped — a runaway
extractor loop could spend without limit. ``cost.py`` adds a real
DB-backed budget guard; this suite exercises every path against a
polyglot SQLite session (no mocks of the spend logic itself).
"""

from __future__ import annotations

import uuid as _uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.dialects import postgresql
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
    _project_budget_exposure_stmt,
    BudgetConfigurationError,
    BudgetExceeded,
    BudgetStatus,
    check_budget,
    project_budget_exposure,
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


async def _add_v1_unknown_call(
    s: AsyncSession,
    project: Project,
    *,
    usage_source: str = "provider_api",
    provider_mode: str | None = None,
    provider: str = "anthropic",
    age_sec: int = 0,
) -> LLMCall:
    when = datetime.now(tz=timezone.utc) - timedelta(seconds=age_sec)
    row = LLMCall(
        id=_uuid.uuid4(),
        project_id=project.id,
        analysis_run_id=None,
        target_id=f"unknown:{_uuid.uuid4().hex[:8]}",
        level=1,
        model_used="claude-sonnet-4-6",
        tokens_used=None,
        status="timeout",
        fallback_reason="provider_timeout",
        generated_at=when,
        contract_version=1,
        operation_id=_uuid.uuid4(),
        budget_scope_id=_uuid.uuid4(),
        purpose="summary",
        attempt_no=1,
        parent_attempt_id=None,
        attempt_kind="primary",
        provider=provider,
        provider_mode=(
            provider_mode
            or ("api" if usage_source == "provider_api" else "subscription")
        ),
        requested_model="claude-sonnet-4-6",
        resolved_model="claude-sonnet-4-6",
        input_fingerprint="a" * 64,
        estimated_input_tokens=100,
        input_estimate_method="test_v1",
        requested_max_output_tokens=50,
        attempt_status="timeout",
        result_status="not_applicable",
        failure_code="provider_timeout",
        started_at=when - timedelta(milliseconds=10),
        completed_at=when,
        duration_ms=10,
        usage_status="lost_after_dispatch",
        usage_source=usage_source,
    )
    s.add(row)
    await s.commit()
    return row


async def _add_v1_reported_cost_call(
    s: AsyncSession,
    project: Project,
) -> LLMCall:
    when = datetime.now(tz=timezone.utc)
    row = LLMCall(
        id=_uuid.uuid4(),
        project_id=project.id,
        analysis_run_id=None,
        target_id=f"reported:{_uuid.uuid4().hex[:8]}",
        level=1,
        model_used="claude-sonnet-4-6",
        tokens_used=1_000_000,
        status="completed",
        generated_at=when,
        contract_version=1,
        operation_id=_uuid.uuid4(),
        budget_scope_id=_uuid.uuid4(),
        purpose="summary",
        attempt_no=1,
        attempt_kind="primary",
        provider="anthropic",
        provider_mode="api",
        requested_model="claude-sonnet-4-6",
        resolved_model="claude-sonnet-4-6",
        input_fingerprint="b" * 64,
        estimated_input_tokens=400_000,
        input_estimate_method="test_v1",
        requested_max_output_tokens=600_000,
        attempt_status="completed",
        result_status="accepted",
        started_at=when - timedelta(milliseconds=10),
        completed_at=when,
        duration_ms=10,
        usage_status="reported_complete",
        usage_source="provider_api",
        input_tokens=400_000,
        output_tokens=600_000,
        total_tokens=1_000_000,
        provider_total_tokens=1_000_000,
        reported_cost_usd=Decimal("0.25"),
    )
    s.add(row)
    await s.commit()
    return row


# ─── rate + conversion ────────────────────────────────────────────


def test_rate_default_is_three_dollars_per_mtok(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    assert rate_usd_per_mtok() == 3.0


def test_rate_honours_env_override(monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "15.0")
    assert rate_usd_per_mtok() == 15.0


@pytest.mark.parametrize("value", ["not-a-number", "-3", "0", "nan", "inf"])
def test_rate_fails_closed_on_unsafe_env(monkeypatch, value):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", value)
    with pytest.raises(BudgetConfigurationError, match="finite positive"):
        rate_usd_per_mtok()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["garbage", "-1", "0", "59"])
async def test_invalid_budget_window_fails_closed(
    session, monkeypatch, value,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", value)
    with pytest.raises(BudgetConfigurationError, match="integer >= 60"):
        await check_budget(session, _uuid.uuid4())


def test_tokens_to_usd_at_default_rate(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_USD_PER_MTOK", raising=False)
    # 1 Mtok at $3/Mtok = $3.0000
    assert tokens_to_usd(1_000_000) == 3.0
    # 1.5 Mtok = $4.5000
    assert tokens_to_usd(1_500_000) == 4.5
    # 100 tokens at $3/Mtok is $0.0003.
    assert tokens_to_usd(100) == 0.0003
    # Guard precision must not round a real sub-$0.0001 exposure to zero.
    assert tokens_to_usd(1) == pytest.approx(0.000003)


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


@pytest.mark.asyncio
async def test_reported_provider_cost_takes_precedence_without_double_count(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    project = await _seed_project(session)
    await _add_v1_reported_cost_call(session, project)

    exposure = await project_budget_exposure(session, project.id)
    assert exposure.known_spend_usd == pytest.approx(0.25)
    assert exposure.budget_exposure_usd == pytest.approx(0.25)
    assert exposure.unknown_provider_calls == 0


def test_budget_exposure_query_compiles_for_postgresql():
    stmt = _project_budget_exposure_stmt(
        _uuid.uuid4(),
        since=datetime.now(tz=timezone.utc) - timedelta(hours=1),
    )
    sql = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "llm_calls.project_id" in sql
    assert "reported_cost_usd" in sql
    assert "CASE WHEN" in sql


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
async def test_tiny_cap_uses_unrounded_internal_exposure(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.000001")
    project = await _seed_project(session)
    await _add_summary(session, project, 1)

    status = await check_budget(session, project.id)
    assert status.spent_usd == pytest.approx(0.000003)
    assert status.budget_exposure_usd == pytest.approx(0.000003)
    assert status.exceeded
    with pytest.raises(BudgetExceeded):
        await require_budget(session, project.id)


@pytest.mark.asyncio
async def test_unknown_provider_usage_fails_closed_even_below_high_cap(
    session, monkeypatch,
):
    """A token proxy cannot prove a model-specific hard-dollar upper bound."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1000.0")
    project = await _seed_project(session)
    await _add_v1_unknown_call(session, project)

    assert await project_spend(session, project.id) == 0.0
    status = await check_budget(session, project.id)
    assert status.spent_usd == 0.0
    assert status.unknown_provider_calls == 1
    assert status.unknown_provider_exposure_usd > 0.0
    assert status.remaining_usd == 0.0
    assert status.exceeded
    with pytest.raises(BudgetExceeded, match="indeterminate"):
        await require_budget(session, project.id)


@pytest.mark.asyncio
async def test_unattested_agent_sdk_unknown_usage_fails_closed(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.000001")
    project = await _seed_project(session)
    await _add_v1_unknown_call(session, project, usage_source="agent_sdk")

    status = await check_budget(session, project.id)
    assert status.unknown_provider_calls == 1
    assert status.unknown_provider_exposure_usd > 0.0
    assert status.exceeded


@pytest.mark.asyncio
async def test_agent_sdk_zero_cost_estimate_cannot_authorize_budget(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.000001")
    project = await _seed_project(session)
    row = await _add_v1_reported_cost_call(session, project)
    row.provider_mode = "unknown"
    row.usage_source = "agent_sdk"
    row.reported_cost_usd = 0.0
    await session.commit()

    exposure = await project_budget_exposure(session, project.id)
    assert exposure.known_spend_usd == pytest.approx(3.0)
    assert exposure.unknown_provider_calls == 1
    assert exposure.unknown_provider_exposure_usd == pytest.approx(3.0)
    assert exposure.budget_exposure_usd == pytest.approx(6.0)
    status = await check_budget(session, project.id)
    assert status.exceeded


@pytest.mark.asyncio
async def test_legacy_row_is_billable_even_with_subscription_labels(
    session, monkeypatch,
):
    """Legacy subscription labels do not authorize free-call treatment."""
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    project = await _seed_project(session)
    summary = await _add_summary(session, project, 1_000_000)
    row = await session.get(LLMCall, summary.id)
    assert row is not None
    row.provider_mode = "subscription"
    row.usage_source = "agent_sdk"
    await session.commit()

    exposure = await project_budget_exposure(session, project.id)
    assert exposure.known_spend_usd == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_legacy_opaque_agent_call_is_cost_indeterminate(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1000.0")
    project = await _seed_project(session)
    summary = await _add_summary(session, project, 1)
    row = await session.get(LLMCall, summary.id)
    assert row is not None
    row.model_used = "claude_code:claude-sonnet-4-6"
    row.tokens_used = None
    await session.commit()

    exposure = await project_budget_exposure(session, project.id)
    assert exposure.unknown_provider_calls == 1
    status = await check_budget(session, project.id)
    assert status.exceeded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "provider_mode", "usage_source"),
    [
        ("anthropic", "api", "agent_sdk"),
        ("anthropic", "subscription", "provider_api"),
        ("openai", "subscription", "agent_sdk"),
    ],
)
async def test_subscription_metadata_mismatch_with_unknown_usage_fails_closed(
    session, monkeypatch, provider, provider_mode, usage_source,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1000.0")
    project = await _seed_project(session)
    await _add_v1_unknown_call(
        session,
        project,
        provider=provider,
        provider_mode=provider_mode,
        usage_source=usage_source,
    )

    status = await check_budget(session, project.id)
    assert status.unknown_provider_calls == 1
    assert status.exceeded
    with pytest.raises(BudgetExceeded, match="indeterminate"):
        await require_budget(session, project.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "provider",
        "provider_mode",
        "usage_source",
        "expected_spend",
        "expected_unknown",
    ),
    [
        ("anthropic", "api", "agent_sdk", 3.0, 1),
        ("anthropic", "subscription", "provider_api", 0.25, 0),
        ("openai", "subscription", "agent_sdk", 3.0, 1),
    ],
)
async def test_subscription_metadata_mismatch_with_known_cost_is_billable(
    session,
    monkeypatch,
    provider,
    provider_mode,
    usage_source,
    expected_spend,
    expected_unknown,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.1")
    project = await _seed_project(session)
    row = await _add_v1_reported_cost_call(session, project)
    row.provider = provider
    row.provider_mode = provider_mode
    row.usage_source = usage_source
    await session.commit()

    exposure = await project_budget_exposure(session, project.id)
    assert exposure.known_spend_usd == pytest.approx(expected_spend)
    assert exposure.unknown_provider_calls == expected_unknown
    status = await check_budget(session, project.id)
    assert status.exceeded
    with pytest.raises(BudgetExceeded):
        await require_budget(session, project.id)


@pytest.mark.asyncio
async def test_old_unknown_provider_usage_leaves_rolling_window(
    session, monkeypatch,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project = await _seed_project(session)
    await _add_v1_unknown_call(session, project, age_sec=7200)

    status = await check_budget(session, project.id)
    assert status.unknown_provider_calls == 0
    assert not status.exceeded


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["garbage", "-1", "nan", "inf"])
async def test_invalid_explicit_project_cap_fails_closed(
    session, monkeypatch, value,
):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", value)
    with pytest.raises(BudgetConfigurationError, match="finite"):
        await check_budget(session, _uuid.uuid4())


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
