"""Atomic, no-refund project-dollar reservation contract tests."""

from __future__ import annotations

import hashlib
import importlib.util
import uuid
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.extractor.cost import (
    BudgetConfigurationError,
    LLMRunBudget,
    RunBudgetExceeded,
    check_budget,
    require_budget,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    LLMPurpose,
    ResultStatus,
    UsageSource,
    UsageStatus,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    BudgetScopeKind,
    LLMPriceContractBreach,
    PRICE_ATTESTATION_COST_BREACH,
    PRICE_ATTESTATION_INPUT_BREACH,
    PRICE_ATTESTATION_MODEL_MISMATCH,
    PRICE_ATTESTATION_OUTPUT_BREACH,
    PRICE_ATTESTATION_TOOL_BREACH,
    assert_attempt_accounting_isolation,
    begin_attempt,
    finalize_attempt,
    fingerprint_input,
)
from app.llm.pricing import (
    OFFICIAL_ANTHROPIC_API_BASE_URL,
    OFFICIAL_GEMINI_GENERATE_API_BASE_URL,
    OFFICIAL_OPENAI_CHAT_API_BASE_URL,
    PRICE_CONTRACTS,
    PRICE_CATALOG_POLICY_VERSION,
    PRICE_CATALOG_SHA256,
    PriceContractError,
    _canonicalize_price_catalog,
    resolve_price_contract,
)
from app.models.findings import (
    LLMCall,
    LLMBudgetScope,
    LLMProjectBudgetAccount,
)
from app.testing.sqlite_polyglot import install_polyglot


_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0038_llm_atomic_usd_budget.py"
)


def _budget() -> LLMRunBudget:
    return LLMRunBudget(
        max_calls=2,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        wall_time_sec=60,
    )


def _metadata(
    *,
    operation_id: uuid.UUID | None = None,
    model: str = "claude-sonnet-4-6",
    route: str | None = "anthropic_messages_api",
    usage_source: UsageSource = UsageSource.PROVIDER_API,
    requested_output: int = 50,
) -> AttemptStartMetadata:
    return AttemptStartMetadata(
        operation_id=operation_id or uuid.uuid4(),
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="api",
        requested_model=model,
        input_fingerprint=fingerprint_input("system", "prompt"),
        estimated_input_tokens=100,
        input_estimate_method="approx_json_v1",
        requested_max_output_tokens=requested_output,
        usage_source=usage_source,
        billing_route=route,
    )


@pytest.fixture
async def dollar_db():
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_attempt_isolation_guard_is_a_connection_free_noop(
    dollar_db,
) -> None:
    """Local SQLite must never receive PostgreSQL isolation operations."""

    async with AsyncSession(dollar_db) as session:
        assert session.in_transaction() is False
        await assert_attempt_accounting_isolation(session)
        assert session.in_transaction() is False


def test_price_contract_reserves_official_input_ceiling_and_rounds_up() -> None:
    contract = resolve_price_contract(
        provider="anthropic",
        billing_route="anthropic_messages_api",
        model="claude-sonnet-4-6",
    )
    reservation = contract.reserve(50)

    assert reservation.reserved_input_tokens == 1_000_000
    assert reservation.reserved_output_tokens == 50
    assert reservation.reserved_cost_usd == Decimal("6.0011250000")


def test_project_policy_version_is_derived_from_the_exact_price_catalog() -> None:
    assert len(PRICE_CATALOG_SHA256) == 64
    assert PRICE_CATALOG_POLICY_VERSION.endswith(PRICE_CATALOG_SHA256[:27])
    assert len(PRICE_CATALOG_POLICY_VERSION) <= 32


def test_price_catalog_digest_and_dispatch_roots_attest_network_destination() -> None:
    contracts = tuple(PRICE_CONTRACTS.values())
    routes = {
        (contract.provider, contract.billing_route): contract.api_base_url
        for contract in contracts
    }
    assert routes[("anthropic", "anthropic_messages_api")] == (
        OFFICIAL_ANTHROPIC_API_BASE_URL
    )
    assert routes[("openai", "openai_chat_completions_api")] == (
        OFFICIAL_OPENAI_CHAT_API_BASE_URL
    )
    assert routes[("gemini", "gemini_generate_content_api")] == (
        OFFICIAL_GEMINI_GENERATE_API_BASE_URL
    )

    openai_contract = resolve_price_contract(
        provider="openai",
        billing_route="openai_chat_completions_api",
        model="gpt-4o-2024-11-20",
    )
    changed = tuple(
        replace(item, api_base_url="https://replacement.invalid/v1")
        if item is openai_contract
        else item
        for item in contracts
    )
    changed_digest = hashlib.sha256(
        _canonicalize_price_catalog(changed).encode("utf-8")
    ).hexdigest()
    assert changed_digest != PRICE_CATALOG_SHA256


