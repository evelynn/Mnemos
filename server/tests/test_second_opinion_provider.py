"""Direct-only provider contract for the Gate-B second opinion."""

from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.extractor.cost import LLMRunBudget as _LLMRunBudget  # noqa: F401
from app.llm.contracts import (
    AttemptStatus,
    LLMPurpose,
    ResultStatus,
    UsageStatus,
)
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptTicket,
    LLMAttemptReplayBlocked,
    SemanticCandidate,
    SemanticCandidateReplay,
    fingerprint_input,
    use_attempt_callbacks,
)
from app.llm.pricing import OFFICIAL_ANTHROPIC_API_BASE_URL
from app.safety.review.second_opinion_provider import (
    FAILURE_HTTP,
    FAILURE_MODEL,
    FAILURE_OUTPUT_LIMIT,
    FAILURE_UNAVAILABLE,
    SECOND_OPINION_MAX_OUTPUT_TOKENS,
    SECOND_OPINION_MODEL,
    SECOND_OPINION_SYSTEM_PROMPT,
    SECOND_OPINION_WALL_TIMEOUT_SECONDS,
    review_via_anthropic,
)
from app.safety.review.second_opinion_schema import SECOND_OPINION_CONTRACT


def _response(
    *,
    model: str = SECOND_OPINION_MODEL,
    stop_reason: str = "end_turn",
    payload: dict | None = None,
):
    text = json.dumps(
        payload
        or {"contract": SECOND_OPINION_CONTRACT, "findings": []}
    )
    return SimpleNamespace(
        id="msg_review_1",
        model=model,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
        content=[SimpleNamespace(type="text", text=text)],
    )


async def _invoke(monkeypatch, *, response=None, provider_error=None):
    init_values: list[dict] = []
    request_values: list[dict] = []

    class Messages:
        async def create(self, **kwargs):
            request_values.append(kwargs)
            if provider_error is not None:
                raise provider_error
            return response

    class FakeAsyncAnthropic:
        def __init__(self, **kwargs):
            init_values.append(kwargs)
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=FakeAsyncAnthropic),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://override.invalid")

    starts = []
    outcomes = []
    candidates: list[SemanticCandidate] = []
    operation_id = uuid.uuid4()
    binding_fingerprint = fingerprint_input(
        "second-opinion-provider-test-binding"
    )
    ticket = AttemptTicket(
        attempt_id=uuid.uuid4(),
        operation_id=operation_id,
        budget_scope_id=uuid.uuid4(),
        started_at=datetime.now(tz=timezone.utc),
        remaining_seconds=60.0,
    )

    async def start(metadata):
        starts.append(metadata)
        return ticket

    async def finish(actual_ticket, outcome):
        assert actual_ticket == ticket
        outcomes.append(outcome)

    async def finish_candidate(actual_ticket, outcome, candidate):
        assert actual_ticket == ticket
        outcomes.append(outcome)
        candidates.append(candidate)

    async def replay_candidate(_request):
        raise AssertionError("fresh provider call must not replay")

    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
    )
    async with use_attempt_callbacks(callbacks):
        result = await review_via_anthropic(
            prompt='{"review":"bounded"}',
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
        )
    if starts:
        assert starts[0].operation_id == operation_id
    if candidates:
        assert candidates[0].binding_fingerprint == binding_fingerprint
    return result, init_values, request_values, starts, outcomes, candidates


@pytest.mark.asyncio
async def test_second_opinion_direct_provider_is_hard_bounded(monkeypatch):
    result, inits, requests, starts, outcomes, candidates = await _invoke(
        monkeypatch,
        response=_response(),
    )

    assert result.complete is True
    assert inits == [
        {
            "api_key": "test-key",
            "base_url": OFFICIAL_ANTHROPIC_API_BASE_URL,
            "timeout": SECOND_OPINION_WALL_TIMEOUT_SECONDS,
            "max_retries": 0,
        }
    ]
    assert requests[0]["model"] == SECOND_OPINION_MODEL
    assert requests[0]["max_tokens"] == SECOND_OPINION_MAX_OUTPUT_TOKENS
    assert requests[0]["system"] == SECOND_OPINION_SYSTEM_PROMPT
    assert starts[0].requested_max_output_tokens == 1_024
    assert starts[0].billing_route == "anthropic_messages_api"
    assert outcomes[0].attempt_status == AttemptStatus.COMPLETED
    assert outcomes[0].result_status == ResultStatus.PENDING
    assert outcomes[0].resolved_model == SECOND_OPINION_MODEL
    assert outcomes[0].usage.total_tokens == 18
    assert candidates[0].contract_name == SECOND_OPINION_CONTRACT
    assert candidates[0].payload == {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": [],
    }


