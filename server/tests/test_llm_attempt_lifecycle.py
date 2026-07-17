"""Executable invariants for the crash-durable physical-attempt service."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
import app.llm.lifecycle as lifecycle
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMPurpose,
    ResultStatus,
    UsageSource,
    normalize_anthropic_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    BudgetScopeKind,
    LLMAttemptConflict,
    PreReservedAttempt,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    persist_pre_reserved_budget,
    reconcile_stale_attempts,
)
from app.models.findings import (
    LLMCall,
    LLMBudgetScope,
    LLMProjectBudgetAccount,
)
from app.models.llm import LLMSemanticCandidate
from app.testing.sqlite_polyglot import install_polyglot


def _metadata(
    *,
    operation_id: uuid.UUID | None = None,
    input_fingerprint: str | None = None,
    provider_mode: str = "api",
) -> AttemptStartMetadata:
    return AttemptStartMetadata(
        operation_id=operation_id or uuid.uuid4(),
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode=provider_mode,
        requested_model="claude-test",
        input_fingerprint=input_fingerprint or fingerprint_input("sys", "prompt"),
        estimated_input_tokens=100,
        input_estimate_method="approx_json_v1",
        requested_max_output_tokens=50,
        usage_source=UsageSource.PROVIDER_API,
    )


@pytest.fixture
async def lifecycle_db():
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_begin_commits_scope_reservation_and_started_row_without_input(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    scope_id = run_id
    operation_id = uuid.uuid4()
    secret_prompt = "private source: API_TOKEN=must-not-be-stored"
    fingerprint = fingerprint_input("system", secret_prompt)
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=scope_id,
    )

    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(
                operation_id=operation_id,
                input_fingerprint=fingerprint,
            ),
        )
        call = (
            await session.execute(select(LLMCall).where(LLMCall.id == ticket.attempt_id))
        ).scalar_one()
        scope = await session.get(LLMBudgetScope, scope_id)

    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.input_tokens_reserved == 100
    assert scope.output_tokens_reserved == 50
    assert scope.analysis_run_id == run_id
    assert call.contract_version == 1
    assert call.status == "started"
    assert call.attempt_status == "started"
    assert call.result_status == "pending"
    assert call.input_fingerprint == fingerprint
    assert call.tokens_used is None
    assert secret_prompt not in repr(call.__dict__)
    assert budget.calls_started == 1
    assert budget.input_tokens_reserved == 100
    assert budget.output_tokens_reserved == 50


@pytest.mark.asyncio
async def test_required_atomic_dollar_policy_denies_before_durable_mutation(
    lifecycle_db,
    monkeypatch,
) -> None:
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    project_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
    )

    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        with pytest.raises(
            RunBudgetExceeded,
            match="project_dollar_budget_required",
        ):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:paid-policy-required",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(),
                require_atomic_dollar_reservation=True,
            )

        calls = (await session.execute(select(LLMCall))).scalars().all()
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()
        accounts = (
            await session.execute(select(LLMProjectBudgetAccount))
        ).scalars().all()

    assert calls == []
    assert scopes == []
    assert accounts == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0
    assert budget.exhausted_reason == "project_dollar_budget_required"


@pytest.mark.asyncio
async def test_stale_started_reconciler_closes_unknown_without_refund(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )

    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        first_ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:stale",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="unknown"),
        )
        second_ticket = await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:stale-2",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="unknown"),
        )
        scope = await session.get(LLMBudgetScope, run_id)
        assert scope is not None
        now = datetime.now(timezone.utc)
        scope.started_at = now - timedelta(minutes=2)
        scope.deadline_at = now - timedelta(minutes=1)
        await session.commit()

        assert await reconcile_stale_attempts(
            session, grace_seconds=0, limit=1
        ) == 1
        assert await reconcile_stale_attempts(
            session, grace_seconds=0, limit=1
        ) == 1
        assert await reconcile_stale_attempts(
            session, grace_seconds=0, limit=1
        ) == 0
        rows = [
            await session.get(LLMCall, first_ticket.attempt_id),
            await session.get(LLMCall, second_ticket.attempt_id),
        ]
        scope = await session.get(LLMBudgetScope, run_id)

    assert all(row is not None for row in rows)
    for row in rows:
        assert row is not None
        assert row.attempt_status == "unknown"
        assert row.result_status == "not_applicable"
        assert row.status == "unknown"
        assert row.failure_code == "stale_started_reconciled"
        assert row.usage_status == "lost_after_dispatch"
        assert row.tokens_used is None
        assert row.total_tokens is None
        assert row.completed_at is not None
        assert row.provider_mode == "unknown"
    assert scope is not None
    assert scope.calls_reserved == 2
    assert scope.input_tokens_reserved == 200
    assert scope.output_tokens_reserved == 100


@pytest.mark.asyncio
async def test_first_durable_attempt_absorbs_prior_opaque_run_reservations(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=4,
        max_input_tokens=1_000,
        max_output_tokens=500,
        wall_time_sec=60,
        scope_id=run_id,
    )
    # An Agent/SDK consumer has not migrated to the durable ledger yet.
    budget.reserve(40)
    # The runner then pre-reserves the current direct attempt with a cheaper
    # estimate than the provider adapter computes from its exact prompt.
    budget.reserve(60, 50)

    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(),
            pre_reserved=PreReservedAttempt(
                input_tokens=60,
                output_tokens=50,
            ),
        )
        scope = await session.get(LLMBudgetScope, run_id)

    assert scope is not None
    assert scope.calls_reserved == 2
    assert scope.input_tokens_reserved == 140
    assert scope.output_tokens_reserved == 50
    assert budget.calls_started == 2
    assert budget.input_tokens_reserved == 140
    assert budget.output_tokens_reserved == 50


@pytest.mark.asyncio
async def test_opaque_agent_budget_survives_restart_without_v1_call_row(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    first_budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=1_000,
        max_output_tokens=500,
        wall_time_sec=60,
        scope_id=run_id,
    )
    first_budget.reserve(80)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        await persist_pre_reserved_budget(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            run_budget=first_budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            pre_reserved=PreReservedAttempt(80, 0),
        )

    restarted_budget = LLMRunBudget(
        max_calls=99,
        max_input_tokens=999_999,
        max_output_tokens=999_999,
        wall_time_sec=600,
        scope_id=run_id,
    )
    restarted_budget.reserve(40)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        with pytest.raises(RunBudgetExceeded, match="run_call_limit_exceeded"):
            await persist_pre_reserved_budget(
                session,
                project_id=project_id,
                analysis_run_id=run_id,
                run_budget=restarted_budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                pre_reserved=PreReservedAttempt(40, 0),
            )
        scope = await session.get(LLMBudgetScope, run_id)
        rows = (await session.execute(select(LLMCall))).scalars().all()

    assert scope is not None
    assert scope.max_calls == 1
    assert scope.calls_reserved == 1
    assert scope.input_tokens_reserved == 80
    assert scope.exhausted_reason == "run_call_limit_exceeded"
    assert rows == []


@pytest.mark.asyncio
async def test_scope_deadline_is_rechecked_after_lock_wait(
    lifecycle_db,
    monkeypatch,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        session.add(
            LLMBudgetScope(
                id=run_id,
                project_id=project_id,
                analysis_run_id=run_id,
                scope_kind="analysis_run",
                policy_version=lifecycle.BUDGET_POLICY_VERSION,
                max_calls=2,
                max_input_tokens=1_000,
                max_output_tokens=500,
                wall_time_sec=60,
                calls_reserved=0,
                input_tokens_reserved=0,
                output_tokens_reserved=0,
                started_at=now - timedelta(seconds=59.8),
                deadline_at=now + timedelta(seconds=0.2),
            )
        )
        await session.commit()

        async def delayed_lock(_session, _scope_id):  # noqa: ANN001
            await asyncio.sleep(0.35)

        monkeypatch.setattr(lifecycle, "_advisory_scope_lock", delayed_lock)
        budget = LLMRunBudget(
            max_calls=99,
            max_input_tokens=999_999,
            max_output_tokens=999_999,
            wall_time_sec=600,
            scope_id=run_id,
        )
        with pytest.raises(RunBudgetExceeded, match="run_deadline_exceeded"):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:late",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=_metadata(),
            )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)

    assert rows == []
    assert scope is not None
    assert scope.calls_reserved == 0
    assert scope.exhausted_reason == "run_deadline_exceeded"


@pytest.mark.asyncio
async def test_duplicate_conflict_releases_only_current_local_pre_reservation(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=10,
        max_input_tokens=5_000,
        max_output_tokens=1_000,
        wall_time_sec=60,
        scope_id=run_id,
    )
    first_metadata = _metadata(operation_id=operation_id)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=first_metadata,
        )

        budget.reserve(60, 50)
        with pytest.raises(LLMAttemptConflict, match="refusing redispatch"):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:one",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=first_metadata,
                pre_reserved=PreReservedAttempt(60, 50),
            )

        assert budget.calls_started == 1
        assert budget.input_tokens_reserved == 100
        assert budget.output_tokens_reserved == 50

        budget.reserve(60, 50)
        await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:two",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(),
            pre_reserved=PreReservedAttempt(60, 50),
        )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, run_id)

    assert len(rows) == 2
    assert scope is not None
    assert scope.calls_reserved == 2
    assert scope.input_tokens_reserved == 200
    assert scope.output_tokens_reserved == 100


@pytest.mark.asyncio
async def test_transient_start_failure_releases_current_local_pre_reservation(
    lifecycle_db,
    monkeypatch,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=3,
        max_input_tokens=1_000,
        max_output_tokens=500,
        wall_time_sec=60,
        scope_id=run_id,
    )
    budget.reserve(60, 50)

    async def fail_lock(_session, _scope_id):  # noqa: ANN001
        raise RuntimeError("transient lock failure")

    monkeypatch.setattr(lifecycle, "_advisory_scope_lock", fail_lock)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        with pytest.raises(RuntimeError, match="transient lock failure"):
            await begin_attempt(
                session,
                project_id=uuid.uuid4(),
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:one",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=_metadata(),
                pre_reserved=PreReservedAttempt(60, 50),
            )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()

    assert rows == []
    assert scopes == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0


@pytest.mark.asyncio
async def test_finalize_and_grounding_classification_update_the_same_attempt(
    lifecycle_db,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(),
        )
        usage = normalize_anthropic_usage(
            {"input_tokens": 80, "output_tokens": 20}
        )
        completed = ticket.started_at + timedelta(milliseconds=25)
        row = await finalize_attempt(
            session,
            ticket=ticket,
            outcome=AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.PENDING,
                usage=usage,
                resolved_model="claude-resolved",
                provider_request_id="msg_safe_123",
                finish_reason="end_turn",
            ),
            completed_at=completed,
        )

        assert row.id == ticket.attempt_id
        assert row.attempt_status == "completed"
        assert row.result_status == "pending"
        assert row.total_tokens == 100
        assert row.tokens_used == 100
        assert row.input_tokens == 80
        assert row.output_tokens == 20
        assert row.duration_ms == 25

        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )
        accepted = await session.get(LLMCall, ticket.attempt_id)

    assert accepted is not None
    assert accepted.result_status == "accepted"
    assert accepted.status == "completed"
    assert accepted.fallback_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_mode", "expected_mode"),
    [
        (None, "unknown"),
        ("unknown", "unknown"),
        ("api", "api"),
    ],
)
async def test_unknown_provider_mode_finalizes_once_to_observed_provenance(
    lifecycle_db,
    observed_mode: str | None,
    expected_mode: str,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    outcome = AttemptOutcome(
        attempt_status=AttemptStatus.COMPLETED,
        result_status=ResultStatus.PENDING,
        usage=normalize_anthropic_usage(
            {"input_tokens": 80, "output_tokens": 20}
        ),
        provider_mode=observed_mode,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id=f"node:auth:{expected_mode}",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="unknown"),
        )
        started = await session.get(LLMCall, ticket.attempt_id)
        assert started is not None and started.provider_mode == "unknown"

        first = await finalize_attempt(
            session, ticket=ticket, outcome=outcome
        )
        replay = await finalize_attempt(
            session, ticket=ticket, outcome=outcome
        )

    assert first.provider_mode == expected_mode
    assert replay.id == first.id
    assert replay.provider_mode == expected_mode


@pytest.mark.asyncio
async def test_unknown_provider_mode_rejects_unattested_subscription(
    lifecycle_db,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:auth:unattested",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="unknown"),
        )
        with pytest.raises(LLMAttemptConflict):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=AttemptOutcome(
                    attempt_status=AttemptStatus.COMPLETED,
                    result_status=ResultStatus.PENDING,
                    usage=normalize_anthropic_usage(
                        {"input_tokens": 80, "output_tokens": 20}
                    ),
                    provider_mode="subscription",
                ),
            )
        row = await session.get(LLMCall, ticket.attempt_id)

    assert row is not None
    assert row.attempt_status == "started"
    assert row.provider_mode == "unknown"


@pytest.mark.asyncio
async def test_provider_mode_override_cannot_rewrite_established_provenance(
    lifecycle_db,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    base = AttemptOutcome(
        attempt_status=AttemptStatus.COMPLETED,
        result_status=ResultStatus.PENDING,
        usage=normalize_anthropic_usage(
            {"input_tokens": 80, "output_tokens": 20}
        ),
        provider_mode="api",
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:auth:immutable",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="unknown"),
        )
        await finalize_attempt(session, ticket=ticket, outcome=base)
        for forbidden in ("subscription", "unknown", "custom"):
            conflicting = AttemptOutcome(
                attempt_status=base.attempt_status,
                result_status=base.result_status,
                usage=base.usage,
                provider_mode=forbidden,
            )
            with pytest.raises(LLMAttemptConflict):
                await finalize_attempt(
                    session, ticket=ticket, outcome=conflicting
                )
        row = await session.get(LLMCall, ticket.attempt_id)

    assert row is not None
    assert row.provider_mode == "api"


@pytest.mark.asyncio
async def test_started_known_provider_mode_rejects_different_override(
    lifecycle_db,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:auth:known",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(provider_mode="api"),
        )
        with pytest.raises(LLMAttemptConflict):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=AttemptOutcome(
                    attempt_status=AttemptStatus.COMPLETED,
                    result_status=ResultStatus.PENDING,
                    usage=normalize_anthropic_usage(
                        {"input_tokens": 80, "output_tokens": 20}
                    ),
                    provider_mode="subscription",
                ),
            )
        row = await session.get(LLMCall, ticket.attempt_id)

    assert row is not None
    assert row.attempt_status == "started"
    assert row.provider_mode == "api"


@pytest.mark.asyncio
async def test_persisted_policy_prevents_restart_from_resetting_call_limit(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    scope_id = run_id
    first_budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=scope_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=first_budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(),
        )

    restarted_budget = LLMRunBudget(
        max_calls=99,
        max_input_tokens=999_999,
        max_output_tokens=999_999,
        wall_time_sec=999,
        scope_id=scope_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        with pytest.raises(RunBudgetExceeded, match="run_call_limit_exceeded"):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:two",
                level=1,
                run_budget=restarted_budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=_metadata(),
            )
        rows = (await session.execute(select(LLMCall))).scalars().all()
        scope = await session.get(LLMBudgetScope, scope_id)

    assert len(rows) == 1
    assert scope is not None
    assert scope.max_calls == 1
    assert scope.calls_reserved == 1
    assert scope.exhausted_reason == "run_call_limit_exceeded"
    assert restarted_budget.max_calls == 1


@pytest.mark.asyncio
async def test_duplicate_operation_attempt_is_never_redispatched_or_reserved_twice(
    lifecycle_db,
) -> None:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=3,
        max_input_tokens=500,
        max_output_tokens=200,
        wall_time_sec=60,
        scope_id=run_id,
    )
    metadata = _metadata(operation_id=operation_id)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=metadata,
        )
        with pytest.raises(LLMAttemptConflict, match="refusing redispatch"):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:one",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=metadata,
            )
        scope = await session.get(LLMBudgetScope, budget.scope_id)

    assert scope is not None
    assert scope.calls_reserved == 1


@pytest.mark.asyncio
async def test_timeout_never_fabricates_zero_usage_and_terminal_replay_is_safe(
    lifecycle_db,
) -> None:
    run_id = uuid.uuid4()
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=500,
        max_output_tokens=100,
        wall_time_sec=60,
        scope_id=run_id,
    )
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        ticket = await begin_attempt(
            session,
            project_id=uuid.uuid4(),
            analysis_run_id=run_id,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:one",
            level=1,
            run_budget=budget,
            scope_kind=BudgetScopeKind.ANALYSIS_RUN,
            metadata=_metadata(),
        )
        outcome = AttemptOutcome(
            attempt_status=AttemptStatus.TIMEOUT,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.PROVIDER_API,
            ),
            failure_code="provider_timeout",
        )
        first = await finalize_attempt(session, ticket=ticket, outcome=outcome)
        replay = await finalize_attempt(session, ticket=ticket, outcome=outcome)

    assert replay.id == first.id
    assert replay.usage_status == "lost_after_dispatch"
    assert replay.tokens_used is None
    assert replay.total_tokens is None


@pytest.mark.asyncio
async def test_boolean_token_reservation_is_rejected_before_scope_write(
    lifecycle_db,
) -> None:
    invalid = _metadata()
    invalid = AttemptStartMetadata(
        operation_id=invalid.operation_id,
        attempt_no=invalid.attempt_no,
        attempt_kind=invalid.attempt_kind,
        provider=invalid.provider,
        provider_mode=invalid.provider_mode,
        requested_model=invalid.requested_model,
        input_fingerprint=invalid.input_fingerprint,
        estimated_input_tokens=True,  # type: ignore[arg-type]
        input_estimate_method=invalid.input_estimate_method,
        requested_max_output_tokens=invalid.requested_max_output_tokens,
        usage_source=invalid.usage_source,
    )
    run_id = uuid.uuid4()
    budget = LLMRunBudget(scope_id=run_id)
    async with AsyncSession(lifecycle_db, expire_on_commit=False) as session:
        with pytest.raises(ValueError, match="estimated_input_tokens"):
            await begin_attempt(
                session,
                project_id=uuid.uuid4(),
                analysis_run_id=run_id,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:one",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.ANALYSIS_RUN,
                metadata=invalid,
            )
        scopes = (await session.execute(select(LLMBudgetScope))).scalars().all()

    assert scopes == []
