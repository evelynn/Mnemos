"""E2 coverage for the durable Chat physical-attempt boundary."""

from __future__ import annotations

import asyncio
import sys
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import chat as chat_api
from app.api import llm_providers as providers
from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
from app.llm.contracts import LLMPurpose, ResultStatus
from app.llm.lifecycle import (
    PRICE_ATTESTATION_MODEL_MISMATCH,
    LLMSemanticCandidateUnavailable,
    use_attempt_callbacks,
)
from app.models.findings import LLMCall, LLMBudgetScope, LLMProjectBudgetAccount
from app.models.llm import LLMSemanticCandidate


async def _accounting_store(monkeypatch):  # noqa: ANN001
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(chat_api.app_db, "SessionLocal", factory)
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1000")
    return engine, factory


def _budget() -> LLMRunBudget:
    return LLMRunBudget(
        max_calls=4,
        max_input_tokens=120_000,
        max_output_tokens=4_800,
        wall_time_sec=60,
    )


def test_chat_semantic_codec_rejects_invalid_structure_before_dispatch():
    with pytest.raises(
        providers.ChatSemanticContractError,
        match="contract name",
    ):
        providers.ChatSemanticCodec(
            contract_name="INVALID CONTRACT",
            binding_fingerprint="b" * 64,
            normalize_response=lambda text: {"reply": text},
            normalizer=lambda payload: payload,
            render_candidate=lambda payload: str(payload),
            parse_failure_code="chat_parse_rejected",
            schema_failure_code="chat_schema_rejected",
        )


