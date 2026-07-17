"""Direct Anthropic provider lifecycle at the structured summary boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace

import pytest

from app.extractor.agent import (
    ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS,
    ANTHROPIC_SUMMARY_SYSTEM_PROMPT,
    ANTHROPIC_SUMMARY_WALL_TIMEOUT_SECONDS,
    FALLBACK_ANTHROPIC_CLIENT_INIT,
    FALLBACK_ANTHROPIC_FINISH_REASON,
    FALLBACK_ANTHROPIC_HTTP,
    FALLBACK_ANTHROPIC_JSON,
    FALLBACK_ANTHROPIC_MODEL,
    FALLBACK_ANTHROPIC_OUTPUT_LIMIT,
    FALLBACK_ANTHROPIC_SCHEMA,
    FALLBACK_ANTHROPIC_TIMEOUT,
    FALLBACK_ANTHROPIC_USAGE,
    Extractor,
)
from app.extractor.packing import _approx_tokens
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    ResultStatus,
    UsageSource,
    UsageStatus,
)
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptTicket,
    fingerprint_input,
    use_attempt_callbacks,
)
from app.llm.privacy import enforce_provider_logging_privacy
from app.llm.pricing import OFFICIAL_ANTHROPIC_API_BASE_URL

pytestmark = pytest.mark.usefixtures("fake_llm_attempt_callbacks")


class _APITimeoutError(Exception):
    pass


def test_ambient_anthropic_debug_cannot_log_prompt_material(
    monkeypatch,
    caplog,
) -> None:
    secret = "DIRECT_PROVIDER_PROMPT_SECRET_SENTINEL"
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    caplog.set_level(logging.DEBUG)

    enforce_provider_logging_privacy()
    logging.getLogger("anthropic._base_client").debug(
        "Request options: %s", secret
    )

    assert os.environ["ANTHROPIC_LOG"] == "off"
    assert secret not in caplog.text


def _provider_module(
    client: object,
    client_kwargs: dict | None = None,
) -> ModuleType:
    module = ModuleType("anthropic")

    def build_client(**kwargs):  # noqa: ANN003
        if client_kwargs is not None:
            client_kwargs.update(kwargs)
        return client

    module.AsyncAnthropic = build_client  # type: ignore[attr-defined]
    module.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
    return module


@pytest.mark.asyncio
async def test_direct_anthropic_client_init_failure_never_starts_or_dispatches(
    monkeypatch,
):
    starts = []

    def fail_client(**_kwargs):
        raise ValueError("secret configuration must not escape")

    module = ModuleType("anthropic")
    module.AsyncAnthropic = fail_client  # type: ignore[attr-defined]
    module.APITimeoutError = _APITimeoutError  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", module)
    extractor = Extractor()
    extractor._api_key = "test-key"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://override.invalid")

    async def start(metadata):  # noqa: ANN001
        starts.append(metadata)
        return _ticket(metadata.operation_id)

    async def finish(_ticket, _outcome):  # noqa: ANN001
        raise AssertionError("no attempt can finish without a dispatch")

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    assert starts == []
    assert result.fallback_reason == FALLBACK_ANTHROPIC_CLIENT_INIT
    assert result._llm_call_id is None


def _response(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=" msg_summary_123 ",
        model=" claude-sonnet-4-6 ",
        stop_reason=" end_turn ",
        content=[SimpleNamespace(type="text", text=text)],
        usage={
            "input_tokens": 11,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 5,
        },
    )


def _valid_text() -> str:
    return json.dumps(
        {
            "summary": "Reads the published graph.",
            "detailed": "Uses only the supplied graph evidence.",
            "claims": [],
            "open_questions": [],
        }
    )


def _ticket(operation_id: uuid.UUID) -> AttemptTicket:
    return AttemptTicket(
        attempt_id=uuid.uuid4(),
        operation_id=operation_id,
        budget_scope_id=uuid.uuid4(),
        started_at=datetime.now(timezone.utc),
        remaining_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_direct_anthropic_starts_immediately_before_dispatch_and_finishes_pending(
    monkeypatch,
):
    events: list[object] = []
    starts = []
    finishes = []
    response = _response(_valid_text())
    client_kwargs = {}

    class _Messages:
        async def create(self, **kwargs):  # noqa: ANN003
            events.append("dispatch")
            assert starts
            assert kwargs["system"] == ANTHROPIC_SUMMARY_SYSTEM_PROMPT
            assert kwargs["max_tokens"] == ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS
            return response

    client = SimpleNamespace(messages=_Messages())
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        _provider_module(client, client_kwargs),
    )
    extractor = Extractor()
    extractor._api_key = "test-key"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://override.invalid")

    async def start(metadata):  # noqa: ANN001
        events.append("start")
        starts.append(metadata)
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        events.append("finish")
        finishes.append((ticket, outcome))

    callbacks = AttemptCallbacks(start=start, finish=finish)
    async with use_attempt_callbacks(callbacks):
        result = await extractor.summarize(1, "sym:orders", [])

    assert events == ["start", "dispatch", "finish"]
    assert client_kwargs == {
        "api_key": "test-key",
        "base_url": OFFICIAL_ANTHROPIC_API_BASE_URL,
        "timeout": 60.0,
        "max_retries": 0,
    }
    metadata = starts[0]
    prompt = Extractor._prompt(1, "sym:orders", [])
    assert metadata.attempt_kind == AttemptKind.PRIMARY
    assert metadata.provider == "anthropic"
    assert metadata.provider_mode == "api"
    assert metadata.requested_model == extractor.model
    assert metadata.input_fingerprint == fingerprint_input(
        ANTHROPIC_SUMMARY_SYSTEM_PROMPT,
        prompt,
    )
    assert metadata.estimated_input_tokens == _approx_tokens(
        {"system": ANTHROPIC_SUMMARY_SYSTEM_PROMPT, "prompt": prompt}
    ) + 256
    assert metadata.input_estimate_method == "utf8_json_bytes_upper_v1"
    assert (
        metadata.requested_max_output_tokens
        == ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS
        == 1024
    )
    assert metadata.usage_source == UsageSource.PROVIDER_API

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.COMPLETED
    assert outcome.result_status == ResultStatus.PENDING
    assert outcome.usage.source == UsageSource.PROVIDER_API
    assert outcome.usage.input_tokens == 16
    assert outcome.usage.output_tokens == 5
    assert outcome.usage.total_tokens == 21
    assert outcome.resolved_model == "claude-sonnet-4-6"
    assert outcome.provider_request_id == "msg_summary_123"
    assert outcome.finish_reason == "end_turn"
    assert result.tokens_used == 21
    assert result.model_used == "claude-sonnet-4-6"
    assert extractor._llm_call_id == ticket.attempt_id
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "expected_result", "expected_failure"),
    [
        ("not-json", ResultStatus.PARSE_REJECTED, FALLBACK_ANTHROPIC_JSON),
        (
            json.dumps({"summary": 42}),
            ResultStatus.SCHEMA_REJECTED,
            FALLBACK_ANTHROPIC_SCHEMA,
        ),
    ],
)
async def test_direct_anthropic_records_parse_and_schema_rejections(
    monkeypatch,
    text,
    expected_result,
    expected_failure,
):
    async def create(**_kwargs):
        return _response(text)

    client = SimpleNamespace(
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.COMPLETED
    assert outcome.result_status == expected_result
    assert outcome.failure_code == expected_failure
    assert outcome.usage.total_tokens == 21
    assert result.fallback_reason == expected_failure
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_http_failure_is_lost_after_dispatch(monkeypatch):
    async def fail(**_kwargs):
        raise RuntimeError("provider body must not escape")

    client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.FAILED
    assert outcome.result_status == ResultStatus.NOT_APPLICABLE
    assert outcome.failure_code == FALLBACK_ANTHROPIC_HTTP
    assert outcome.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    assert outcome.usage.source == UsageSource.PROVIDER_API
    assert result.fallback_reason == FALLBACK_ANTHROPIC_HTTP
    assert result.tokens_used is None
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_provider_timeout_has_distinct_terminal_status(
    monkeypatch,
):
    async def fail(**_kwargs):
        raise _APITimeoutError("provider timeout")

    client = SimpleNamespace(messages=SimpleNamespace(create=fail))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.TIMEOUT
    assert outcome.result_status == ResultStatus.NOT_APPLICABLE
    assert outcome.failure_code == FALLBACK_ANTHROPIC_TIMEOUT
    assert outcome.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    assert result.fallback_reason == FALLBACK_ANTHROPIC_TIMEOUT
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_has_absolute_wall_timeout_for_nonreturning_call(
    monkeypatch,
):
    from app.extractor import agent as agent_module

    dispatched = asyncio.Event()

    async def never_returns(**_kwargs):
        dispatched.set()
        await asyncio.sleep(1)

    client = SimpleNamespace(messages=SimpleNamespace(create=never_returns))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    monkeypatch.setattr(
        agent_module,
        "ANTHROPIC_SUMMARY_WALL_TIMEOUT_SECONDS",
        0.01,
    )
    extractor = Extractor()
    extractor._api_key = "test-key"

    result = await asyncio.wait_for(
        extractor.summarize(1, "review:second_opinion", []),
        timeout=0.25,
    )

    assert ANTHROPIC_SUMMARY_WALL_TIMEOUT_SECONDS == 60.0
    assert dispatched.is_set()
    assert result.fallback_reason == FALLBACK_ANTHROPIC_TIMEOUT


@pytest.mark.asyncio
async def test_direct_anthropic_output_limit_is_never_grounding_eligible(
    monkeypatch,
):
    response = _response(_valid_text())
    response.stop_reason = "max_tokens"

    async def create(**_kwargs):
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.COMPLETED
    assert outcome.result_status == ResultStatus.OUTPUT_LIMIT_REJECTED
    assert outcome.failure_code == FALLBACK_ANTHROPIC_OUTPUT_LIMIT
    assert outcome.finish_reason == "max_tokens"
    assert outcome.usage.total_tokens == 21
    assert result.fallback_reason == FALLBACK_ANTHROPIC_OUTPUT_LIMIT
    assert result.tokens_used == 21
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    ["stop_sequence", "tool_use", "pause_turn", "refusal", None],
)
async def test_direct_anthropic_non_end_turn_is_never_grounding_eligible(
    monkeypatch,
    stop_reason,
):
    response = _response(_valid_text())
    response.stop_reason = stop_reason

    async def create(**_kwargs):
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.FAILED
    assert outcome.result_status == ResultStatus.NOT_APPLICABLE
    assert outcome.failure_code == FALLBACK_ANTHROPIC_FINISH_REASON
    assert outcome.finish_reason == stop_reason
    assert outcome.usage.total_tokens == 21
    assert result.fallback_reason == FALLBACK_ANTHROPIC_FINISH_REASON
    assert result.tokens_used == 21
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_missing_resolved_model_fails_closed(monkeypatch):
    response = _response(_valid_text())
    response.model = None

    async def create(**_kwargs):
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.FAILED
    assert outcome.result_status == ResultStatus.NOT_APPLICABLE
    assert outcome.failure_code == FALLBACK_ANTHROPIC_MODEL
    assert outcome.resolved_model is None
    assert outcome.usage.total_tokens == 21
    assert result.fallback_reason == FALLBACK_ANTHROPIC_MODEL
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_invalid_usage_finishes_without_guessing(monkeypatch):
    response = _response(_valid_text())
    response.usage["input_tokens"] = "11"

    async def create(**_kwargs):
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        result = await extractor.summarize(1, "sym:orders", [])

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.COMPLETED
    assert outcome.result_status == ResultStatus.NOT_APPLICABLE
    assert outcome.failure_code == FALLBACK_ANTHROPIC_USAGE
    assert outcome.usage.status == UsageStatus.UNAVAILABLE
    assert outcome.usage.source == UsageSource.PROVIDER_API
    assert result.fallback_reason == FALLBACK_ANTHROPIC_USAGE
    assert result.tokens_used is None
    assert result._llm_call_id == ticket.attempt_id


@pytest.mark.asyncio
async def test_direct_anthropic_start_and_finish_failures_propagate(monkeypatch):
    dispatched = False

    async def create(**_kwargs):
        nonlocal dispatched
        dispatched = True
        return _response(_valid_text())

    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))

    class LifecycleFailure(RuntimeError):
        pass

    async def start_failure(_metadata):  # noqa: ANN001
        raise LifecycleFailure("start")

    async def unreachable_finish(_ticket, _outcome):  # noqa: ANN001
        raise AssertionError("finish cannot run without a ticket")

    extractor = Extractor()
    extractor._api_key = "test-key"
    async with use_attempt_callbacks(
        AttemptCallbacks(start=start_failure, finish=unreachable_finish)
    ):
        with pytest.raises(LifecycleFailure, match="start"):
            await extractor.summarize(1, "sym:orders", [])
    assert dispatched is False

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish_failure(_ticket, _outcome):  # noqa: ANN001
        raise LifecycleFailure("finish")

    extractor = Extractor()
    extractor._api_key = "test-key"
    async with use_attempt_callbacks(
        AttemptCallbacks(start=start, finish=finish_failure)
    ):
        with pytest.raises(LifecycleFailure, match="finish"):
            await extractor.summarize(1, "sym:orders", [])
    assert dispatched is True


@pytest.mark.asyncio
async def test_direct_anthropic_cancellation_remains_owned_by_runner(monkeypatch):
    async def cancel(**_kwargs):
        raise asyncio.CancelledError

    client = SimpleNamespace(messages=SimpleNamespace(create=cancel))
    monkeypatch.setitem(sys.modules, "anthropic", _provider_module(client))
    extractor = Extractor()
    extractor._api_key = "test-key"
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        with pytest.raises(asyncio.CancelledError):
            await extractor.summarize(1, "sym:orders", [])

    assert extractor._llm_call_id is not None
    assert finishes == []
