"""Direct Anthropic summary: durable start -> usage -> DB grounding."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.extractor import runner as runner_module
from app.extractor.agent import (
    FALLBACK_AGENT_SDK_INIT,
    FALLBACK_AGENT_SDK_RESULT,
    FALLBACK_ANTHROPIC_CLIENT_INIT,
    FALLBACK_ANTHROPIC_IMPORT,
    Extractor,
)
from app.extractor.cost import LLMRunBudget, project_budget_exposure
from app.extractor.runner import (
    FALLBACK_ACCOUNTING_UNAVAILABLE,
    FALLBACK_ATTEMPT_REPLAY_BLOCKED,
    _ground_result_or_fallback,
    _summarize_with_budget,
)
from app.llm.contracts import AttemptStatus, UsageStatus
from app.llm.lifecycle import BUDGET_POLICY_VERSION
from app.models.findings import LLMCall, LLMBudgetScope, LLMProjectBudgetAccount
from app.models.graph import Node
from app.models.llm import LLMSemanticCandidate
from app.models.overlays import GraphNodeHumanOverlay
from app.models.projects import Project  # noqa: F401
from app.testing.sqlite_polyglot import install_polyglot


_PRODUCTION_BEGIN_ATTEMPT = runner_module.begin_attempt


@pytest.fixture(autouse=True)
def _summary_contract_lifecycle_mode(monkeypatch):
    """Exercise Summary semantics independently of paid-route authorization."""

    async def generic_test_begin_attempt(*args, **kwargs):  # noqa: ANN002, ANN003
        assert kwargs.pop("require_atomic_dollar_reservation") is True
        return await _PRODUCTION_BEGIN_ATTEMPT(
            *args,
            **kwargs,
            require_atomic_dollar_reservation=False,
        )

    monkeypatch.setattr(
        runner_module,
        "begin_attempt",
        generic_test_begin_attempt,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["anthropic_client", "agent_sdk_options"])
async def test_provider_local_setup_failure_refunds_preflight_without_ledger(
    monkeypatch,
    backend: str,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    if backend == "anthropic_client":
        provider_module = ModuleType("anthropic")

        def fail_client(**_kwargs):
            raise ValueError("local client configuration failed")

        provider_module.AsyncAnthropic = fail_client  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "anthropic", provider_module)
        monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
        extractor = Extractor()
        extractor._api_key = "test-key"
        expected = FALLBACK_ANTHROPIC_CLIENT_INIT
    else:
        sdk_module = ModuleType("claude_agent_sdk")

        class Options:
            def __init__(self, **_kwargs) -> None:
                raise ValueError("local options configuration failed")

        sdk_module.ClaudeAgentOptions = Options  # type: ignore[attr-defined]
        sdk_module.AssistantMessage = object  # type: ignore[attr-defined]
        sdk_module.ResultMessage = object  # type: ignore[attr-defined]
        sdk_module.TextBlock = object  # type: ignore[attr-defined]
        sdk_module.query = object()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
        extractor = Extractor()
        expected = FALLBACK_AGENT_SDK_INIT

    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=2_048,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            extractor,
            uuid.uuid4(),
            1,
            "sym:setup",
            [],
            analysis_run_id=run_id,
            run_budget=budget,
        )
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()
        calls = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert result.fallback_reason == expected
    assert scopes == []
    assert calls == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0
    assert budget.exhausted_reason == expected


@pytest.mark.asyncio
async def test_agent_sdk_availability_race_is_predispatch_and_refunded(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    checks = iter((True, False))
    monkeypatch.setattr(
        "app.extractor.agent_sdk.is_agent_sdk_available",
        lambda: next(checks),
    )
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            Extractor(),
            uuid.uuid4(),
            1,
            "sym:availability-race",
            [],
            analysis_run_id=run_id,
            run_budget=budget,
        )
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()
        calls = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert result.fallback_reason == FALLBACK_AGENT_SDK_INIT
    assert scopes == []
    assert calls == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0


@pytest.mark.asyncio
async def test_agent_sdk_result_does_not_inherit_direct_import_failure(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    async def direct_import_failure(self, _level, _target, _evidence):
        self._pending_from = "anthropic"
        self._pending_reason = FALLBACK_ANTHROPIC_IMPORT
        return None

    class Options:
        def __init__(self, **_kwargs) -> None:
            pass

    class ResultMessage:
        def __init__(self) -> None:
            self.is_error = True
            self.subtype = "error_during_execution"
            self.stop_reason = "end_turn"

    class AssistantMessage:
        pass

    class TextBlock:
        pass

    async def query(**_kwargs):
        yield ResultMessage()

    sdk_module = ModuleType("claude_agent_sdk")
    sdk_module.ClaudeAgentOptions = Options  # type: ignore[attr-defined]
    sdk_module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    sdk_module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    sdk_module.TextBlock = TextBlock  # type: ignore[attr-defined]
    sdk_module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.setattr(
        Extractor,
        "_summarize_via_anthropic_sdk",
        direct_import_failure,
    )
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    extractor = Extractor()
    extractor._api_key = "configured-key"
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            extractor,
            uuid.uuid4(),
            1,
            "sym:mixed",
            [],
            analysis_run_id=run_id,
            run_budget=budget,
        )
        scope = await session.get(LLMBudgetScope, run_id)
        rows = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert result.fallback_reason == FALLBACK_AGENT_SDK_RESULT
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.output_tokens_reserved == 128_000
    assert len(rows) == 1
    assert rows[0].contract_version == 1
    assert rows[0].provider_mode == "unknown"
    assert rows[0].usage_source == "agent_sdk"
    assert rows[0].failure_code == FALLBACK_AGENT_SDK_RESULT
    assert rows[0].fallback_reason == FALLBACK_AGENT_SDK_RESULT


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["anthropic_start", "agent_start"])
async def test_deadline_during_accounting_never_fabricates_physical_call(
    monkeypatch,
    backend: str,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    dispatches = 0
    if backend == "anthropic_start":
        async def slow_begin(*_args, **_kwargs):
            await asyncio.sleep(1)
            raise AssertionError("cancelled accounting resumed")

        async def create(**_kwargs):
            nonlocal dispatches
            dispatches += 1
            raise AssertionError("provider ran before accounting")

        monkeypatch.setattr("app.extractor.runner.begin_attempt", slow_begin)
        provider_module = ModuleType("anthropic")
        provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
            messages=SimpleNamespace(create=create)
        )
        monkeypatch.setitem(sys.modules, "anthropic", provider_module)
        monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
        extractor = Extractor()
        extractor._api_key = "test-key"
        max_output = 2_048
    else:
        async def slow_begin(*_args, **_kwargs):
            await asyncio.sleep(1)
            raise AssertionError("cancelled accounting resumed")

        class Options:
            def __init__(self, **_kwargs) -> None:
                pass

        async def query(**_kwargs):
            nonlocal dispatches
            dispatches += 1
            raise AssertionError("provider ran before accounting")
            yield  # pragma: no cover

        sdk_module = ModuleType("claude_agent_sdk")
        sdk_module.ClaudeAgentOptions = Options  # type: ignore[attr-defined]
        sdk_module.AssistantMessage = object  # type: ignore[attr-defined]
        sdk_module.ResultMessage = object  # type: ignore[attr-defined]
        sdk_module.TextBlock = object  # type: ignore[attr-defined]
        sdk_module.query = query  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
        monkeypatch.setattr("app.extractor.runner.begin_attempt", slow_begin)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
        extractor = Extractor()
        max_output = 128_000

    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=max_output,
        wall_time_sec=0.03,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            extractor,
            uuid.uuid4(),
            1,
            "sym:slow-accounting",
            [],
            analysis_run_id=run_id,
            run_budget=budget,
        )
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()
        calls = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert result.fallback_reason == "run_deadline_exceeded"
    assert dispatches == 0
    assert scopes == []
    assert calls == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0


@pytest.mark.asyncio
async def test_direct_summary_commits_started_before_dispatch_then_accepts_grounding(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    node_id = "sym:orders"
    observed_before_dispatch: list[tuple[str, str, int]] = []

    class _Messages:
        async def create(self, **_kwargs):
            # A separate transaction can already observe the accounting fact;
            # a worker death from this point cannot erase the reservation.
            async with AsyncSession(engine, expire_on_commit=False) as check:
                row = (await check.execute(select(LLMCall))).scalar_one()
                scope = (await check.execute(select(LLMBudgetScope))).scalar_one()
                observed_before_dispatch.append(
                    (row.attempt_status, row.result_status, scope.calls_reserved)
                )
            return SimpleNamespace(
                id="msg_summary_1",
                    model="claude-sonnet-4-6",
                stop_reason="end_turn",
                usage={
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 3,
                    "output_tokens": 5,
                },
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(
                            {
                                "summary": "Orders are present.",
                                "detailed": "The supplied symbol is grounded.",
                                "claims": [
                                    {
                                        "claim": "The order symbol exists.",
                                        "evidence": [
                                            {
                                                "kind": "node",
                                                "node_id": node_id,
                                                "certainty": "asserted",
                                            }
                                        ],
                                    }
                                ],
                                "open_questions": [],
                            }
                        ),
                    )
                ],
            )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=_Messages()
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    evidence = [
        {"kind": "node", "node_id": node_id, "certainty": "asserted"}
    ]
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=2_048,
        wall_time_sec=60,
        scope_id=run_id,
    )
    extractor = Extractor()
    extractor._api_key = "test-key"

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            Node(
                id=node_id,
                project_id=project_id,
                kind="Symbol",
                data={"name": "orders"},
                certainty="asserted",
                created_by=["test"],
            )
        )
        await session.commit()

        result = await _summarize_with_budget(
            session,
            extractor,
            project_id,
            1,
            node_id,
            evidence,
            analysis_run_id=run_id,
            run_budget=budget,
        )
        call_id = result._llm_call_id
        pending = await session.get(LLMCall, call_id)
        assert pending is not None
        assert pending.result_status == "pending"

        grounded = await _ground_result_or_fallback(
            session,
            project_id=project_id,
            level=1,
            target_id=node_id,
            evidence=evidence,
            result=result,
        )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)

    await engine.dispose()
    assert observed_before_dispatch == [("started", "pending", 1)]
    assert len(rows) == 1
    assert rows[0].id == call_id
    assert rows[0].contract_version == 1
    assert rows[0].attempt_status == "completed"
    assert rows[0].result_status == "accepted"
    assert rows[0].usage_status == "reported_complete"
    assert rows[0].input_tokens == 15
    assert rows[0].output_tokens == 5
    assert rows[0].tokens_used == rows[0].total_tokens == 20
    assert rows[0].provider_request_id == "msg_summary_1"
    assert rows[0].resolved_model == "claude-sonnet-4-6"
    assert grounded.fallback_reason == ""
    assert grounded.claims[0]["evidence"][0]["node_id"] == node_id
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.output_tokens_reserved == 1024
    assert scope.input_tokens_reserved > 0


@pytest.mark.asyncio
async def test_resumed_duplicate_summary_never_redispatches_or_aborts_level(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    dispatches = 0

    async def create(**_kwargs):
        nonlocal dispatches
        dispatches += 1
        return SimpleNamespace(
            id="msg_once",
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage={"input_tokens": 3, "output_tokens": 2},
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "summary": "One bounded result.",
                            "detailed": "The ledger prevents redispatch.",
                            "claims": [
                                {
                                    "claim": "The target exists.",
                                    "evidence": [
                                        {
                                            "kind": "node",
                                            "node_id": "sym:once",
                                            "certainty": "asserted",
                                        }
                                    ],
                                }
                            ],
                            "open_questions": [],
                        }
                    ),
                )
            ],
        )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    extractor = Extractor()
    extractor._api_key = "test-key"
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    resumed_run_id = uuid.uuid4()
    revised_run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=10_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    evidence = [{"kind": "node", "node_id": "sym:once"}]

    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await _summarize_with_budget(
            session,
            extractor,
            project_id,
            1,
            "sym:once",
            evidence,
            analysis_run_id=run_id,
            run_budget=budget,
            graph_generation=1,
            overlay_generation=0,
        )

    resumed_extractor = Extractor()
    resumed_extractor._api_key = "test-key"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        resumed = await _summarize_with_budget(
            session,
            resumed_extractor,
            project_id,
            1,
            "sym:once",
            evidence,
            analysis_run_id=resumed_run_id,
            run_budget=LLMRunBudget(
                max_calls=99,
                max_input_tokens=99_999,
                max_output_tokens=99_999,
                wall_time_sec=600,
                scope_id=resumed_run_id,
            ),
            graph_generation=1,
            overlay_generation=0,
        )

    revised_extractor = Extractor()
    revised_extractor._api_key = "test-key"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        revised = await _summarize_with_budget(
            session,
            revised_extractor,
            project_id,
            1,
            "sym:once",
            evidence,
            analysis_run_id=revised_run_id,
            run_budget=LLMRunBudget(
                max_calls=2,
                max_input_tokens=10_000,
                max_output_tokens=2_048,
                wall_time_sec=60,
                scope_id=revised_run_id,
            ),
            graph_generation=2,
            overlay_generation=0,
        )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        candidates = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()

    await engine.dispose()
    assert first.fallback_reason == ""
    assert resumed.fallback_reason == ""
    assert resumed.summary == first.summary
    assert resumed.tokens_used == first.tokens_used == 5
    assert resumed._llm_call_id == first._llm_call_id
    assert revised.fallback_reason == ""
    assert revised._llm_call_id != first._llm_call_id
    assert dispatches == 2
    assert len(rows) == 2
    assert len(candidates) == 2
    assert scope is not None
    assert scope.calls_reserved == 1
    assert len(scopes) == 2
    assert budget.calls_started == 1
    assert budget.output_tokens_reserved == 1_024


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["rejected", "deleted", "corrupt"])
async def test_non_authoritative_summary_candidate_never_redispatches(
    monkeypatch,
    damage: str,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    dispatches = 0
    node_id = "sym:non-authoritative"

    async def create(**_kwargs):
        nonlocal dispatches
        dispatches += 1
        claims = []
        if damage != "rejected":
            claims = [
                {
                    "claim": "The bounded target exists.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": node_id,
                            "certainty": "asserted",
                        }
                    ],
                }
            ]
        return SimpleNamespace(
            id="msg-non-authoritative",
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage={"input_tokens": 2, "output_tokens": 1},
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "summary": "Bounded candidate.",
                            "detailed": "Candidate replay must fail closed.",
                            "claims": claims,
                            "open_questions": [],
                        }
                    ),
                )
            ],
        )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    project_id = uuid.uuid4()
    evidence = [{"kind": "node", "node_id": node_id}]
    first_scope = uuid.uuid4()
    first_extractor = Extractor()
    first_extractor._api_key = "test-key"
    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await _summarize_with_budget(
            session,
            first_extractor,
            project_id,
            1,
            node_id,
            evidence,
            analysis_run_id=first_scope,
            run_budget=LLMRunBudget(
                max_calls=1,
                max_input_tokens=10_000,
                max_output_tokens=2_048,
                wall_time_sec=60,
                scope_id=first_scope,
            ),
            graph_generation=1,
            overlay_generation=0,
        )
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        if damage == "deleted":
            await session.delete(candidate)
            await session.commit()
        elif damage == "corrupt":
            candidate.payload_ciphertext = b"corrupt"
            await session.commit()

    restarted_extractor = Extractor()
    restarted_extractor._api_key = "test-key"
    restarted_scope = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        restarted = await _summarize_with_budget(
            session,
            restarted_extractor,
            project_id,
            1,
            node_id,
            evidence,
            analysis_run_id=restarted_scope,
            run_budget=LLMRunBudget(
                max_calls=99,
                max_input_tokens=99_999,
                max_output_tokens=99_999,
                wall_time_sec=600,
                scope_id=restarted_scope,
            ),
            graph_generation=1,
            overlay_generation=0,
        )
        calls = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    if damage == "rejected":
        assert first.fallback_reason == "ungrounded_response"
        assert restarted.fallback_reason == "ungrounded_response"
        assert restarted.tokens_used == first.tokens_used == 3
        assert restarted._llm_call_id == first._llm_call_id
    elif damage == "deleted":
        assert first.fallback_reason == ""
        assert restarted.fallback_reason == FALLBACK_ATTEMPT_REPLAY_BLOCKED
    else:
        assert first.fallback_reason == ""
        assert restarted.fallback_reason == FALLBACK_ACCOUNTING_UNAVAILABLE
    assert dispatches == 1
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_canonical_but_claimless_summary_closes_pending_as_grounding_rejected(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    async def create(**_kwargs):
        return SimpleNamespace(
            id="msg-claimless",
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage={"input_tokens": 2, "output_tokens": 1},
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                            {
                                "summary": "No grounded claim.",
                                "detailed": "The envelope is valid but empty.",
                                "claims": [],
                                "open_questions": [],
                        }
                    ),
                )
            ],
        )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    extractor = Extractor()
    extractor._api_key = "test-key"
    run_id = uuid.uuid4()

    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            extractor,
            uuid.uuid4(),
            1,
            "sym:empty",
            [],
            analysis_run_id=run_id,
            run_budget=LLMRunBudget(
                max_calls=1,
                max_input_tokens=10_000,
                max_output_tokens=2_048,
                wall_time_sec=60,
                scope_id=run_id,
            ),
        )
        rows = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert result.fallback_reason == "ungrounded_response"
    assert len(rows) == 1
    assert rows[0].result_status == "grounding_rejected"
    assert rows[0].fallback_reason == "ungrounded_response"


@pytest.mark.asyncio
async def test_restarted_worker_tightens_timeout_to_persisted_scope_deadline(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    provider_started = False

    async def create(**_kwargs):
        nonlocal provider_started
        provider_started = True
        await asyncio.sleep(2)
        raise AssertionError("persisted deadline did not cancel the provider")

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    extractor = Extractor()
    extractor._api_key = "test-key"

    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            LLMBudgetScope(
                id=run_id,
                project_id=project_id,
                analysis_run_id=run_id,
                scope_kind="analysis_run",
                policy_version=BUDGET_POLICY_VERSION,
                max_calls=2,
                max_input_tokens=10_000,
                max_output_tokens=2_048,
                wall_time_sec=60,
                calls_reserved=0,
                input_tokens_reserved=0,
                output_tokens_reserved=0,
                started_at=now - timedelta(seconds=59.5),
                deadline_at=now + timedelta(seconds=0.5),
            )
        )
        await session.commit()

        restarted_budget = LLMRunBudget(
            max_calls=99,
            max_input_tokens=999_999,
            max_output_tokens=999_999,
            wall_time_sec=600,
            scope_id=run_id,
        )
        started = time.monotonic()
        result = await _summarize_with_budget(
            session,
            extractor,
            project_id,
            1,
            "sym:deadline",
            [],
            analysis_run_id=run_id,
            run_budget=restarted_budget,
        )
        elapsed = time.monotonic() - started
        row = (await session.execute(select(LLMCall))).scalar_one()

    await engine.dispose()
    assert provider_started is True
    assert elapsed < 1.5
    assert result.fallback_reason == "run_deadline_exceeded"
    assert row.attempt_status == AttemptStatus.TIMEOUT.value
    assert row.usage_status == UsageStatus.LOST_AFTER_DISPATCH.value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key_source", "expected_mode", "expected_known_spend"),
    [
        ("none", "unknown", 0.000066),
        ("ANTHROPIC_API_KEY", "api", 0.000066),
    ],
)
async def test_agent_sdk_summary_commits_budget_scope_before_dispatch(
    monkeypatch,
    api_key_source,
    expected_mode,
    expected_known_spend,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    observed_before_dispatch: list[
        tuple[str, str, int, int, str, int]
    ] = []

    class TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class AssistantMessage:
        def __init__(self, content) -> None:  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error: bool = False) -> None:
            self.is_error = is_error
            self.usage = {
                "input_tokens": 12,
                "cache_creation_input_tokens": 2,
                "cache_read_input_tokens": 3,
                "output_tokens": 5,
            }
            self.total_cost_usd = 0.01
            self.num_turns = 1
            self.session_id = "session_summary_1"
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class SystemMessage:
        def __init__(self) -> None:
            self.subtype = "init"
            self.data = {"apiKeySource": api_key_source}

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs) -> None:
            pass

    async def query(**_kwargs):
        async with AsyncSession(engine, expire_on_commit=False) as check:
            scope = (await check.execute(select(LLMBudgetScope))).scalar_one()
            call = (await check.execute(select(LLMCall))).scalar_one()
            pre_dispatch_cost = await project_budget_exposure(
                check, project_id
            )
            observed_before_dispatch.append(
                (
                    call.attempt_status,
                    call.result_status,
                    scope.calls_reserved,
                    scope.output_tokens_reserved,
                    call.provider_mode,
                    pre_dispatch_cost.unknown_provider_calls,
                )
            )
        payload = {
            "summary": "Orders are summarized from graph evidence.",
            "detailed": "Only the bounded symbol evidence was used.",
            "claims": [
                {
                    "claim": "The order symbol exists.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": "sym:orders",
                            "certainty": "asserted",
                        }
                    ],
                }
            ],
            "open_questions": [],
        }
        yield SystemMessage()
        yield AssistantMessage([TextBlock(json.dumps(payload))])
        yield ResultMessage(False)

    sdk_module = ModuleType("claude_agent_sdk")
    sdk_module.TextBlock = TextBlock  # type: ignore[attr-defined]
    sdk_module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    sdk_module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    sdk_module.SystemMessage = SystemMessage  # type: ignore[attr-defined]
    sdk_module.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    sdk_module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await _summarize_with_budget(
            session,
            Extractor(),
            project_id,
            1,
            "sym:orders",
            [{"kind": "node", "node_id": "sym:orders"}],
            analysis_run_id=run_id,
            run_budget=budget,
        )
        scope = await session.get(LLMBudgetScope, run_id)
        rows = (await session.execute(select(LLMCall))).scalars().all()
        exposure = await project_budget_exposure(session, project_id)

    await engine.dispose()
    assert observed_before_dispatch == [
        ("started", "pending", 1, 128_000, "unknown", 1)
    ]
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.input_tokens_reserved > 0
    assert scope.output_tokens_reserved == 128_000
    assert len(rows) == 1
    assert rows[0].contract_version == 1
    assert rows[0].attempt_status == "completed"
    assert rows[0].result_status == "pending"
    assert rows[0].provider == "anthropic"
    assert rows[0].provider_mode == expected_mode
    assert rows[0].usage_source == "agent_sdk"
    assert rows[0].usage_status == "reported_complete"
    assert rows[0].input_tokens == 17
    assert rows[0].output_tokens == 5
    assert rows[0].total_tokens == rows[0].tokens_used == 22
    assert rows[0].reported_cost_usd is None
    assert float(rows[0].estimated_cost_usd) == pytest.approx(0.01)
    assert rows[0].pricing_version == (
        "claude-agent-sdk-0.2.118-client-estimate"
    )
    assert rows[0].requested_max_output_tokens == 128_000
    assert rows[0].provider_request_id is None
    assert rows[0].finish_reason == "end_turn"
    assert result._llm_call_id == rows[0].id
    assert result.tokens_used == 22
    assert result.model_used == "claude-sonnet-4-6:agent_sdk"
    assert exposure.known_spend_usd == pytest.approx(expected_known_spend)
    assert exposure.unknown_provider_calls == 1


@pytest.mark.asyncio
async def test_restarted_agent_sdk_summary_blocks_duplicate_dispatch(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    dispatches = 0

    class TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class AssistantMessage:
        def __init__(self, content) -> None:  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self) -> None:
            self.is_error = False
            self.subtype = "success"
            self.usage = {"input_tokens": 3, "output_tokens": 2}
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs) -> None:
            pass

    async def query(**_kwargs):
        nonlocal dispatches
        dispatches += 1
        yield AssistantMessage(
            [
                TextBlock(
                    json.dumps(
                            {
                                "summary": "One subscription result.",
                                "detailed": "The stable operation prevents replay.",
                                "claims": [
                                    {
                                        "claim": "The subscription target exists.",
                                        "evidence": [
                                            {
                                                "kind": "node",
                                                "node_id": "sym:agent-once",
                                                "certainty": "asserted",
                                            }
                                        ],
                                    }
                                ],
                                "open_questions": [],
                        }
                    )
                )
            ]
        )
        yield ResultMessage()

    sdk_module = ModuleType("claude_agent_sdk")
    sdk_module.TextBlock = TextBlock  # type: ignore[attr-defined]
    sdk_module.AssistantMessage = AssistantMessage  # type: ignore[attr-defined]
    sdk_module.ResultMessage = ResultMessage  # type: ignore[attr-defined]
    sdk_module.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    sdk_module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    restarted_run_id = uuid.uuid4()
    evidence = [{"kind": "node", "node_id": "sym:agent-once"}]
    first_budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(engine, expire_on_commit=False) as session:
        first = await _summarize_with_budget(
            session,
            Extractor(),
            project_id,
            1,
            "sym:agent-once",
            evidence,
            analysis_run_id=run_id,
            run_budget=first_budget,
        )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        restarted = await _summarize_with_budget(
            session,
            Extractor(),
            project_id,
            1,
            "sym:agent-once",
            evidence,
            analysis_run_id=restarted_run_id,
            run_budget=LLMRunBudget(
                max_calls=99,
                max_input_tokens=2_000_000,
                max_output_tokens=999_999,
                wall_time_sec=600,
                scope_id=restarted_run_id,
            ),
        )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()

    await engine.dispose()
    assert first._llm_call_id == rows[0].id
    assert restarted.fallback_reason == ""
    assert restarted.summary == first.summary
    assert restarted.tokens_used == first.tokens_used == 5
    assert restarted._llm_call_id == first._llm_call_id
    assert dispatches == 1
    assert len(rows) == 1
    assert rows[0].contract_version == 1
    assert rows[0].usage_source == "agent_sdk"
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.output_tokens_reserved == 128_000
    assert len(scopes) == 1


@pytest.mark.asyncio
async def test_outer_deadline_closes_agent_sdk_attempt_with_agent_usage_source(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    provider_started = False

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs) -> None:
            pass

    async def query(**_kwargs):
        nonlocal provider_started
        provider_started = True
        await asyncio.sleep(2)
        raise AssertionError("durable deadline did not cancel Agent SDK")
        yield  # pragma: no cover

    sdk_module = ModuleType("claude_agent_sdk")
    sdk_module.TextBlock = object  # type: ignore[attr-defined]
    sdk_module.AssistantMessage = object  # type: ignore[attr-defined]
    sdk_module.ResultMessage = object  # type: ignore[attr-defined]
    sdk_module.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    sdk_module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        session.add(
            LLMBudgetScope(
                id=run_id,
                project_id=project_id,
                analysis_run_id=run_id,
                scope_kind="analysis_run",
                policy_version=BUDGET_POLICY_VERSION,
                max_calls=2,
                max_input_tokens=2_000_000,
                max_output_tokens=128_000,
                wall_time_sec=60,
                calls_reserved=0,
                input_tokens_reserved=0,
                output_tokens_reserved=0,
                started_at=now - timedelta(seconds=59.5),
                deadline_at=now + timedelta(seconds=0.5),
            )
        )
        await session.commit()
        started = time.monotonic()
        result = await _summarize_with_budget(
            session,
            Extractor(),
            project_id,
            1,
            "sym:agent-deadline",
            [],
            analysis_run_id=run_id,
            run_budget=LLMRunBudget(
                max_calls=99,
                max_input_tokens=2_000_000,
                max_output_tokens=999_999,
                wall_time_sec=600,
                scope_id=run_id,
            ),
        )
        elapsed = time.monotonic() - started
        row = (await session.execute(select(LLMCall))).scalar_one()

    await engine.dispose()
    assert provider_started is True
    assert elapsed < 1.5
    assert result.fallback_reason == "run_deadline_exceeded"
    assert row.attempt_status == AttemptStatus.TIMEOUT.value
    assert row.usage_status == UsageStatus.LOST_AFTER_DISPATCH.value
    assert row.usage_source == "agent_sdk"
    assert row.failure_code == "run_deadline_exceeded"


@pytest.mark.asyncio
async def test_outer_cancellation_closes_agent_sdk_attempt_once(
    monkeypatch,
) -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)

    provider_started = asyncio.Event()
    never_finishes = asyncio.Event()

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs) -> None:
            pass

    async def query(**_kwargs):
        provider_started.set()
        await never_finishes.wait()
        yield  # pragma: no cover

    sdk_module = ModuleType("claude_agent_sdk")
    sdk_module.TextBlock = object  # type: ignore[attr-defined]
    sdk_module.AssistantMessage = object  # type: ignore[attr-defined]
    sdk_module.ResultMessage = object  # type: ignore[attr-defined]
    sdk_module.ClaudeAgentOptions = ClaudeAgentOptions  # type: ignore[attr-defined]
    sdk_module.query = query  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", sdk_module)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        task = asyncio.create_task(
            _summarize_with_budget(
                session,
                Extractor(),
                project_id,
                1,
                "sym:agent-cancel",
                [],
                analysis_run_id=run_id,
                run_budget=LLMRunBudget(
                    max_calls=1,
                    max_input_tokens=2_000_000,
                    max_output_tokens=128_000,
                    wall_time_sec=60,
                    scope_id=run_id,
                ),
            )
        )
        await asyncio.wait_for(provider_started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)

    await engine.dispose()
    assert len(rows) == 1
    assert rows[0].attempt_status == AttemptStatus.CANCELLED.value
    assert rows[0].usage_status == UsageStatus.LOST_AFTER_DISPATCH.value
    assert rows[0].usage_source == "agent_sdk"
    assert rows[0].failure_code == "summary_call_cancelled"
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.output_tokens_reserved == 128_000