@pytest.mark.asyncio
async def test_openai_started_is_committed_before_dispatch_and_usage_is_normalized(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    state = providers.ChatProviderAttemptState()
    observed_started: list[tuple[str, str, int]] = []

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        async with factory() as check:
            row = (await check.execute(select(LLMCall))).scalar_one()
            scope = (await check.execute(select(LLMBudgetScope))).scalar_one()
            observed_started.append(
                (row.attempt_status, row.result_status, scope.calls_reserved)
            )
        return {
            "id": "chatcmpl-test",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "grounded answer"},
                }
            ],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            },
        }

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    callbacks = chat_api._chat_attempt_callbacks(
        project_id=project_id,
        purpose=LLMPurpose.CHAT_ANSWER,
        run_budget=budget,
    )
    async with use_attempt_callbacks(callbacks):
        reply = await providers.provider_chat(
            "openai",
            {
                "openai": {
                    "api_key": "test",
                    "model": "gpt-4o-2024-11-20",
                    "base_url": "https://api.openai.com/v1",
                }
            },
            system="system",
            prompt="bounded grounded prompt",
            max_output_tokens=1_200,
            run_budget=budget,
            operation_id=chat_api._chat_operation_id(
                project_id, LLMPurpose.CHAT_ANSWER
            ),
            attempt_state=state,
        )

    assert reply == "grounded answer"
    assert observed_started == [("started", "pending", 1)]
    assert state.attempt_id is not None
    await chat_api._classify_chat_candidate(
        state.attempt_id,
        result_status=ResultStatus.ACCEPTED,
    )
    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
        scope = (await check.execute(select(LLMBudgetScope))).scalar_one()
    assert row.purpose == "chat_answer"
    assert row.provider == "openai"
    assert row.provider_mode == "api"
    assert row.attempt_status == "completed"
    assert row.result_status == "accepted"
    assert row.provider_request_id == "chatcmpl-test"
    assert row.resolved_model == "gpt-4o-2024-11-20"
    assert row.input_tokens == 11
    assert row.output_tokens == 7
    assert row.total_tokens == 18
    assert scope.id == budget.scope_id
    assert scope.calls_reserved == 1
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "expected_provider", "expected_input", "expected_output"),
    [
        ("gemini", "gemini", 13, 6),
        ("claudecode", "anthropic", 17, 9),
    ],
)
async def test_gemini_and_anthropic_usage_survive_the_durable_boundary(
    monkeypatch,
    provider,
    expected_provider,
    expected_input,
    expected_output,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    state = providers.ChatProviderAttemptState()

    if provider == "gemini":
        async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
            return {
                "responseId": "gemini-id",
                "modelVersion": "gemini-2.5-flash",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "answer"}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 13,
                    "candidatesTokenCount": 6,
                    "totalTokenCount": 19,
                },
            }

        monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
        cfg = {"gemini": {"api_key": "test", "model": "gemini-2.5-flash"}}
    else:
        class Messages:
            async def create(self, **_kwargs):  # noqa: ANN003
                return SimpleNamespace(
                    id="anthropic-id",
                    model="claude-sonnet-4-6",
                    stop_reason="end_turn",
                    content=[SimpleNamespace(type="text", text="answer")],
                    usage=SimpleNamespace(input_tokens=17, output_tokens=9),
                )

        class AsyncAnthropic:
            def __init__(self, **_kwargs):  # noqa: ANN003
                self.messages = Messages()

        monkeypatch.setitem(
            sys.modules,
            "anthropic",
            SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
        )
        cfg = {
            "claudecode": {
                "api_key": "test",
                "model": "claude-sonnet-4-6",
                "mode": "api",
            }
        }

    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=budget,
        )
    ):
        reply = await providers.provider_chat(
            provider,
            cfg,
            system="system",
            prompt="prompt",
            max_output_tokens=1_200,
            run_budget=budget,
            operation_id=chat_api._chat_operation_id(
                project_id, LLMPurpose.CHAT_ANSWER
            ),
            attempt_state=state,
        )
    assert reply == "answer"
    assert state.attempt_id is not None
    await chat_api._classify_chat_candidate(
        state.attempt_id,
        result_status=ResultStatus.ACCEPTED,
    )
    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
    assert row.provider == expected_provider
    assert row.input_tokens == expected_input
    assert row.output_tokens == expected_output
    assert row.total_tokens == expected_input + expected_output
    assert row.result_status == "accepted"
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini", "claudecode"])
async def test_production_adapter_rejects_resolved_model_drift(
    monkeypatch,
    provider,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    state = providers.ChatProviderAttemptState()

    if provider == "openai":
        async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
            return {"id": "drift", "model": "gpt-4o", "choices": []}

        monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
        cfg = {
            "openai": {
                "api_key": "test",
                "model": "gpt-4o-2024-11-20",
                "base_url": "https://api.openai.com/v1",
            }
        }
    elif provider == "gemini":
        async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
            return {
                "responseId": "drift",
                "modelVersion": "models/gemini-2.5-pro",
                "candidates": [],
            }

        monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
        cfg = {"gemini": {"api_key": "test", "model": "gemini-2.5-flash"}}
    else:
        class Messages:
            async def create(self, **_kwargs):  # noqa: ANN003
                return SimpleNamespace(
                    id="drift",
                    model="claude-opus-4-6",
                    stop_reason="end_turn",
                    content=[],
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                )

        class AsyncAnthropic:
            def __init__(self, **_kwargs):  # noqa: ANN003
                self.messages = Messages()

        monkeypatch.setitem(
            sys.modules,
            "anthropic",
            SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
        )
        cfg = {
            "claudecode": {
                "api_key": "test",
                "model": "claude-sonnet-4-6",
                "mode": "api",
            }
        }

    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=budget,
        )
    ):
        reply = await providers.provider_chat(
            provider,
            cfg,
            system="system",
            prompt="exact model required",
            max_output_tokens=1_200,
            run_budget=budget,
            operation_id=chat_api._chat_operation_id(
                project_id, LLMPurpose.CHAT_ANSWER
            ),
            attempt_state=state,
        )

    assert reply is None
    assert state.attempt_id is not None
    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
    assert row.result_status == ResultStatus.NOT_APPLICABLE.value
    assert row.failure_code == PRICE_ATTESTATION_MODEL_MISMATCH
    await engine.dispose()