@pytest.mark.asyncio
async def test_second_opinion_provider_minimizes_prose_before_candidate_finish(
    monkeypatch,
) -> None:
    sentinel = "provider-free-prose-sentinel must never persist"
    provider_payload = {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": [
            {
                "severity": "warning",
                "category": "correctness",
                "message": sentinel,
                "evidence": [
                    {
                        "kind": "diff_line",
                        "path": "service.py",
                        "side": "new",
                        "line": 10,
                        "content_sha256": "a" * 64,
                    }
                ],
            }
        ],
    }

    result, *_rest, candidates = await _invoke(
        monkeypatch,
        response=_response(payload=provider_payload),
    )

    serialized = json.dumps(result.payload, sort_keys=True)
    assert sentinel not in serialized
    assert result.payload == candidates[0].payload
    assert "Mnemos candidate signal correctness:" in serialized


@pytest.mark.asyncio
async def test_second_opinion_rejects_resolved_model_mismatch(monkeypatch):
    result, _inits, _requests, _starts, outcomes, candidates = await _invoke(
        monkeypatch,
        response=_response(model="claude-other-model"),
    )

    assert result.failure_code == FAILURE_MODEL
    assert outcomes[0].attempt_status == AttemptStatus.FAILED
    assert outcomes[0].result_status == ResultStatus.NOT_APPLICABLE
    assert candidates == []


@pytest.mark.asyncio
async def test_second_opinion_rejects_provider_output_limit(monkeypatch):
    result, _inits, _requests, _starts, outcomes, candidates = await _invoke(
        monkeypatch,
        response=_response(stop_reason="max_tokens"),
    )

    assert result.failure_code == FAILURE_OUTPUT_LIMIT
    assert outcomes[0].result_status == ResultStatus.OUTPUT_LIMIT_REJECTED
    assert candidates == []


@pytest.mark.asyncio
async def test_second_opinion_provider_exception_text_is_not_logged_or_returned(
    monkeypatch,
    caplog,
):
    sentinel = "raw-provider-prompt-secret-sentinel"
    result, _inits, _requests, _starts, outcomes, candidates = await _invoke(
        monkeypatch,
        response=None,
        provider_error=RuntimeError(sentinel),
    )

    assert result.failure_code == FAILURE_HTTP
    assert outcomes[0].attempt_status == AttemptStatus.FAILED
    assert sentinel not in caplog.text
    assert sentinel not in repr(result)
    assert candidates == []


@pytest.mark.asyncio
async def test_second_opinion_never_falls_back_without_direct_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)

    result = await review_via_anthropic(prompt='{"review":"bounded"}')

    assert result.failure_code == FAILURE_UNAVAILABLE
    assert result.attempt_id is None


@pytest.mark.asyncio
async def test_second_opinion_replays_accepted_candidate_on_blocked_start(
    monkeypatch,
) -> None:
    requests: list[dict] = []

    class Messages:
        async def create(self, **kwargs):
            requests.append(kwargs)
            raise AssertionError("candidate replay must not dispatch")

    class FakeAsyncAnthropic:
        def __init__(self, **_kwargs):
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=FakeAsyncAnthropic),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    operation_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    project_id = uuid.uuid4()
    binding_fingerprint = fingerprint_input(
        "second-opinion-accepted-replay-binding"
    )
    prompt = '{"review":"bounded"}'
    input_fingerprint = fingerprint_input(
        SECOND_OPINION_SYSTEM_PROMPT,
        prompt,
    )

    async def start(_metadata):
        raise LLMAttemptReplayBlocked("stable operation already exists")

    async def finish(_ticket, _outcome):
        raise AssertionError("replay must not finalize transport")

    async def finish_candidate(_ticket, _outcome, _candidate):
        raise AssertionError("replay must not rewrite candidate")

    async def replay_candidate(request):
        assert request.operation_id == operation_id
        assert request.input_fingerprint == input_fingerprint
        assert request.binding_fingerprint == binding_fingerprint
        return SemanticCandidateReplay(
            attempt_id=attempt_id,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SECOND_OPINION,
            input_fingerprint=input_fingerprint,
            contract_name=SECOND_OPINION_CONTRACT,
            binding_fingerprint=binding_fingerprint,
            candidate_status="accepted",
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.ACCEPTED,
            requested_model=SECOND_OPINION_MODEL,
            resolved_model=SECOND_OPINION_MODEL,
            total_tokens=18,
            usage_status=UsageStatus.REPORTED_COMPLETE,
            payload={
                "contract": SECOND_OPINION_CONTRACT,
                "findings": [],
            },
        )

    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
    )
    async with use_attempt_callbacks(callbacks):
        result = await review_via_anthropic(
            prompt=prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
        )

    assert result.complete is True
    assert result.replayed is True
    assert result.attempt_id == attempt_id
    assert requests == []