def test_openai_alias_is_not_an_immutable_price_contract() -> None:
    with pytest.raises(PriceContractError):
        resolve_price_contract(
            provider="openai",
            billing_route="openai_chat_completions_api",
            model="gpt-4o",
        )

    snapshot = resolve_price_contract(
        provider="openai",
        billing_route="openai_chat_completions_api",
        model="gpt-4o-2024-11-20",
    )
    assert snapshot.contract_id == (
        "openai.chat.gpt-4o-2024-11-20.2026-07-16.v2"
    )


@pytest.mark.parametrize(
    ("provider", "route", "model"),
    [
        ("anthropic", "claude_agent_sdk", "claude-sonnet-4-6"),
        ("atlas", None, ""),
        ("anthropic", "anthropic_messages_api", "claude-unknown"),
        ("openai", "custom_openai_proxy", "gpt-4o"),
    ],
)
def test_unknown_or_opaque_price_contract_fails_closed(
    provider: str, route: str | None, model: str
) -> None:
    with pytest.raises(PriceContractError):
        resolve_price_contract(provider=provider, billing_route=route, model=model)


@pytest.mark.asyncio
async def test_begin_atomically_persists_account_and_no_refund_reservation(
    dollar_db, monkeypatch
) -> None:
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project_id = uuid.uuid4()
    budget = _budget()

    async with AsyncSession(dollar_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:priced",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )
        row = await session.get(LLMCall, ticket.attempt_id)
        account = await session.get(LLMProjectBudgetAccount, project_id)
        assert row is not None
        assert account is not None
        assert account.cap_usd == Decimal("10.0000000000")
        assert row.reserved_input_tokens == 1_000_000
        assert row.reserved_output_tokens == 50
        assert row.reserved_cost_usd == Decimal("6.0011250000")
        assert row.reservation_price_contract_id == (
            "anthropic.messages.claude-sonnet-4-6.conservative-1m.2026-07-16.v2"
        )

        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=AttemptOutcome(
                attempt_status=AttemptStatus.FAILED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(
                    lost_after_dispatch=True,
                    source=UsageSource.PROVIDER_API,
                ),
                failure_code="test_failure",
            ),
        )
        finalized = await session.get(LLMCall, ticket.attempt_id)
        assert finalized is not None
        assert finalized.reserved_cost_usd == Decimal("6.0011250000")


@pytest.mark.asyncio
async def test_serialized_sum_rejects_second_call_above_cap(
    dollar_db, monkeypatch
) -> None:
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "7")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project_id = uuid.uuid4()

    async with AsyncSession(dollar_db, expire_on_commit=False) as first:
        await begin_attempt(
            first,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:first",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )

    second_budget = _budget()
    async with AsyncSession(dollar_db, expire_on_commit=False) as second:
        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_limit_exceeded"
        ):
            await begin_attempt(
                second,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:second",
                level=1,
                run_budget=second_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
            )
        assert second_budget.exhausted_reason == "project_dollar_limit_exceeded"

    async with AsyncSession(dollar_db) as check:
        assert (
            await check.scalar(
                select(func.count()).select_from(LLMCall).where(
                    LLMCall.project_id == project_id
                )
            )
        ) == 1


@pytest.mark.asyncio
async def test_agent_sdk_is_rejected_before_any_account_scope_or_call(
    dollar_db, monkeypatch
) -> None:
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    project_id = uuid.uuid4()
    budget = _budget()

    async with AsyncSession(dollar_db) as session:
        with pytest.raises(
            RunBudgetExceeded,
            match="project_dollar_price_contract_unavailable",
        ):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:agent",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(
                    route="claude_agent_sdk",
                    usage_source=UsageSource.AGENT_SDK,
                ),
            )
        assert budget.exhausted_reason == (
            "project_dollar_price_contract_unavailable"
        )
        assert await session.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(LLMBudgetScope)
        ) == 0
        assert await session.scalar(select(func.count()).select_from(LLMCall)) == 0