@pytest.mark.asyncio
async def test_rewrite_and_answer_use_distinct_stable_operations_in_one_scope(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    payloads = iter(
        [
            {
                "id": "rewrite-id",
                "model": "gpt-4o-2024-11-20",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": '["auth", "token", "session"]'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 8,
                    "completion_tokens": 4,
                    "total_tokens": 12,
                },
            },
            {
                "id": "answer-id",
                "model": "gpt-4o-2024-11-20",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "answer"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ]
    )

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return next(payloads)

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    cfg = {
        "openai": {
            "api_key": "test",
            "model": "gpt-4o-2024-11-20",
            "base_url": "https://api.openai.com/v1",
        }
    }
    states: list[providers.ChatProviderAttemptState] = []
    for purpose, output_cap in (
        (LLMPurpose.CHAT_REWRITE, 128),
        (LLMPurpose.CHAT_ANSWER, 1_200),
    ):
        state = providers.ChatProviderAttemptState()
        states.append(state)
        async with use_attempt_callbacks(
            chat_api._chat_attempt_callbacks(
                project_id=project_id,
                purpose=purpose,
                run_budget=budget,
            )
        ):
            reply = await providers.provider_chat(
                "openai",
                cfg,
                system="system",
                prompt=purpose.value,
                max_output_tokens=output_cap,
                run_budget=budget,
                operation_id=chat_api._chat_operation_id(project_id, purpose),
                attempt_state=state,
            )
        assert reply
        assert state.attempt_id is not None
        await chat_api._classify_chat_candidate(
            state.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )

    async with factory() as check:
        rows = (
            await check.execute(select(LLMCall).order_by(LLMCall.started_at))
        ).scalars().all()
        scope = (await check.execute(select(LLMBudgetScope))).scalar_one()
    assert [row.purpose for row in rows] == ["chat_rewrite", "chat_answer"]
    assert len({row.operation_id for row in rows}) == 2
    assert rows[0].operation_id == providers._bound_provider_operation_id(
        chat_api._chat_operation_id(project_id, LLMPurpose.CHAT_REWRITE),
        provider="openai",
        model="gpt-4o-2024-11-20",
        route_fingerprint=providers.fingerprint_input(
            "mnemos.chat.provider-route.v2",
            "openai",
            "https://api.openai.com/v1",
        ),
        requested_max_output_tokens=128,
        system="system",
        prompt=LLMPurpose.CHAT_REWRITE.value,
    )
    assert rows[1].operation_id == providers._bound_provider_operation_id(
        chat_api._chat_operation_id(project_id, LLMPurpose.CHAT_ANSWER),
        provider="openai",
        model="gpt-4o-2024-11-20",
        route_fingerprint=providers.fingerprint_input(
            "mnemos.chat.provider-route.v2",
            "openai",
            "https://api.openai.com/v1",
        ),
        requested_max_output_tokens=1_200,
        system="system",
        prompt=LLMPurpose.CHAT_ANSWER.value,
    )
    assert scope.calls_reserved == 2
    assert scope.output_tokens_reserved == 1_328
    await engine.dispose()


@pytest.mark.asyncio
async def test_same_exact_chat_retry_replays_candidate_without_second_dispatch(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    dispatches = 0

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal dispatches
        dispatches += 1
        return {
            "id": "stable-retry-id",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "one paid answer"},
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 4,
                "total_tokens": 13,
            },
        }

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    cfg = {
        "openai": {
            "api_key": "test",
            "model": "gpt-4o-2024-11-20",
            "base_url": "https://api.openai.com/v1",
        }
    }
    operation_id = chat_api._chat_operation_id(
        project_id,
        LLMPurpose.CHAT_ANSWER,
    )

    first_budget = _budget()
    first_state = providers.ChatProviderAttemptState()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=first_budget,
        )
    ):
        reply = await providers.provider_chat(
            "openai",
            cfg,
            system="stable system",
            prompt="stable exact prompt",
            max_output_tokens=1_200,
            run_budget=first_budget,
            operation_id=operation_id,
            attempt_state=first_state,
        )
    assert reply == "one paid answer"
    assert first_state.attempt_id is not None
    await chat_api._classify_chat_candidate(
        first_state.attempt_id,
        result_status=ResultStatus.ACCEPTED,
    )
    # A stored exact candidate remains replayable after an operator disables
    # new paid dispatch. The lifecycle checks duplicates before local policy.
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    second_budget = _budget()
    second_state = providers.ChatProviderAttemptState()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=second_budget,
        )
    ):
        replayed = await providers.provider_chat(
            "openai",
            cfg,
            system="stable system",
            prompt="stable exact prompt",
            max_output_tokens=1_200,
            run_budget=second_budget,
            operation_id=operation_id,
            attempt_state=second_state,
        )

    async with factory() as check:
        rows = (await check.execute(select(LLMCall))).scalars().all()
        candidates = (
            await check.execute(select(LLMSemanticCandidate))
        ).scalars().all()
    assert replayed == "one paid answer"
    assert dispatches == 1
    assert len(rows) == 1
    assert len(candidates) == 1
    assert rows[0].result_status == ResultStatus.ACCEPTED.value
    assert candidates[0].candidate_status == "accepted"
    assert second_state.attempt_id == first_state.attempt_id
    assert second_budget.exhausted_reason is None
    assert second_state.consumer_result_status == ResultStatus.ACCEPTED
    assert second_budget.calls_started == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_route_and_output_cap_changes_never_replay_old_candidate(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    dispatches = 0

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal dispatches
        dispatches += 1
        return {
            "id": f"route-cap-{dispatches}",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "short answer"}}
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        }

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    operation_id = chat_api._chat_operation_id(
        project_id,
        LLMPurpose.CHAT_ANSWER,
    )
    for base_url, output_cap in (
        ("https://api.openai.com/v1", 1_200),
        ("https://api.openai.com/v1", 128),
    ):
        budget = _budget()
        async with use_attempt_callbacks(
            chat_api._chat_attempt_callbacks(
                project_id=project_id,
                purpose=LLMPurpose.CHAT_ANSWER,
                run_budget=budget,
            )
        ):
            reply = await providers.provider_chat(
                "openai",
                {
                    "openai": {
                        "api_key": "test",
                        "model": "gpt-4o-2024-11-20",
                        "base_url": base_url,
                    }
                },
                system="stable system",
                prompt="same logical prompt",
                max_output_tokens=output_cap,
                run_budget=budget,
                operation_id=operation_id,
            )
        assert reply == "short answer"

    denied_routes = (
        "https://compatible.example.invalid/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v2",
        "https://api.openai.com/v1/",
        "https://api.openai.com/v1?route=proxy",
        "https://api.openai.com/v1#fragment",
    )
    for denied_base_url in denied_routes:
        budget = _budget()
        async with use_attempt_callbacks(
            chat_api._chat_attempt_callbacks(
                project_id=project_id,
                purpose=LLMPurpose.CHAT_ANSWER,
                run_budget=budget,
            )
        ):
            with pytest.raises(
                RunBudgetExceeded,
                match="project_dollar_price_contract_unavailable",
            ):
                await providers.provider_chat(
                    "openai",
                    {
                        "openai": {
                            "api_key": "test",
                            "model": "gpt-4o-2024-11-20",
                            "base_url": denied_base_url,
                        }
                    },
                    system="stable system",
                    prompt="same logical prompt",
                    max_output_tokens=128,
                    run_budget=budget,
                    operation_id=operation_id,
                )

    async with factory() as check:
        rows = (await check.execute(select(LLMCall))).scalars().all()
        candidates = (
            await check.execute(select(LLMSemanticCandidate))
        ).scalars().all()
    assert dispatches == 2
    assert len(rows) == 2
    assert len({row.operation_id for row in rows}) == 2
    assert len(candidates) == 2
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_crash_after_terminal_candidate_replays_pending_product(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    dispatches = 0

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal dispatches
        dispatches += 1
        return {
            "id": "pending-crash-id",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": "recoverable grounded answer"},
                }
            ],
            "usage": {
                "prompt_tokens": 9,
                "completion_tokens": 5,
                "total_tokens": 14,
            },
        }

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    cfg = {
        "openai": {
            "api_key": "test",
            "model": "gpt-4o-2024-11-20",
            "base_url": "https://api.openai.com/v1",
        }
    }
    operation_id = chat_api._chat_operation_id(
        project_id,
        LLMPurpose.CHAT_ANSWER,
    )
    codec = providers.chat_answer_semantic_codec(
        str(project_id),
        LLMPurpose.CHAT_ANSWER.value,
        "stable system",
        "stable exact prompt",
    )

    first_budget = _budget()
    first_state = providers.ChatProviderAttemptState()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=first_budget,
        )
    ):
        first = await providers.provider_chat(
            "openai",
            cfg,
            system="stable system",
            prompt="stable exact prompt",
            max_output_tokens=1_200,
            run_budget=first_budget,
            operation_id=operation_id,
            attempt_state=first_state,
            semantic_codec=codec,
        )
    assert first == "recoverable grounded answer"
    assert first_state.consumer_result_status == ResultStatus.PENDING

    # Simulate a process crash before the Chat consumer classifies/publishes.
    retry_budget = _budget()
    retry_state = providers.ChatProviderAttemptState()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=retry_budget,
        )
    ):
        replayed = await providers.provider_chat(
            "openai",
            cfg,
            system="stable system",
            prompt="stable exact prompt",
            max_output_tokens=1_200,
            run_budget=retry_budget,
            operation_id=operation_id,
            attempt_state=retry_state,
            semantic_codec=codec,
        )

    assert replayed == "recoverable grounded answer"
    assert dispatches == 1
    assert retry_budget.calls_started == 0
    assert retry_state.attempt_id == first_state.attempt_id
    assert retry_state.consumer_result_status == ResultStatus.PENDING
    assert retry_state.attempt_id is not None
    await chat_api._classify_chat_candidate(
        retry_state.attempt_id,
        result_status=ResultStatus.ACCEPTED,
    )
    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
        candidate = (
            await check.execute(select(LLMSemanticCandidate))
        ).scalar_one()
    assert row.result_status == ResultStatus.ACCEPTED.value
    assert candidate.candidate_status == "accepted"
    await engine.dispose()


