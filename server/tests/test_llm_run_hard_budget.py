"""Hard, provider-independent bounds for optional L1-L3 narration."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from app.extractor.agent import ExtractorResult
from app.extractor.agent_extract import MAX_AGENT_CODE_CHARS, extract_file_via_agent_sdk
from app.extractor.agent_flow import MAX_AGENT_OUTPUT_CHARS as FLOW_OUTPUT_CHARS
from app.extractor.agent_sdk import MAX_AGENT_SUMMARY_OUTPUT_CHARS
from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
from app.extractor.runner import _summarize_with_budget, _unchanged
from app.models.findings import Summary
from app.orchestrator.jobs import _reserve_agent_provider_call


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


def test_agent_extraction_spends_the_same_finite_call_budget() -> None:
    budget = LLMRunBudget(max_calls=1, max_input_tokens=10_000, wall_time_sec=60)
    assert _reserve_agent_provider_call(
        budget,
        language="go",
        file_rel="main.go",
        code="package main\nfunc main() {}\n",
    ) > 0
    with pytest.raises(RunBudgetExceeded, match="run_call_limit_exceeded"):
        _reserve_agent_provider_call(
            budget,
            language="go",
            file_rel="other.go",
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


class _Session:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.commits = 0

    def add(self, value: object) -> None:
        self.added.append(value)

    async def commit(self) -> None:
        self.commits += 1


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
async def test_run_call_cap_stops_before_second_provider_call(monkeypatch) -> None:
    async def _allow(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr("app.extractor.runner.require_budget", _allow)
    session = _Session()
    extractor = _Extractor()
    budget = LLMRunBudget(max_calls=1, max_input_tokens=10_000, wall_time_sec=60)
    evidence = [{"kind": "node", "node_id": "n", "certainty": "asserted"}]

    first = await _summarize_with_budget(
        session, extractor, uuid.uuid4(), 1, "n", evidence,
        run_budget=budget,
    )
    second = await _summarize_with_budget(
        session, extractor, uuid.uuid4(), 1, "n2", evidence,
        run_budget=budget,
    )

    assert first.fallback_reason == ""
    assert second.fallback_reason == "run_call_limit_exceeded"
    assert extractor.calls == 1
    assert budget.stats()["calls_started"] == 1


@pytest.mark.asyncio
async def test_absolute_deadline_cancels_inflight_call_and_ledgers_attempt(
    monkeypatch,
) -> None:
    async def _allow(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    class _SlowExtractor(_Extractor):
        async def summarize(self, _level, _target, _evidence):  # noqa: ANN001
            self.calls += 1
            await asyncio.sleep(1)
            raise AssertionError("deadline did not cancel the provider")

    monkeypatch.setattr("app.extractor.runner.require_budget", _allow)
    session = _Session()
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
    assert len(session.added) == 1
    assert session.commits == 1


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


def test_agent_stream_output_caps_are_bounded() -> None:
    assert MAX_AGENT_CODE_CHARS == 16_000
    assert MAX_AGENT_SUMMARY_OUTPUT_CHARS == 64 * 1024
    assert FLOW_OUTPUT_CHARS == 256 * 1024
