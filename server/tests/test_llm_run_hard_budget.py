"""Hard, provider-independent bounds for optional L1-L3 narration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.extractor.agent import ExtractorResult
from app.extractor.agent_extract import (
    MAX_AGENT_CODE_CHARS,
    MAX_AGENT_EXTRACT_OUTPUT_BYTES,
    extract_file_via_agent_sdk,
)
from app.extractor.agent_flow import MAX_AGENT_OUTPUT_BYTES as FLOW_OUTPUT_BYTES
from app.extractor.agent_sdk import (
    AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS,
    AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
    MAX_AGENT_SUMMARY_OUTPUT_BYTES,
)
from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
from app.extractor.runner import _summarize_with_budget, _unchanged
from app.models.findings import LLMCall, Summary
from app.orchestrator.jobs import _reserve_agent_provider_call
from app.testing.llm_adapter import create_llm_ledger_tables


def test_run_budget_enforces_call_and_input_limits() -> None:
    calls = LLMRunBudget(max_calls=1, max_input_tokens=10_000, wall_time_sec=60)
    assert calls.reserve(100) > 0
    with pytest.raises(RunBudgetExceeded, match="run_call_limit_exceeded"):
        calls.reserve(100)
    assert calls.exhausted_reason == "run_call_limit_exceeded"

    tokens = LLMRunBudget(max_calls=10, max_input_tokens=1_000, wall_time_sec=60)
    with pytest.raises(RunBudgetExceeded, match="run_input_token_limit_exceeded"):
        tokens.reserve(1_001)
    assert tokens.calls_started == 0


def test_run_budget_reserves_output_before_dispatch() -> None:
    budget = LLMRunBudget(
        max_calls=3,
        max_input_tokens=10_000,
        max_output_tokens=1_500,
        wall_time_sec=60,
    )

    assert budget.reserve(100, requested_output_tokens=1_200) > 0
    with pytest.raises(RunBudgetExceeded, match="run_output_token_limit_exceeded"):
        budget.reserve(100, requested_output_tokens=301)

    assert budget.calls_started == 1
    assert budget.input_tokens_reserved == 100
    assert budget.output_tokens_reserved == 1_200
    assert budget.stats()["reserved_output_tokens"] == 1_200


def test_unstarted_reservation_can_expand_or_release_but_not_refund_durable() -> None:
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
    )
    budget.reserve(100, requested_output_tokens=1_024)
    budget.expand_unstarted_output(1_024, 128_000)
    assert budget.output_tokens_reserved == 128_000
    budget.release_unstarted(100, 128_000)
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0

    budget.reserve(100, requested_output_tokens=1_024)
    budget._durable_calls_synced = 1
    budget._durable_input_tokens_synced = 100
    budget._durable_output_tokens_synced = 1_024
    with pytest.raises(RuntimeError, match="durably persisted"):
        budget.release_unstarted(100, 1_024)


def test_run_budget_enforces_absolute_deadline(monkeypatch) -> None:
    import app.extractor.cost as cost

    ticks = iter((131.0,))
    monkeypatch.setattr(cost.time, "monotonic", lambda: next(ticks))
    budget = LLMRunBudget(
        max_calls=10,
        max_input_tokens=10_000,
        wall_time_sec=30,
        started_monotonic=100.0,
    )
    with pytest.raises(RunBudgetExceeded, match="run_deadline_exceeded"):
        budget.reserve(100)


def test_agent_extraction_reserves_full_opaque_context_before_dispatch() -> None:
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        wall_time_sec=60,
    )
    assert _reserve_agent_provider_call(
        budget,
        language="go",
        file_rel="main.go",
        code="package main\nfunc main() {}\n",
    ) > 0
    assert budget.input_tokens_reserved == AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS
    assert budget.output_tokens_reserved == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    with pytest.raises(RunBudgetExceeded, match="run_output_token_limit_exceeded"):
        _reserve_agent_provider_call(
            budget,
            language="go",
            file_rel="other.go",
            code="package main\n",
        )


def test_default_run_budget_disables_opaque_agent_sdk_input() -> None:
    budget = LLMRunBudget()
    with pytest.raises(RunBudgetExceeded, match="run_input_token_limit_exceeded"):
        _reserve_agent_provider_call(
            budget,
            language="go",
            file_rel="main.go",
            code="package main\n",
        )


def _summary(
    *,
    fallback_reason: str | None,
    model_used: str,
    claims: list[dict] | None = None,
) -> Summary:
    return Summary(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        target_id="py:m::f",
        level=1,
        summary="summary",
        detailed="detail",
        claims=(
            claims
            if claims is not None
            else [
                {
                    "claim": "The node is present.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": "py:m::f",
                            "certainty": "asserted",
                        }
                    ],
                }
            ]
        ),
        open_questions=[],
        evidence_hash="same",
        model_used=model_used,
        tokens_used=None,
        fallback_reason=fallback_reason,
        generated_at=datetime.now(tz=timezone.utc),
    )


def test_fallback_summary_is_never_a_durable_cache_hit(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.extractor.runner.evidence_hash", lambda _evidence: "same"
    )
    assert not _unchanged(
        _summary(fallback_reason="no_backend", model_used="stub:no_backend"),
        [{"kind": "node"}],
    )
    assert _unchanged(
        _summary(fallback_reason=None, model_used="claude"),
        [{"kind": "node"}],
    )
    assert not _unchanged(
        _summary(fallback_reason=None, model_used="legacy", claims=[]),
        [{"kind": "node"}],
    )


@pytest_asyncio.fixture
async def lifecycle_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await create_llm_ledger_tables(connection)
    session_factory = sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        yield session
    await engine.dispose()


class _Extractor:
    model = "test-model"

    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, _level, _target, _evidence):  # noqa: ANN001
        self.calls += 1
        return ExtractorResult(
            summary="ok",
            detailed="ok",
            claims=[
                {
                    "claim": "The requested node is present.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": "n",
                            "certainty": "asserted",
                        }
                    ],
                }
            ],
            open_questions=[],
            model_used=self.model,
            tokens_used=10,
        )


@pytest.mark.asyncio
async def test_custom_success_without_started_ticket_is_rejected(
    lifecycle_session: AsyncSession,
) -> None:
    session = lifecycle_session
    extractor = _Extractor()
    budget = LLMRunBudget(max_calls=1, max_input_tokens=10_000, wall_time_sec=60)
    evidence = [{"kind": "node", "node_id": "n", "certainty": "asserted"}]

    first = await _summarize_with_budget(
        session, extractor, uuid.uuid4(), 1, "n", evidence,
        run_budget=budget,
    )

    assert first.fallback_reason == "durable_accounting_unavailable"
    assert extractor.calls == 1
    assert budget.stats()["calls_started"] == 0
    assert (await session.execute(select(LLMCall))).scalars().all() == []


@pytest.mark.asyncio
async def test_absolute_deadline_never_invents_a_v0_attempt(
    lifecycle_session: AsyncSession,
) -> None:
    class _SlowExtractor(_Extractor):
        async def summarize(self, _level, _target, _evidence):  # noqa: ANN001
            self.calls += 1
            await asyncio.sleep(1)
            raise AssertionError("deadline did not cancel the provider")

    session = lifecycle_session
    extractor = _SlowExtractor()
    budget = LLMRunBudget(
        max_calls=10,
        max_input_tokens=10_000,
        wall_time_sec=0.02,
    )
    result = await _summarize_with_budget(
        session,
        extractor,
        uuid.uuid4(),
        1,
        "n",
        [{"kind": "node", "node_id": "n", "certainty": "asserted"}],
        analysis_run_id=uuid.uuid4(),
        run_budget=budget,
    )

    assert result.fallback_reason == "run_deadline_exceeded"
    assert budget.exhausted_reason == "run_deadline_exceeded"
    assert (await session.execute(select(LLMCall))).scalars().all() == []


@pytest.mark.asyncio
async def test_agent_extraction_rejects_prefix_only_file_before_provider(
    monkeypatch,
) -> None:
    def _provider_must_not_be_probed() -> bool:
        raise AssertionError("oversized input reached provider discovery")

    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available",
        _provider_must_not_be_probed,
    )
    assert await extract_file_via_agent_sdk(
        language="python",
        file_rel="large.py",
        code="x" * 101,
        max_code_chars=100,
    ) is None


def test_agent_stream_memory_caps_are_distinct_from_provider_token_budget() -> None:
    assert MAX_AGENT_CODE_CHARS == 16_000
    assert MAX_AGENT_SUMMARY_OUTPUT_BYTES == 64 * 1024
    assert MAX_AGENT_EXTRACT_OUTPUT_BYTES == 256 * 1024
    assert FLOW_OUTPUT_BYTES == 256 * 1024
    # Character retention bounds protect process memory.  They do not replace
    # the conservative provider-token reservation required before dispatch.
    assert AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS == 128_000