@pytest.mark.asyncio
async def test_chat_retry_with_changed_semantic_binding_fails_without_dispatch(
    monkeypatch,
):
    engine, _factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    dispatches = 0

    async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal dispatches
        dispatches += 1
        return {
            "id": "binding-id",
            "model": "gpt-4o-2024-11-20",
            "choices": [
                {"finish_reason": "stop", "message": {"content": "answer"}}
            ],
            "usage": {
                "prompt_tokens": 4,
                "completion_tokens": 2,
                "total_tokens": 6,
            },
        }

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    cfg = {
        "openai": {
            "api_key": "test",
            "model": "gpt-4o-2024-11-20",
            "base_url": "https://api.openai.com/v1",
        }
    }
    operation_id = chat_api._chat_operation_id(
        project_id,
        LLMPurpose.CHAT_ANSWER,
    )
    first_budget = _budget()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=first_budget,
        )
    ):
        assert await providers.provider_chat(
            "openai",
            cfg,
            system="system",
            prompt="prompt",
            max_output_tokens=1_200,
            run_budget=first_budget,
            operation_id=operation_id,
            semantic_codec=providers.chat_answer_semantic_codec("binding-a"),
        ) == "answer"

    retry_budget = _budget()
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=retry_budget,
        )
    ):
        with pytest.raises(LLMSemanticCandidateUnavailable):
            await providers.provider_chat(
                "openai",
                cfg,
                system="system",
                prompt="prompt",
                max_output_tokens=1_200,
                run_budget=retry_budget,
                operation_id=operation_id,
                semantic_codec=providers.chat_answer_semantic_codec("binding-b"),
            )

    assert dispatches == 1
    assert retry_budget.calls_started == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_atlas_generation_is_fail_disabled_without_attempt_or_network(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    state = providers.ChatProviderAttemptState()
    urls: list[str] = []

    async def fake_post(_client, url, **_kwargs):  # noqa: ANN001, ANN003
        urls.append(url)
        async with factory() as check:
            rows = (await check.execute(select(LLMCall))).scalars().all()
            assert len(rows) == 1
            assert rows[0].attempt_status == "started"
        if len(urls) == 1:
            return {"id": "session-1"}
        return {"message": "atlas answer"}

    monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=budget,
        )
    ):
        reply = await providers.provider_chat(
            "atlas",
            {
                "atlas": {
                    "api_key": "test",
                    "agent_id": "agent-1",
                    "base_url": "https://atlas.invalid/api",
                }
            },
            system="system",
            prompt="prompt",
            max_output_tokens=1_200,
            run_budget=budget,
            operation_id=chat_api._chat_operation_id(
                project_id, LLMPurpose.CHAT_ANSWER
            ),
            attempt_state=state,
        )
    assert reply is None
    assert urls == []
    assert state.attempt_id is None
    assert budget.calls_started == 0
    async with factory() as check:
        rows = (await check.execute(select(LLMCall))).scalars().all()
        scopes = (await check.execute(select(LLMBudgetScope))).scalars().all()
    assert rows == []
    assert scopes == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancelled_chat_attempt_is_finalized_without_raw_payload(
    monkeypatch,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()
    dispatched = asyncio.Event()

    async def wait_forever(**_kwargs):  # noqa: ANN003
        dispatched.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(providers, "_openai_compatible", wait_forever)
    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=budget,
        )
    ):
        task = asyncio.create_task(
            providers.provider_chat(
                "openai",
                {
                    "openai": {
                        "api_key": "test",
                        "model": "gpt-4o-2024-11-20",
                        "base_url": "https://api.openai.com/v1",
                    }
                },
                system="sentinel-system-must-not-persist",
                prompt="sentinel-prompt-must-not-persist",
                max_output_tokens=1_200,
                run_budget=budget,
                operation_id=chat_api._chat_operation_id(
                    project_id, LLMPurpose.CHAT_ANSWER
                ),
            )
        )
        await asyncio.wait_for(dispatched.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
    assert row.attempt_status == "cancelled"
    assert row.result_status == "not_applicable"
    assert row.usage_status == "lost_after_dispatch"
    assert row.failure_code == "chat_cancelled"
    assert "sentinel" not in str(row.__dict__)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_kind", "attempt_status", "result_status", "failure_code"),
    [
        ("output_limit", "completed", "output_limit_rejected", "chat_output_limit"),
        ("timeout", "timeout", "not_applicable", "chat_timeout"),
        ("transport", "failed", "not_applicable", "chat_transport_error"),
    ],
)
async def test_terminal_timeout_and_transport_failures_leave_no_started_row(
    monkeypatch,
    failure_kind,
    attempt_status,
    result_status,
    failure_code,
):
    engine, factory = await _accounting_store(monkeypatch)
    project_id = uuid.uuid4()
    budget = _budget()

    if failure_kind == "output_limit":
        async def fake_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
            return {
                "id": "limited-id",
                "model": "gpt-4o-2024-11-20",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": "partial"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 1_200,
                    "total_tokens": 1_210,
                },
            }

        monkeypatch.setattr(providers, "_post_json_bounded", fake_post)
    elif failure_kind == "timeout":
        async def fail_timeout(**_kwargs):  # noqa: ANN003
            raise TimeoutError

        monkeypatch.setattr(providers, "_openai_compatible", fail_timeout)
    else:
        async def fail_transport(**_kwargs):  # noqa: ANN003
            raise RuntimeError("sentinel-provider-body")

        monkeypatch.setattr(providers, "_openai_compatible", fail_transport)

    async with use_attempt_callbacks(
        chat_api._chat_attempt_callbacks(
            project_id=project_id,
            purpose=LLMPurpose.CHAT_ANSWER,
            run_budget=budget,
        )
    ):
        reply = await providers.provider_chat(
            "openai",
            {
                "openai": {
                    "api_key": "test",
                    "model": "gpt-4o-2024-11-20",
                    "base_url": "https://api.openai.com/v1",
                }
            },
            system="system",
            prompt="prompt",
            max_output_tokens=1_200,
            run_budget=budget,
            operation_id=chat_api._chat_operation_id(
                project_id, LLMPurpose.CHAT_ANSWER
            ),
        )
    assert reply is None
    async with factory() as check:
        row = (await check.execute(select(LLMCall))).scalar_one()
    assert row.attempt_status == attempt_status
    assert row.result_status == result_status
    assert row.failure_code == failure_code
    assert row.attempt_status != "started"
    assert "sentinel-provider-body" not in str(row.__dict__)
    await engine.dispose()
