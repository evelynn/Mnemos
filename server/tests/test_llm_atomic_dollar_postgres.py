"""Live PostgreSQL evidence for atomic project-dollar reservations.

These tests deliberately use independent connections.  SQLite can exercise
the policy math, but it cannot prove PostgreSQL advisory/row-lock ordering or
the crash-reconciliation behavior that protects a shared project cap.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.extractor.cost import LLMRunBudget, RunBudgetExceeded, check_budget
from app.llm.contracts import AttemptKind, AttemptStatus, LLMPurpose, UsageSource
from app.llm.lifecycle import (
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMLifecycleError,
    begin_attempt,
    fingerprint_input,
    reconcile_stale_attempts,
)
from app.models import all_models as _all_models  # noqa: F401
from app.models.base import Base
from app.models.findings import LLMCall, LLMProjectBudgetAccount


pytestmark = pytest.mark.integration


def _postgres_url() -> str | None:
    raw = os.environ.get("MNEMOS_TEST_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL", ""
    )
    if not raw.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    _prefix, rest = raw.split("://", 1)
    return "postgresql+asyncpg://" + rest


@pytest_asyncio.fixture
async def postgres_budget_database():
    """Create a private schema while retaining real PostgreSQL locks/MVCC."""

    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    # Match the application engine contract even when the cluster/role URL
    # carries a different default_transaction_isolation option.
    root_engine = create_async_engine(url, isolation_level="READ COMMITTED")
    schema = f"mnemos_llm_dollar_{uuid.uuid4().hex}"
    schema_engine = None
    try:
        async with root_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        async with schema_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(schema_engine, expire_on_commit=False)
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await root_engine.dispose()


def _budget(*, wall_time_sec: int = 60) -> LLMRunBudget:
    return LLMRunBudget(
        max_calls=1,
        max_input_tokens=1_000,
        max_output_tokens=1_000,
        wall_time_sec=wall_time_sec,
    )


def _metadata() -> AttemptStartMetadata:
    return AttemptStartMetadata(
        operation_id=uuid.uuid4(),
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="api",
        requested_model="claude-sonnet-4-6",
        input_fingerprint=fingerprint_input("system", "postgres-race"),
        estimated_input_tokens=100,
        input_estimate_method="approx_json_v1",
        requested_max_output_tokens=50,
        usage_source=UsageSource.PROVIDER_API,
        billing_route="anthropic_messages_api",
    )


async def _begin(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    budget: LLMRunBudget,
) -> AttemptTicket:
    async with factory() as session:
        return await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id=f"node:{budget.scope_id}",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(),
        )


@pytest.mark.asyncio
async def test_postgres_concurrent_reservations_have_exactly_one_winner(
    postgres_budget_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two workers may observe capacity, but only one can reserve it."""

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "7")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project_id = uuid.uuid4()
    start = asyncio.Event()

    async with postgres_budget_database() as probe:
        connection = await probe.connection()
        assert await connection.get_isolation_level() == "READ COMMITTED"

    async def contender() -> AttemptTicket:
        budget = _budget()
        await start.wait()
        return await _begin(
            postgres_budget_database,
            project_id=project_id,
            budget=budget,
        )

    tasks = [asyncio.create_task(contender()) for _ in range(2)]
    start.set()
    outcomes = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=20
    )

    winners = [value for value in outcomes if isinstance(value, AttemptTicket)]
    rejected = [value for value in outcomes if isinstance(value, RunBudgetExceeded)]
    assert len(winners) == 1
    assert len(rejected) == 1
    assert rejected[0].reason == "project_dollar_limit_exceeded"

    async with postgres_budget_database() as session:
        calls, reserved = (
            await session.execute(
                select(
                    func.count(LLMCall.id),
                    func.coalesce(func.sum(LLMCall.reserved_cost_usd), 0),
                ).where(LLMCall.project_id == project_id)
            )
        ).one()
        account = await session.get(LLMProjectBudgetAccount, project_id)
    assert calls == 1
    assert reserved == Decimal("6.0011250000")
    assert account is not None
    assert account.cap_usd == Decimal("7.0000000000")

    # A stale cap-disabled worker must still report the durable project policy
    # rather than claiming that monetary protection is off.
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0")
    async with postgres_budget_database() as stale_worker:
        status = await check_budget(stale_worker, project_id)
    assert status.enabled is True
    assert status.cap_usd == 7.0
    assert status.policy_mismatch is True
    assert status.exceeded is True


@pytest.mark.asyncio
async def test_postgres_repeatable_read_is_rejected_before_any_reservation(
    postgres_budget_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A custom RR session cannot obtain a provider-dispatch ticket."""

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "7")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project_id = uuid.uuid4()
    base_engine = postgres_budget_database.kw["bind"]

    async with base_engine.connect() as raw_connection:
        connection = await raw_connection.execution_options(
            isolation_level="REPEATABLE READ"
        )
        assert await connection.get_isolation_level() == "REPEATABLE READ"
        async with AsyncSession(bind=connection, expire_on_commit=False) as session:
            with pytest.raises(
                LLMLifecycleError,
                match="postgresql_attempt_accounting_requires_read_committed",
            ):
                await begin_attempt(
                    session,
                    project_id=project_id,
                    analysis_run_id=None,
                    purpose=LLMPurpose.SUMMARY,
                    target_id="node:repeatable-read",
                    level=1,
                    run_budget=_budget(),
                    scope_kind=BudgetScopeKind.REQUEST,
                    metadata=_metadata(),
                )

    async with postgres_budget_database() as check:
        assert await check.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount).where(
                LLMProjectBudgetAccount.project_id == project_id
            )
        ) == 0
        assert await check.scalar(
            select(func.count()).select_from(LLMCall).where(
                LLMCall.project_id == project_id
            )
        ) == 0


@pytest.mark.asyncio
async def test_postgres_crash_reconcile_keeps_no_refund_reservation(
    postgres_budget_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A killed worker leaves STARTED spend unknown, never refunded to zero."""

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    project_id = uuid.uuid4()
    ticket = await _begin(
        postgres_budget_database,
        project_id=project_id,
        budget=_budget(wall_time_sec=1),
    )

    # Simulate a process dying after STARTED commit and provider dispatch.  A
    # new worker can only reconcile after the DB-owned scope deadline.
    await asyncio.sleep(1.2)
    async with postgres_budget_database() as reconciler:
        assert await reconcile_stale_attempts(
            reconciler, grace_seconds=0, limit=10
        ) == 1

    async with postgres_budget_database() as session:
        row = await session.get(LLMCall, ticket.attempt_id)
    assert row is not None
    assert row.attempt_status == AttemptStatus.UNKNOWN.value
    assert row.failure_code == "stale_started_reconciled"
    assert row.usage_status == "lost_after_dispatch"
    assert row.tokens_used is None
    assert row.reserved_cost_usd == Decimal("6.0011250000")