@pytest.mark.asyncio
async def test_persisted_policy_mismatch_fails_closed(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    async with AsyncSession(dollar_db, expire_on_commit=False) as first:
        await begin_attempt(
            first,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:first",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "11")
    async with AsyncSession(dollar_db) as second:
        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_policy_mismatch"
        ):
            await begin_attempt(
                second,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:drift",
                level=1,
                run_budget=_budget(),
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
            )


@pytest.mark.asyncio
async def test_stale_price_catalog_worker_policy_fails_closed(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "20")
    async with AsyncSession(dollar_db, expire_on_commit=False) as first:
        await begin_attempt(
            first,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:first-catalog",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )
        account = await first.get(LLMProjectBudgetAccount, project_id)
        assert account is not None
        assert account.policy_version == PRICE_CATALOG_POLICY_VERSION
        account.policy_version = "usd1:stale-catalog"
        await first.commit()

    async with AsyncSession(dollar_db) as stale:
        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_policy_mismatch"
        ):
            await begin_attempt(
                stale,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:stale-catalog",
                level=1,
                run_budget=_budget(),
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
            )


@pytest.mark.asyncio
async def test_finalizer_rejects_semantic_success_from_unattested_model(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "20")
    async with AsyncSession(dollar_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:model-drift",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )
        with pytest.raises(
            LLMPriceContractBreach,
            match=PRICE_ATTESTATION_MODEL_MISMATCH,
        ):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=AttemptOutcome(
                    attempt_status=AttemptStatus.COMPLETED,
                    result_status=ResultStatus.PENDING,
                    usage=unavailable_usage(
                        source=UsageSource.PROVIDER_API
                    ),
                    resolved_model="claude-more-expensive",
                ),
            )
        row = await session.get(LLMCall, ticket.attempt_id)
        assert row is not None and row.attempt_status == "completed"
        assert row.result_status == ResultStatus.NOT_APPLICABLE.value
        assert row.failure_code == PRICE_ATTESTATION_MODEL_MISMATCH


@pytest.mark.asyncio
async def test_rejected_model_drift_is_durable_unpriced_and_blocks_next_call(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "20")
    async with AsyncSession(dollar_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:rejected-model-drift",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(source=UsageSource.PROVIDER_API),
                resolved_model="claude-more-expensive",
                failure_code=PRICE_ATTESTATION_MODEL_MISMATCH,
            ),
        )
        status = await check_budget(session, project_id)
        assert status.unpriced_provider_calls == 1
        assert status.exceeded is True

        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_history_unpriced"
        ):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:blocked-after-model-drift",
                level=1,
                run_budget=_budget(),
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(operation_id=uuid.uuid4()),
            )


@pytest.mark.parametrize(
    ("usage", "failure_code"),
    [
        (
            LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=1_000_001,
                output_tokens=1,
                total_tokens=1_000_002,
            ),
            PRICE_ATTESTATION_INPUT_BREACH,
        ),
        (
            LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=1,
                output_tokens=51,
                total_tokens=52,
            ),
            PRICE_ATTESTATION_OUTPUT_BREACH,
        ),
        (
            LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                reported_cost_usd=Decimal("6.0011250001"),
            ),
            PRICE_ATTESTATION_COST_BREACH,
        ),
        (
            LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                tool_input_tokens=1,
            ),
            PRICE_ATTESTATION_TOOL_BREACH,
        ),
    ],
    ids=("input", "output", "reported-cost", "tool-input"),
)
@pytest.mark.asyncio
async def test_terminal_price_ceiling_breach_is_durable_and_blocks_products(
    dollar_db,
    monkeypatch,
    usage: LLMUsageV1,
    failure_code: str,
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "20")
    async with AsyncSession(dollar_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id=f"node:{failure_code}",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )
        with pytest.raises(LLMPriceContractBreach, match=failure_code):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=AttemptOutcome(
                    attempt_status=AttemptStatus.COMPLETED,
                    result_status=ResultStatus.PENDING,
                    usage=usage,
                    resolved_model="claude-sonnet-4-6",
                ),
            )

        row = await session.get(LLMCall, ticket.attempt_id)
        assert row is not None
        assert row.attempt_status == AttemptStatus.COMPLETED.value
        assert row.result_status == ResultStatus.NOT_APPLICABLE.value
        assert row.failure_code == failure_code
        assert (await check_budget(session, project_id)).unpriced_provider_calls == 1


