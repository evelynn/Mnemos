"""Encrypted, atomic semantic-candidate ledger invariants."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import app.cli as cli
import app.safety.crypto as crypto
from app.config import get_settings
from app.extractor.cost import LLMRunBudget
import app.llm.lifecycle as lifecycle
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    LLMPurpose,
    ResultStatus,
    UsageStatus,
    UsageSource,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptReplayState,
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMAttemptConflict,
    LLMAttemptReplayBlocked,
    LLMSemanticCandidateCorrupt,
    LLMSemanticCandidateError,
    LLMSemanticCandidateUnavailable,
    PreReservedAttempt,
    SemanticCandidate,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    load_attempt_replay,
    load_terminal_semantic_candidate,
)
from app.models.auth import Secret
from app.models.findings import (
    LLMCall,
    LLMBudgetScope,
    LLMProjectBudgetAccount,
)
from app.models.llm import LLMSemanticCandidate
from app.testing.sqlite_polyglot import install_polyglot


CONTRACT = "mnemos.test_candidate.v1"
PROMPT_SECRET = "prompt API_TOKEN=prompt-secret-must-not-be-stored"
PAYLOAD_SECRET = "provider copied source-secret-must-be-encrypted"


def _clear_crypto_cache() -> None:
    get_settings.cache_clear()  # type: ignore[attr-defined]
    if hasattr(crypto.get_kms, "cache_clear"):
        crypto.get_kms.cache_clear()


@pytest.fixture(autouse=True)
def _candidate_environment(monkeypatch: pytest.MonkeyPatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("MNEMOS_ENV", "test")
    monkeypatch.setenv("KMS_BACKEND", "local")
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", raising=False)
    _clear_crypto_cache()
    yield key
    _clear_crypto_cache()


async def _create_candidate_tables(
    engine: AsyncEngine,
    *,
    include_secret: bool = False,
) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        if include_secret:
            await connection.run_sync(Secret.__table__.create)


@pytest_asyncio.fixture
async def candidate_database():
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    await _create_candidate_tables(engine, include_secret=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _input_fingerprint() -> str:
    return fingerprint_input("system", PROMPT_SECRET)


def _binding_fingerprint(marker: str = "v1") -> str:
    return fingerprint_input("candidate-binding", marker)


def _payload(value: str = PAYLOAD_SECRET) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "findings": [{"kind": "test", "message": value}],
    }


def _normalize_test_candidate(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    if set(payload) != {"contract", "findings"}:
        raise ValueError("invalid test candidate keys")
    if payload.get("contract") != CONTRACT:
        raise ValueError("invalid test candidate contract")
    findings = payload.get("findings")
    if not isinstance(findings, list) or len(findings) != 1:
        raise ValueError("invalid test candidate findings")
    finding = findings[0]
    if (
        not isinstance(finding, dict)
        or set(finding) != {"kind", "message"}
        or finding.get("kind") != "test"
        or not isinstance(finding.get("message"), str)
    ):
        raise ValueError("invalid test candidate finding")
    return {
        "contract": CONTRACT,
        "findings": [
            {"kind": "test", "message": finding["message"]}
        ],
    }


def _metadata(operation_id: uuid.UUID) -> AttemptStartMetadata:
    return AttemptStartMetadata(
        operation_id=operation_id,
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="api",
        requested_model="claude-test",
        input_fingerprint=_input_fingerprint(),
        estimated_input_tokens=128,
        input_estimate_method="utf8_bytes_upper_v1",
        requested_max_output_tokens=64,
        usage_source=UsageSource.PROVIDER_API,
    )


def _budget(scope_id: uuid.UUID) -> LLMRunBudget:
    return LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000,
        max_output_tokens=256,
        wall_time_sec=120,
        scope_id=scope_id,
    )


def _outcome() -> AttemptOutcome:
    return AttemptOutcome(
        attempt_status=AttemptStatus.COMPLETED,
        result_status=ResultStatus.PENDING,
        usage=LLMUsageV1(
            status=UsageStatus.REPORTED_COMPLETE,
            source=UsageSource.PROVIDER_API,
            input_tokens=5,
            output_tokens=4,
            total_tokens=9,
        ),
        resolved_model="claude-test",
        finish_reason="end_turn",
    )


async def _begin(
    factory: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    scope_id: uuid.UUID,
    operation_id: uuid.UUID,
) -> AttemptTicket:
    async with factory() as session:
        return await begin_attempt(
            session,
            project_id=project_id,
            analysis_run_id=None,
            purpose=LLMPurpose.SUMMARY,
            target_id="node:semantic-candidate",
            level=1,
            run_budget=_budget(scope_id),
            scope_kind=BudgetScopeKind.REQUEST,
            metadata=_metadata(operation_id),
        )


@pytest.mark.asyncio
async def test_finalize_atomically_encrypts_candidate_and_replays_exactly(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )

    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)

    assert call is not None
    assert call.attempt_status == AttemptStatus.COMPLETED.value
    assert call.result_status == ResultStatus.PENDING.value
    assert stored is not None
    assert stored.operation_id == operation_id
    assert stored.input_fingerprint == _input_fingerprint()
    assert stored.contract_name == CONTRACT
    assert stored.binding_fingerprint == _binding_fingerprint()
    assert stored.candidate_status == "candidate"
    assert PAYLOAD_SECRET.encode() not in stored.payload_ciphertext
    assert PROMPT_SECRET.encode() not in stored.payload_ciphertext
    persisted_repr = repr(stored.__dict__)
    assert PAYLOAD_SECRET not in persisted_repr
    assert PROMPT_SECRET not in persisted_repr

    async with candidate_database() as session:
        replay = await load_terminal_semantic_candidate(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            input_fingerprint=_input_fingerprint(),
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )

    assert replay.attempt_id == ticket.attempt_id
    assert replay.attempt_status == AttemptStatus.COMPLETED
    assert replay.result_status == ResultStatus.PENDING
    assert replay.resolved_model == "claude-test"
    assert replay.total_tokens == 9
    assert replay.usage_status == UsageStatus.REPORTED_COMPLETE
    assert replay.payload == _payload()


@pytest.mark.asyncio
async def test_attempt_replay_uses_one_outer_join_snapshot_and_optional_input(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    async with candidate_database() as session:
        started = await load_attempt_replay(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert started.state == AttemptReplayState.STARTED
    assert started.attempt_status == AttemptStatus.STARTED
    assert started.result_status == ResultStatus.PENDING
    assert started.usage is not None
    assert started.usage.status == UsageStatus.UNAVAILABLE
    assert started.payload is None

    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )

    async with candidate_database() as session:
        statement_count = 0
        original_execute = session.execute

        async def counted_execute(*args, **kwargs):  # noqa: ANN002, ANN003
            nonlocal statement_count
            statement_count += 1
            return await original_execute(*args, **kwargs)

        monkeypatch.setattr(session, "execute", counted_execute)
        replay = await load_attempt_replay(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            # Summary replay is provider-neutral and intentionally omits the
            # exact provider-input fingerprint.
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )

    assert statement_count == 1
    assert replay.state == AttemptReplayState.TERMINAL_WITH_CANDIDATE
    assert replay.attempt_id == ticket.attempt_id
    assert replay.input_fingerprint == _input_fingerprint()
    assert replay.attempt_status == AttemptStatus.COMPLETED
    assert replay.result_status == ResultStatus.PENDING
    assert replay.provider == "anthropic"
    assert replay.provider_mode == "api"
    assert replay.requested_model == "claude-test"
    assert replay.resolved_model == "claude-test"
    assert replay.requested_max_output_tokens == 64
    assert replay.usage is not None
    assert replay.usage.total_tokens == 9
    assert replay.payload == _payload()


@pytest.mark.asyncio
async def test_attempt_replay_distinguishes_absent_and_terminal_without_candidate(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=AttemptOutcome(
                attempt_status=AttemptStatus.FAILED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=_outcome().usage,
                failure_code="provider_failed",
                finish_reason="provider_error",
            ),
        )

    async with candidate_database() as session:
        terminal = await load_attempt_replay(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert terminal.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE
    assert terminal.attempt_id == ticket.attempt_id
    assert terminal.attempt_status == AttemptStatus.FAILED
    assert terminal.result_status == ResultStatus.NOT_APPLICABLE
    assert terminal.failure_code == "provider_failed"
    assert terminal.provider == "anthropic"
    assert terminal.requested_model == "claude-test"
    assert terminal.requested_max_output_tokens == 64
    assert terminal.usage is not None
    assert terminal.usage.total_tokens == 9
    assert terminal.payload is None

    async with candidate_database() as session:
        absent = await load_attempt_replay(
            session,
            operation_id=uuid.uuid4(),
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert absent.state == AttemptReplayState.ABSENT
    assert absent.attempt_id is None
    assert absent.attempt_status is None
    assert absent.usage is None
    assert absent.payload is None

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateUnavailable,
            match="unavailable for terminal replay",
        ):
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                normalizer=_normalize_test_candidate,
            )


@pytest.mark.asyncio
async def test_terminal_replay_precedes_fresh_exhausted_and_new_dollar_policy(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    original_scope_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=original_scope_id,
        operation_id=operation_id,
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )

    # This policy did not exist for the original attempt. ``claude-test`` has
    # no price contract, so interpreting the new policy before duplicate
    # lookup would fail instead of replaying the durable result.
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    fresh_scope_id = uuid.uuid4()
    fresh_budget = LLMRunBudget(
        max_calls=0,
        max_input_tokens=1,
        max_output_tokens=1,
        wall_time_sec=120,
        scope_id=fresh_scope_id,
    )
    fresh_budget.stop("already_exhausted")
    budget_before = (
        fresh_budget.calls_started,
        fresh_budget.input_tokens_reserved,
        fresh_budget.output_tokens_reserved,
        fresh_budget.exhausted_reason,
        fresh_budget._durable_scope_id,
    )
    async with candidate_database() as session:
        with pytest.raises(LLMAttemptReplayBlocked):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:semantic-candidate",
                level=1,
                run_budget=fresh_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(operation_id),
            )
    budget_after = (
        fresh_budget.calls_started,
        fresh_budget.input_tokens_reserved,
        fresh_budget.output_tokens_reserved,
        fresh_budget.exhausted_reason,
        fresh_budget._durable_scope_id,
    )
    assert budget_after == budget_before

    async with candidate_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMBudgetScope)
        ) == 1
        assert await session.get(LLMBudgetScope, fresh_scope_id) is None
        assert await session.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount)
        ) == 0
    async with candidate_database() as session:
        replay = await load_attempt_replay(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.state == AttemptReplayState.TERMINAL_WITH_CANDIDATE
    assert replay.payload == _payload()


@pytest.mark.asyncio
async def test_duplicate_replay_restores_exact_local_pre_reservation(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1.0")
    replay_scope_id = uuid.uuid4()
    replay_budget = _budget(replay_scope_id)
    replay_budget.reserve(128, 64)

    async with candidate_database() as session:
        with pytest.raises(LLMAttemptReplayBlocked):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:semantic-candidate",
                level=1,
                run_budget=replay_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(operation_id),
                pre_reserved=PreReservedAttempt(128, 64),
            )

    assert replay_budget.calls_started == 0
    assert replay_budget.input_tokens_reserved == 0
    assert replay_budget.output_tokens_reserved == 0
    assert replay_budget.exhausted_reason is None
    assert replay_budget._durable_scope_id is None
    async with candidate_database() as session:
        assert await session.get(LLMBudgetScope, replay_scope_id) is None
        assert await session.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount)
        ) == 0


@pytest.mark.asyncio
async def test_begin_attempt_rechecks_after_project_lock_before_mutation(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def fake_check(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        events.append("check")
        if events == ["check", "lock", "check"]:
            raise LLMAttemptReplayBlocked("concurrent winner")

    async def fake_lock(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
        events.append("lock")

    def forbidden_policy(_metadata):  # noqa: ANN001
        events.append("dollar_policy")
        raise AssertionError("dollar policy ran before post-lock recheck")

    monkeypatch.setattr(
        lifecycle,
        "_assert_operation_dispatch_available",
        fake_check,
    )
    monkeypatch.setattr(
        lifecycle,
        "_advisory_project_budget_lock",
        fake_lock,
    )
    monkeypatch.setattr(lifecycle, "_dollar_reservation_for", forbidden_policy)
    budget = _budget(uuid.uuid4())
    budget.reserve(128, 64)

    async with candidate_database() as session:
        with pytest.raises(LLMAttemptReplayBlocked, match="concurrent winner"):
            await begin_attempt(
                session,
                project_id=uuid.uuid4(),
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:semantic-candidate",
                level=1,
                run_budget=budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(uuid.uuid4()),
                pre_reserved=PreReservedAttempt(128, 64),
            )

    assert events == ["check", "lock", "check"]
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0
    async with candidate_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMCall)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(LLMBudgetScope)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount)
        ) == 0


@pytest.mark.asyncio
async def test_candidate_classification_is_atomic_idempotent_and_replayable(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )
    async with candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )

    async with candidate_database() as session:
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
    assert call is not None
    assert stored is not None
    assert call.result_status == ResultStatus.ACCEPTED.value
    assert call.failure_code is None
    assert stored.candidate_status == "accepted"
    assert stored.failure_code is None

    async with candidate_database() as session:
        replay = await load_terminal_semantic_candidate(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            input_fingerprint=_input_fingerprint(),
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.candidate_status == "accepted"
    assert replay.result_status == ResultStatus.ACCEPTED
    assert replay.payload == _payload()


@pytest.mark.asyncio
async def test_classification_commit_false_composes_with_product_transaction(
    candidate_database,
) -> None:
    ticket = await _begin(
        candidate_database,
        project_id=uuid.uuid4(),
        scope_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )

    # Composition mode stages both CAS projections but leaves the outer
    # transaction owner free to roll back its product and classification.
    async with candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
            commit=False,
        )
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
        assert call is not None and stored is not None
        assert call.result_status == ResultStatus.ACCEPTED.value
        assert stored.candidate_status == "accepted"
        session.add(
            Secret(
                id=uuid.uuid4(),
                label="composed-semantic-product",
                kind="test_product",
                ciphertext=b"opaque-product",
                iv=b"0" * 16,
            )
        )
        assert session.in_transaction()
        await session.rollback()

    async with candidate_database() as session:
        rolled_back_call = await session.get(LLMCall, ticket.attempt_id)
        rolled_back_candidate = await session.get(
            LLMSemanticCandidate,
            ticket.attempt_id,
        )
        assert rolled_back_call is not None
        assert rolled_back_candidate is not None
        assert rolled_back_call.result_status == ResultStatus.PENDING.value
        assert rolled_back_candidate.candidate_status == "candidate"
        assert await session.scalar(
            select(func.count())
            .select_from(Secret)
            .where(Secret.label == "composed-semantic-product")
        ) == 0

    async with candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
            commit=False,
        )
        session.add(
            Secret(
                id=uuid.uuid4(),
                label="composed-semantic-product",
                kind="test_product",
                ciphertext=b"opaque-product",
                iv=b"0" * 16,
            )
        )
        await session.commit()

    async with candidate_database() as session:
        committed_call = await session.get(LLMCall, ticket.attempt_id)
        committed_candidate = await session.get(
            LLMSemanticCandidate,
            ticket.attempt_id,
        )
        assert committed_call is not None
        assert committed_candidate is not None
        assert committed_call.result_status == ResultStatus.ACCEPTED.value
        assert committed_candidate.candidate_status == "accepted"
        assert await session.scalar(
            select(func.count())
            .select_from(Secret)
            .where(Secret.label == "composed-semantic-product")
        ) == 1


@pytest.mark.asyncio
async def test_classification_commit_false_never_ends_caller_transaction(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = await _begin(
        candidate_database,
        project_id=uuid.uuid4(),
        scope_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )
    async with candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )

    async with candidate_database() as session:
        async def forbidden_commit() -> None:
            raise AssertionError("composition mode ended the transaction")

        monkeypatch.setattr(session, "commit", forbidden_commit)
        # Same-state idempotence must retain the caller-owned transaction.
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.ACCEPTED,
            commit=False,
        )
        assert session.in_transaction()
        # An error in composition mode also leaves rollback ownership with
        # the caller instead of silently ending its broader transaction.
        with pytest.raises(
            LLMAttemptConflict,
            match="replay changed its failure code",
        ):
            await classify_attempt_result(
                session,
                attempt_id=ticket.attempt_id,
                result_status=ResultStatus.ACCEPTED,
                failure_code="changed_failure",
                commit=False,
            )
        assert session.in_transaction()
        await session.rollback()


@pytest.mark.asyncio
async def test_candidate_rejection_requires_static_code_and_stays_consistent(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )

    async with candidate_database() as session:
        with pytest.raises(
            LLMAttemptConflict,
            match="requires a static failure code",
        ):
            await classify_attempt_result(
                session,
                attempt_id=ticket.attempt_id,
                result_status=ResultStatus.GROUNDING_REJECTED,
            )
    async with candidate_database() as session:
        pending_call = await session.get(LLMCall, ticket.attempt_id)
        pending_candidate = await session.get(
            LLMSemanticCandidate,
            ticket.attempt_id,
        )
    assert pending_call is not None
    assert pending_candidate is not None
    assert pending_call.result_status == ResultStatus.PENDING.value
    assert pending_candidate.candidate_status == "candidate"

    failure_code = "test_grounding_rejected"
    async with candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=ticket.attempt_id,
            result_status=ResultStatus.GROUNDING_REJECTED,
            failure_code=failure_code,
        )
    async with candidate_database() as session:
        rejected_call = await session.get(LLMCall, ticket.attempt_id)
        rejected_candidate = await session.get(
            LLMSemanticCandidate,
            ticket.attempt_id,
        )
    assert rejected_call is not None
    assert rejected_candidate is not None
    assert rejected_call.result_status == ResultStatus.GROUNDING_REJECTED.value
    assert rejected_call.failure_code == failure_code
    assert rejected_candidate.candidate_status == "rejected"
    assert rejected_candidate.failure_code == failure_code

    async with candidate_database() as session:
        replay = await load_terminal_semantic_candidate(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            input_fingerprint=_input_fingerprint(),
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.candidate_status == "rejected"
    assert replay.result_status == ResultStatus.GROUNDING_REJECTED


@pytest.mark.asyncio
async def test_replay_fails_closed_when_candidate_classification_drifts(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )
    async with candidate_database() as session:
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
        assert stored is not None
        stored.candidate_status = "accepted"
        await session.commit()

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateCorrupt,
            match="classification is inconsistent",
        ):
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                normalizer=_normalize_test_candidate,
            )


@pytest.mark.asyncio
async def test_terminal_candidate_finalize_is_idempotent_but_drift_is_rejected(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )
        replayed = await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )
        assert replayed.id == ticket.attempt_id
        with pytest.raises(
            LLMAttemptConflict,
            match="candidate replay changed",
        ):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=_outcome(),
                candidate=SemanticCandidate(
                    contract_name=CONTRACT,
                    binding_fingerprint=_binding_fingerprint(),
                    payload=_payload("different semantic product"),
                ),
            )

    async with candidate_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMSemanticCandidate)
        ) == 1


@pytest.mark.asyncio
async def test_candidate_encryption_failure_rolls_back_terminal_and_product(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    monkeypatch.setattr(lifecycle, "encrypt", lambda _value: (b"x", b"bad"))

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateError,
            match="encryption envelope is invalid",
        ):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=_outcome(),
                candidate=candidate,
            )

    async with candidate_database() as session:
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
    assert call is not None
    assert call.attempt_status == AttemptStatus.STARTED.value
    assert call.result_status == ResultStatus.PENDING.value
    assert call.completed_at is None
    assert stored is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result_status", "failure_code"),
    [
        (ResultStatus.ACCEPTED, None),
        (ResultStatus.PENDING, "provider_warning"),
    ],
)
async def test_candidate_finalize_requires_clean_pending_transport(
    candidate_database,
    result_status: ResultStatus,
    failure_code: str | None,
) -> None:
    ticket = await _begin(
        candidate_database,
        project_id=uuid.uuid4(),
        scope_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )
    base_outcome = _outcome()
    invalid_outcome = AttemptOutcome(
        attempt_status=AttemptStatus.COMPLETED,
        result_status=result_status,
        usage=base_outcome.usage,
        resolved_model=base_outcome.resolved_model,
        failure_code=failure_code,
        finish_reason=base_outcome.finish_reason,
    )
    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateError,
            match="completed pending transport",
        ):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=invalid_outcome,
                candidate=SemanticCandidate(
                    contract_name=CONTRACT,
                    binding_fingerprint=_binding_fingerprint(),
                    payload=_payload(),
                ),
            )

    async with candidate_database() as session:
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
    assert call is not None
    assert call.attempt_status == AttemptStatus.STARTED.value
    assert call.result_status == ResultStatus.PENDING.value
    assert stored is None


@pytest.mark.asyncio
async def test_replay_fails_closed_on_binding_contract_and_ciphertext_damage(
    candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateUnavailable,
            match="contract binding does not match",
        ):
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint("wrong"),
                normalizer=_normalize_test_candidate,
            )

    def leaking_normalizer(_payload: Mapping[str, Any]) -> Mapping[str, Any]:
        raise ValueError(PAYLOAD_SECRET)

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateCorrupt,
            match="contract validation failed",
        ) as exc_info:
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                normalizer=leaking_normalizer,
            )
    assert PAYLOAD_SECRET not in str(exc_info.value)

    async with candidate_database() as session:
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
        assert stored is not None
        stored.payload_ciphertext = b"A" * len(stored.payload_ciphertext)
        await session.commit()

    async with candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateCorrupt,
            match="decryption failed",
        ):
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                normalizer=_normalize_test_candidate,
            )


def test_semantic_candidate_constructor_is_strict_and_bounded() -> None:
    with pytest.raises(LLMSemanticCandidateError, match="contract name"):
        SemanticCandidate(
            contract_name="Bad Contract",
            binding_fingerprint=_binding_fingerprint(),
            payload={"contract": "Bad Contract"},
        )
    with pytest.raises(LLMSemanticCandidateError, match="does not match"):
        SemanticCandidate(
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            payload={"contract": "mnemos.other.v1"},
        )
    with pytest.raises(LLMSemanticCandidateError, match="non-finite"):
        SemanticCandidate(
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            payload={"contract": CONTRACT, "value": float("nan")},
        )
    with pytest.raises(LLMSemanticCandidateError, match="byte limit"):
        SemanticCandidate(
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            payload={"contract": CONTRACT, "value": "x" * 1_048_576},
        )


@pytest.mark.asyncio
async def test_rotate_fernet_key_rewraps_candidates_and_existing_secrets(
    candidate_database,
    monkeypatch: pytest.MonkeyPatch,
    _candidate_environment: str,
) -> None:
    old_key = _candidate_environment
    new_key = Fernet.generate_key().decode()
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    old_fernet = Fernet(old_key.encode())
    async with candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=candidate,
        )
        session.add(
            Secret(
                id=uuid.uuid4(),
                label="rotation-test-secret",
                kind="api_key",
                ciphertext=old_fernet.encrypt(b"existing-db-secret"),
                iv=os.urandom(16),
            )
        )
        await session.commit()

    monkeypatch.setattr(cli, "SessionLocal", candidate_database)
    assert await cli._rotate_fernet_key(old_key, new_key, False) == 0

    new_fernet = Fernet(new_key.encode())
    async with candidate_database() as session:
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
        secret = (
            await session.execute(
                select(Secret).where(Secret.label == "rotation-test-secret")
            )
        ).scalar_one()
    assert stored is not None
    plaintext = new_fernet.decrypt(stored.payload_ciphertext)
    assert PAYLOAD_SECRET.encode() in plaintext
    with pytest.raises(InvalidToken):
        old_fernet.decrypt(stored.payload_ciphertext)
    assert new_fernet.decrypt(secret.ciphertext) == b"existing-db-secret"

    monkeypatch.setenv("FERNET_KEY", new_key)
    _clear_crypto_cache()
    async with candidate_database() as session:
        replay = await load_terminal_semantic_candidate(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            input_fingerprint=_input_fingerprint(),
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.payload == _payload()


def _postgres_url() -> str | None:
    raw = os.environ.get("MNEMOS_TEST_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL", ""
    )
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql+"):
        return "postgresql+asyncpg://" + raw.split("://", 1)[1]
    return None


@pytest_asyncio.fixture
async def postgres_candidate_database():
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url, poolclass=NullPool)
    schema = f"mnemos_semantic_candidate_{uuid.uuid4().hex}"
    schema_engine: AsyncEngine | None = None
    try:
        try:
            async with root_engine.begin() as connection:
                await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        except (OSError, ConnectionError):
            pytest.skip("PostgreSQL service is unavailable")
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        await _create_candidate_tables(schema_engine)
        yield async_sessionmaker(schema_engine, expire_on_commit=False)
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        except (OSError, ConnectionError):
            pass
        await root_engine.dispose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_same_operation_dispatches_once_and_replays(
    postgres_candidate_database,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    scope_id = uuid.uuid4()

    async def start_once() -> AttemptTicket | Exception:
        try:
            return await _begin(
                postgres_candidate_database,
                project_id=project_id,
                scope_id=scope_id,
                operation_id=operation_id,
            )
        except Exception as exc:  # noqa: BLE001 - asserted structurally
            return exc

    results = await asyncio.gather(start_once(), start_once())
    tickets = [item for item in results if isinstance(item, AttemptTicket)]
    blocked = [
        item for item in results if isinstance(item, LLMAttemptReplayBlocked)
    ]
    assert len(tickets) == 1
    assert len(blocked) == 1

    candidate = SemanticCandidate(
        contract_name=CONTRACT,
        binding_fingerprint=_binding_fingerprint(),
        payload=_payload(),
    )
    async with postgres_candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=tickets[0],
            outcome=_outcome(),
            candidate=candidate,
        )
    async with postgres_candidate_database() as session:
        await classify_attempt_result(
            session,
            attempt_id=tickets[0].attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )
    async with postgres_candidate_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMCall)
        ) == 1
        scope = (
            await session.execute(select(LLMBudgetScope))
        ).scalar_one()
        assert scope.calls_reserved == 1
        assert scope.input_tokens_reserved == 128
        assert scope.output_tokens_reserved == 64
        assert await session.scalar(
            select(func.count()).select_from(LLMSemanticCandidate)
        ) == 1
        stored = await session.get(
            LLMSemanticCandidate,
            tickets[0].attempt_id,
        )
        assert stored is not None
        assert stored.candidate_status == "accepted"
        assert PAYLOAD_SECRET.encode() not in stored.payload_ciphertext
    async with postgres_candidate_database() as session:
        replay = await load_terminal_semantic_candidate(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            input_fingerprint=_input_fingerprint(),
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.payload == _payload()
    assert replay.candidate_status == "accepted"
    assert replay.result_status == ResultStatus.ACCEPTED

    async with postgres_candidate_database() as session:
        stored = await session.get(
            LLMSemanticCandidate,
            tickets[0].attempt_id,
        )
        assert stored is not None
        stored.payload_ciphertext = b"A" * len(stored.payload_ciphertext)
        await session.commit()
    async with postgres_candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateCorrupt,
            match="decryption failed",
        ):
            await load_terminal_semantic_candidate(
                session,
                operation_id=operation_id,
                attempt_no=1,
                project_id=project_id,
                purpose=LLMPurpose.SUMMARY,
                input_fingerprint=_input_fingerprint(),
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                normalizer=_normalize_test_candidate,
            )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_terminal_replay_ignores_new_policy_and_fresh_budget(
    postgres_candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = uuid.uuid4()
    operation_id = uuid.uuid4()
    ticket = await _begin(
        postgres_candidate_database,
        project_id=project_id,
        scope_id=uuid.uuid4(),
        operation_id=operation_id,
    )
    async with postgres_candidate_database() as session:
        await finalize_attempt(
            session,
            ticket=ticket,
            outcome=_outcome(),
            candidate=SemanticCandidate(
                contract_name=CONTRACT,
                binding_fingerprint=_binding_fingerprint(),
                payload=_payload(),
            ),
        )

    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "3600")
    fresh_scope_id = uuid.uuid4()
    fresh_budget = LLMRunBudget(
        max_calls=0,
        max_input_tokens=1,
        max_output_tokens=1,
        wall_time_sec=120,
        scope_id=fresh_scope_id,
        exhausted_reason="already_exhausted",
    )
    async with postgres_candidate_database() as session:
        with pytest.raises(LLMAttemptReplayBlocked):
            await begin_attempt(
                session,
                project_id=project_id,
                analysis_run_id=None,
                purpose=LLMPurpose.SUMMARY,
                target_id="node:semantic-candidate",
                level=1,
                run_budget=fresh_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=_metadata(operation_id),
            )

    assert fresh_budget.calls_started == 0
    assert fresh_budget.input_tokens_reserved == 0
    assert fresh_budget.output_tokens_reserved == 0
    assert fresh_budget.exhausted_reason == "already_exhausted"
    async with postgres_candidate_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMCall)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(LLMBudgetScope)
        ) == 1
        assert await session.get(LLMBudgetScope, fresh_scope_id) is None
        assert await session.scalar(
            select(func.count()).select_from(LLMProjectBudgetAccount)
        ) == 0
    async with postgres_candidate_database() as session:
        replay = await load_attempt_replay(
            session,
            operation_id=operation_id,
            attempt_no=1,
            project_id=project_id,
            purpose=LLMPurpose.SUMMARY,
            contract_name=CONTRACT,
            binding_fingerprint=_binding_fingerprint(),
            normalizer=_normalize_test_candidate,
        )
    assert replay.state == AttemptReplayState.TERMINAL_WITH_CANDIDATE
    assert replay.payload == _payload()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_candidate_encryption_failure_rolls_back_both_rows(
    postgres_candidate_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ticket = await _begin(
        postgres_candidate_database,
        project_id=uuid.uuid4(),
        scope_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
    )
    monkeypatch.setattr(lifecycle, "encrypt", lambda _value: (b"x", b"bad"))

    async with postgres_candidate_database() as session:
        with pytest.raises(
            LLMSemanticCandidateError,
            match="encryption envelope is invalid",
        ):
            await finalize_attempt(
                session,
                ticket=ticket,
                outcome=_outcome(),
                candidate=SemanticCandidate(
                    contract_name=CONTRACT,
                    binding_fingerprint=_binding_fingerprint(),
                    payload=_payload(),
                ),
            )

    async with postgres_candidate_database() as session:
        call = await session.get(LLMCall, ticket.attempt_id)
        stored = await session.get(LLMSemanticCandidate, ticket.attempt_id)
    assert call is not None
    assert call.attempt_status == AttemptStatus.STARTED.value
    assert call.result_status == ResultStatus.PENDING.value
    assert call.completed_at is None
    assert stored is None


def _load_0040_migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0040_llm_semantic_products.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_test_0040_llm_semantic_products",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_0040_upgrade_and_empty_downgrade_execute_real_ddl() -> None:
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    engine = create_async_engine(url, poolclass=NullPool)
    schema = f"mnemos_migration_0040_{uuid.uuid4().hex}"
    migration = _load_0040_migration()

    def run_upgrade(sync_connection) -> None:  # noqa: ANN001
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            migration.upgrade()

    def run_downgrade(sync_connection) -> None:  # noqa: ANN001
        context = MigrationContext.configure(sync_connection)
        with Operations.context(context):
            migration.downgrade()

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.execute(
                text(f'SET LOCAL search_path TO "{schema}"')
            )
            await connection.execute(
                text("CREATE TABLE llm_calls (id uuid PRIMARY KEY)")
            )
            await connection.run_sync(run_upgrade)
            exists = await connection.scalar(
                text("SELECT to_regclass('llm_semantic_products')")
            )
            assert exists == "llm_semantic_products"
            constraints = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_constraint c "
                    "JOIN pg_class t ON t.oid = c.conrelid "
                    "JOIN pg_namespace n ON n.oid = t.relnamespace "
                    "WHERE n.nspname = :schema "
                    "AND t.relname = 'llm_semantic_products'"
                ),
                {"schema": schema},
            )
            assert int(constraints or 0) >= 8
            await connection.run_sync(run_downgrade)
            assert await connection.scalar(
                text("SELECT to_regclass('llm_semantic_products')")
            ) is None
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        except (OSError, ConnectionError):
            pass
        await engine.dispose()