@pytest.mark.asyncio
async def test_cap_disabled_worker_cannot_bypass_persisted_project_policy(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    async with AsyncSession(dollar_db, expire_on_commit=False) as first:
        await begin_attempt(
            first,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:priced",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0")
    disabled_budget = _budget()
    async with AsyncSession(dollar_db) as disabled:
        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_policy_mismatch"
        ):
            await begin_attempt(
                disabled,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:bypass",
                level=1,
                run_budget=disabled_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
            )
    assert disabled_budget.exhausted_reason == "project_dollar_policy_mismatch"

    async with AsyncSession(dollar_db) as check:
        assert await check.scalar(
            select(func.count()).select_from(LLMCall).where(
                LLMCall.project_id == project_id
            )
        ) == 1


@pytest.mark.asyncio
async def test_budget_status_uses_durable_account_and_surfaces_worker_drift(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    async with AsyncSession(dollar_db, expire_on_commit=False) as owner:
        await begin_attempt(
            owner,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:durable-policy",
            level=1,
            run_budget=_budget(),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0")
    async with AsyncSession(dollar_db) as stale_worker:
        status = await check_budget(stale_worker, project_id)
        assert status.enabled is True
        assert status.cap_usd == 10.0
        assert status.window_sec == 3600
        assert status.policy_mismatch is True
        assert status.authorized_reservation_usd == pytest.approx(6.001125)
        assert status.exceeded is True
        with pytest.raises(BudgetConfigurationError, match="durable dollar policy"):
            await require_budget(stale_worker, project_id)


@pytest.mark.asyncio
async def test_first_denial_persists_policy_account_without_attempt(
    dollar_db, monkeypatch
) -> None:
    project_id = uuid.uuid4()
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "6")
    budget = _budget()

    async with AsyncSession(dollar_db) as session:
        with pytest.raises(
            RunBudgetExceeded, match="project_dollar_limit_exceeded"
        ):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:too-expensive",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
            )

    assert budget.exhausted_reason == "project_dollar_limit_exceeded"
    async with AsyncSession(dollar_db) as check:
        assert await check.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount).where(
                LLMProjectBudgetAccount.project_id == project_id
            )
        ) == 1
        assert await check.scalar(
            select(func.count()).select_from(LLMBudgetScope)
        ) == 0
        assert await check.scalar(select(func.count()).select_from(LLMCall)) == 0


def test_model_exposes_account_and_reservation_shape() -> None:
    account = LLMProjectBudgetAccount.__table__
    assert set(account.c.keys()) == {
        "project_id",
        "policy_version",
        "cap_usd",
        "window_sec",
        "created_at",
    }
    assert account.c.project_id.primary_key
    assert isinstance(account.c.cap_usd.type, sa.Numeric)
    assert account.c.cap_usd.type.precision == 20
    assert account.c.cap_usd.type.scale == 10
    assert {
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reservation_price_contract_id",
    } <= set(LLMCall.__table__.c.keys())
    shape = next(
        constraint
        for constraint in LLMCall.__table__.constraints
        if constraint.name == "ck_llm_calls_cost_reservation_shape"
    )
    sql = " ".join(str(shape.sqltext).split())
    assert "estimated_input_tokens <= reserved_input_tokens" in sql
    assert "requested_max_output_tokens = reserved_output_tokens" in sql


class _RecordedOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_llm_atomic_usd_0038", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0038_migration_matches_chain_and_reservation_columns() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder
    migration.upgrade()

    assert migration.down_revision == "0037_llm_budget_scopes"
    created = next(call for call in recorder.calls if call[0] == "create_table")
    assert created[1][0] == "llm_project_budget_accounts"
    added = {
        args[1].name
        for name, args, _kwargs in recorder.calls
        if name == "add_column"
    }
    assert added == {
        "reserved_input_tokens",
        "reserved_output_tokens",
        "reservation_price_contract_id",
    }
    assert any(
        name == "create_check_constraint"
        and args[0] == "ck_llm_calls_cost_reservation_shape"
        for name, args, _kwargs in recorder.calls
    )


def test_0038_downgrade_guard_precedes_destructive_ddl() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder
    migration.downgrade()

    assert recorder.calls[0][0] == "execute"
    guard = " ".join(str(recorder.calls[0][1][0]).lower().split())
    assert "cannot downgrade 0038" in guard
    assert "llm_project_budget_accounts" in guard
    assert "reservation_price_contract_id" in guard
