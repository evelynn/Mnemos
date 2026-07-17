"""Crash-safe canonical Gate-B product persistence and replay."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# Import the extractor package's public cost module before the lifecycle.  The
# package initializer wires its adapters in this order in production too.
from app.extractor.cost import LLMRunBudget as _LLMRunBudget  # noqa: F401
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    ResultStatus,
    UsageSource,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    SemanticCandidate,
    current_attempt_callbacks,
    fingerprint_input,
)
from app.models import all_models as _all_models  # noqa: F401
from app.models.base import Base
from app.models.findings import LLMCall
from app.models.graph import AnalysisRun, GraphHead
from app.models.llm import LLMSemanticCandidate
from app.models.plans import Plan, SecondOpinionProduct
from app.models.projects import Project
from app.review_revision import CanonicalReviewRevision
from app.safety.crypto import decrypt
from app.safety.review import second_opinion
from app.safety.review.second_opinion_provider import (
    SECOND_OPINION_MODEL,
    SECOND_OPINION_SYSTEM_PROMPT,
    SecondOpinionProviderResult,
)
from app.safety.review.second_opinion_schema import (
    SECOND_OPINION_CONTRACT,
    normalize_second_opinion_candidate_payload,
)
from app.testing.graph_publication import published_run_fields
from app.testing.sqlite_polyglot import install_polyglot


DIFF = (
    "--- a/service.py\n"
    "+++ b/service.py\n"
    "@@ -10,1 +10,1 @@\n"
    "-return old_value\n"
    "+return new_value\n"
)
PROVIDER_PROSE_SENTINEL = (
    "provider-free-prose-sentinel must never persist"
)


@pytest_asyncio.fixture
async def product_database():
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


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
async def postgres_product_database():
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url)
    schema = f"mnemos_second_opinion_{uuid.uuid4().hex}"
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
        except (OSError, ConnectionError):
            pass
        await root_engine.dispose()


async def _seed(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, CanonicalReviewRevision]:
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    run_id = uuid.uuid4()
    published_at = datetime.now(tz=timezone.utc)
    async with factory() as session:
        session.add(
            Project(
                id=project_id,
                name="second-opinion-test",
                gitlab_project_id=1,
                gitlab_url="https://gitlab.example/mnemos/test",
                default_branch="main",
                languages=["python"],
            )
        )
        await session.flush()
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:second-opinion",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at,
                completed_at=published_at,
                **published_run_fields(
                    generation=1,
                    published_at=published_at,
                ),
            )
        )
        session.add(
            Plan(
                id=plan_id,
                project_id=project_id,
                status="approved",
                spec={"title": "second-opinion replay"},
                tasks=[],
                requester="test",
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=published_at,
            )
        )
        await session.commit()
    return (
        project_id,
        plan_id,
        CanonicalReviewRevision(
            project_id=project_id,
            run_id=run_id,
            source_generation=1,
            overlay_generation=0,
            git_sha="a" * 40,
        ),
    )


def _provider_payload(prompt: str, *, invented_digest: bool = False) -> dict:
    envelope = json.loads(prompt)
    reference = next(
        dict(line)
        for line in envelope["diff_lines"]
        if line["side"] == "new"
    )
    reference.pop("text")
    if invented_digest:
        reference["content_sha256"] = "f" * 64
    return {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": [
            {
                "severity": "warning",
                "category": "correctness",
                "message": PROVIDER_PROSE_SENTINEL,
                "evidence": [reference],
            }
        ],
    }


def _candidate_payload(
    prompt: str,
    *,
    invented_digest: bool = False,
) -> dict:
    return normalize_second_opinion_candidate_payload(
        _provider_payload(prompt, invented_digest=invented_digest)
    )


async def _start_and_finish(
    prompt: str,
    *,
    operation_id: uuid.UUID,
    binding_fingerprint: str,
    payload: dict,
) -> uuid.UUID:
    callbacks = current_attempt_callbacks()
    assert callbacks is not None
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=operation_id,
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="anthropic",
            provider_mode="api",
            requested_model=SECOND_OPINION_MODEL,
            input_fingerprint=fingerprint_input(
                SECOND_OPINION_SYSTEM_PROMPT,
                prompt,
            ),
            estimated_input_tokens=100,
            input_estimate_method="test_v1",
            requested_max_output_tokens=1_024,
            usage_source=UsageSource.PROVIDER_API,
            billing_route="anthropic_messages_api",
        )
    )
    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    assert callbacks.finish_candidate is not None
    await callbacks.finish_candidate(
        ticket,
        AttemptOutcome(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PENDING,
            usage=unavailable_usage(source=UsageSource.PROVIDER_API),
            resolved_model=SECOND_OPINION_MODEL,
            finish_reason="end_turn",
        ),
        SemanticCandidate(
            contract_name=SECOND_OPINION_CONTRACT,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        ),
    )
    return ticket.attempt_id


async def _start_and_finish_without_candidate(
    prompt: str,
    *,
    operation_id: uuid.UUID,
) -> uuid.UUID:
    callbacks = current_attempt_callbacks()
    assert callbacks is not None
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=operation_id,
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="anthropic",
            provider_mode="api",
            requested_model=SECOND_OPINION_MODEL,
            input_fingerprint=fingerprint_input(
                SECOND_OPINION_SYSTEM_PROMPT,
                prompt,
            ),
            estimated_input_tokens=100,
            input_estimate_method="test_v1",
            requested_max_output_tokens=1_024,
            usage_source=UsageSource.PROVIDER_API,
            billing_route="anthropic_messages_api",
        )
    )
    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    await callbacks.finish(
        ticket,
        AttemptOutcome(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PENDING,
            usage=unavailable_usage(source=UsageSource.PROVIDER_API),
            resolved_model=SECOND_OPINION_MODEL,
            finish_reason="end_turn",
        ),
    )
    return ticket.attempt_id


def _configure(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "1000")
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", raising=False)
    monkeypatch.setattr(second_opinion, "_accounting_session_factory", factory)

    async def budget_ok(_session, _project_id):
        return None

    monkeypatch.setattr(second_opinion, "require_budget", budget_ok)


async def _leave_candidate_before_product(
    *,
    monkeypatch: pytest.MonkeyPatch,
    project_id: uuid.UUID,
    plan_id: uuid.UUID,
    revision: CanonicalReviewRevision,
) -> tuple[second_opinion.SecondOpinionReview, dict[str, int]]:
    state = {"dispatches": 0}

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        state["dispatches"] += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    async def crash_before_product(**_kwargs):
        raise RuntimeError("private-crash-sentinel-must-not-persist")

    persist = second_opinion._persist_product_and_classify
    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    monkeypatch.setattr(
        second_opinion,
        "_persist_product_and_classify",
        crash_before_product,
    )
    crashed = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    monkeypatch.setattr(
        second_opinion,
        "_persist_product_and_classify",
        persist,
    )
    return crashed, state


@pytest.mark.asyncio
async def test_sqlite_completed_product_replays_without_provider_or_raw_diff(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    first = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert first.coverage == "complete"
    assert first.replayed is False
    assert replay.coverage == "complete"
    assert replay.replayed is True
    assert replay.findings == first.findings
    assert dispatches == 1
    async with product_database() as session:
        product = (
            await session.execute(select(SecondOpinionProduct))
        ).scalar_one()
        attempt = await session.get(LLMCall, product.attempt_id)
        candidate = await session.get(
            LLMSemanticCandidate,
            product.attempt_id,
        )
    persisted = json.dumps(product.canonical_payload, sort_keys=True)
    assert "return new_value" not in persisted
    assert PROVIDER_PROSE_SENTINEL not in persisted
    assert '"text"' not in persisted
    assert product.input_coverage == "complete"
    assert product.grounding_status == "fully_grounded"
    assert attempt is not None
    assert attempt.result_status == ResultStatus.ACCEPTED.value
    assert candidate is not None
    assert candidate.candidate_status == "accepted"
    assert b"return new_value" not in candidate.payload_ciphertext
    assert PROVIDER_PROSE_SENTINEL.encode() not in candidate.payload_ciphertext
    assert "return new_value" not in repr(candidate.__dict__)
    plaintext = decrypt(candidate.payload_ciphertext, candidate.payload_iv)
    assert "return new_value" not in plaintext
    assert PROVIDER_PROSE_SENTINEL not in plaintext
    assert "Mnemos candidate signal correctness:" in plaintext


@pytest.mark.asyncio
async def test_sqlite_accounting_only_pending_result_never_becomes_product(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        assert binding_fingerprint
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish_without_candidate(
            prompt,
            operation_id=operation_id,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    first = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    restarted = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert first.failure_code == second_opinion.FAILURE_ACCOUNTING
    assert restarted.failure_code == (
        second_opinion.FAILURE_TERMINAL_WITHOUT_PRODUCT
    )
    assert dispatches == 1
    async with product_database() as session:
        attempt = (await session.execute(select(LLMCall))).scalar_one()
        assert attempt.result_status == ResultStatus.PENDING.value
        assert await session.scalar(
            select(func.count()).select_from(LLMSemanticCandidate)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 0


@pytest.mark.asyncio
async def test_sqlite_provider_result_must_match_durable_candidate_digest(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        assert model == SECOND_OPINION_MODEL
        durable_payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=durable_payload,
        )
        mismatched_payload = json.loads(json.dumps(durable_payload))
        mismatched_payload["findings"][0]["severity"] = "info"
        return SecondOpinionProviderResult(
            mismatched_payload,
            attempt_id,
            None,
        )

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    result = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert result.failure_code == second_opinion.FAILURE_ACCOUNTING
    async with product_database() as session:
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        assert candidate.candidate_status == "candidate"
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 0


@pytest.mark.asyncio
async def test_sqlite_bad_source_generation_cannot_dispatch_or_persist(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)

    async def provider(**_kwargs):
        raise AssertionError("invalid publication generation must not dispatch")

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    result = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=replace(revision, source_generation=2),
    )

    assert result.failure_code == second_opinion.FAILURE_PRODUCT_INVALID
    async with product_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMCall)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(LLMSemanticCandidate)
        ) == 0
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 0


@pytest.mark.asyncio
async def test_sqlite_terminal_candidate_recovers_product_without_redispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    persist = second_opinion._persist_product_and_classify

    async def crash_before_atomic_product(**_kwargs):
        raise RuntimeError("must-not-be-persisted")

    monkeypatch.setattr(
        second_opinion,
        "_persist_product_and_classify",
        crash_before_atomic_product,
    )
    crashed = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    monkeypatch.setattr(
        second_opinion,
        "_persist_product_and_classify",
        persist,
    )
    restarted = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert crashed.failure_code == second_opinion.FAILURE_ACCOUNTING
    assert restarted.coverage == "complete"
    assert restarted.failure_code is None
    assert restarted.replayed is True
    assert dispatches == 1
    async with product_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(LLMSemanticCandidate)
        ) == 1
        attempt = (await session.execute(select(LLMCall))).scalar_one()
    assert attempt.attempt_status == AttemptStatus.COMPLETED.value
    assert attempt.result_status == ResultStatus.ACCEPTED.value


@pytest.mark.asyncio
async def test_sqlite_product_write_failure_rolls_back_classification(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    real_sqlite_insert = second_opinion.sqlite_insert

    def fail_product_insert(*_args, **_kwargs):
        raise RuntimeError("product-write-failure-sentinel")

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    monkeypatch.setattr(
        second_opinion,
        "sqlite_insert",
        fail_product_insert,
    )
    failed = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert failed.failure_code == second_opinion.FAILURE_ACCOUNTING
    assert dispatches == 1
    async with product_database() as session:
        attempt = (await session.execute(select(LLMCall))).scalar_one()
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        assert attempt.result_status == ResultStatus.PENDING.value
        assert attempt.failure_code is None
        assert candidate.candidate_status == "candidate"
        assert candidate.failure_code is None
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 0

    monkeypatch.setattr(
        second_opinion,
        "sqlite_insert",
        real_sqlite_insert,
    )
    recovered = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert recovered.coverage == "complete"
    assert recovered.replayed is True
    assert dispatches == 1
    async with product_database() as session:
        attempt = (await session.execute(select(LLMCall))).scalar_one()
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        assert attempt.result_status == ResultStatus.ACCEPTED.value
        assert candidate.candidate_status == "accepted"
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 1


@pytest.mark.asyncio
async def test_sqlite_accepted_candidate_rebuilds_missing_product_without_dispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    first = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    assert first.coverage == "complete"
    async with product_database() as session:
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        assert candidate.candidate_status == "accepted"
        await session.execute(delete(SecondOpinionProduct))
        await session.commit()

    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    assert replay.coverage == "complete"
    assert replay.replayed is True
    assert replay.findings == first.findings
    assert dispatches == 1
    async with product_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 1


@pytest.mark.asyncio
async def test_sqlite_terminal_without_candidate_fails_closed_without_dispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    crashed, state = await _leave_candidate_before_product(
        monkeypatch=monkeypatch,
        project_id=project_id,
        plan_id=plan_id,
        revision=revision,
    )
    assert crashed.failure_code == second_opinion.FAILURE_ACCOUNTING

    async with product_database() as session:
        await session.execute(delete(LLMSemanticCandidate))
        await session.commit()
    restarted = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert restarted.failure_code == (
        second_opinion.FAILURE_TERMINAL_WITHOUT_PRODUCT
    )
    assert state["dispatches"] == 1
    async with product_database() as session:
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 0


@pytest.mark.asyncio
async def test_sqlite_candidate_binding_mismatch_fails_closed_without_dispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    _crashed, state = await _leave_candidate_before_product(
        monkeypatch=monkeypatch,
        project_id=project_id,
        plan_id=plan_id,
        revision=revision,
    )
    async with product_database() as session:
        await session.execute(
            update(LLMSemanticCandidate).values(
                binding_fingerprint=fingerprint_input(
                    "tampered-second-opinion-binding"
                )
            )
        )
        await session.commit()

    restarted = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    assert restarted.failure_code == second_opinion.FAILURE_PRODUCT_INVALID
    assert state["dispatches"] == 1


@pytest.mark.asyncio
async def test_sqlite_corrupt_candidate_fails_closed_without_dispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
    caplog,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    crashed, state = await _leave_candidate_before_product(
        monkeypatch=monkeypatch,
        project_id=project_id,
        plan_id=plan_id,
        revision=revision,
    )
    assert "private-crash-sentinel-must-not-persist" not in caplog.text
    assert "private-crash-sentinel-must-not-persist" not in repr(crashed)
    async with product_database() as session:
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
        candidate.payload_ciphertext = b"A" * len(
            candidate.payload_ciphertext
        )
        await session.commit()

    restarted = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    assert restarted.failure_code == second_opinion.FAILURE_PRODUCT_INVALID
    assert state["dispatches"] == 1


@pytest.mark.asyncio
async def test_sqlite_rejected_reference_is_not_persisted_and_replays_incomplete(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt, invented_digest=True)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(
            payload,
            attempt_id,
            None,
        )

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    first = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert first.coverage == "incomplete"
    assert replay.coverage == "incomplete"
    assert replay.failure_code == second_opinion.FAILURE_GROUNDING
    assert replay.findings == ()
    assert dispatches == 1
    async with product_database() as session:
        product = (
            await session.execute(select(SecondOpinionProduct))
        ).scalar_one()
        attempt = await session.get(LLMCall, product.attempt_id)
    assert product.canonical_payload["findings"] == []
    assert "f" * 64 not in json.dumps(product.canonical_payload)
    assert product.rejected_findings == 1
    assert attempt is not None
    assert attempt.result_status == ResultStatus.GROUNDING_REJECTED.value


@pytest.mark.asyncio
async def test_sqlite_empty_diff_product_has_no_attempt_and_replays(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)

    async def provider(**_kwargs):
        raise AssertionError("empty diff must not instantiate a provider call")

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    first = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff="",
        prior_findings=[],
        review_revision=revision,
    )
    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff="",
        prior_findings=[],
        review_revision=revision,
    )

    assert first.coverage == "complete"
    assert replay.coverage == "complete"
    assert replay.replayed is True
    async with product_database() as session:
        product = (
            await session.execute(select(SecondOpinionProduct))
        ).scalar_one()
        call_count = await session.scalar(
            select(func.count()).select_from(LLMCall)
        )
    assert product.attempt_id is None
    assert call_count == 0


@pytest.mark.asyncio
async def test_sqlite_corrupt_product_fails_closed_without_redispatch(
    product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, plan_id, revision = await _seed(product_database)
    _configure(monkeypatch, product_database)
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal dispatches
        assert model == SECOND_OPINION_MODEL
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish(
            prompt,
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        dispatches += 1
        return SecondOpinionProviderResult(payload, attempt_id, None)

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    async with product_database() as session:
        await session.execute(
            update(SecondOpinionProduct).values(
                canonical_payload={
                    "contract": SECOND_OPINION_CONTRACT,
                    "findings": [],
                    "raw_response": "forbidden",
                }
            )
        )
        await session.commit()

    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )

    assert replay.coverage == "incomplete"
    assert replay.failure_code == second_opinion.FAILURE_PRODUCT_INVALID
    assert dispatches == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_terminal_candidate_recovers_after_product_crash(
    postgres_product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = postgres_product_database
    project_id, plan_id, revision = await _seed(factory)
    _configure(monkeypatch, factory)
    crashed, state = await _leave_candidate_before_product(
        monkeypatch=monkeypatch,
        project_id=project_id,
        plan_id=plan_id,
        revision=revision,
    )
    assert crashed.failure_code == second_opinion.FAILURE_ACCOUNTING

    replay = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=plan_id,
        diff=DIFF,
        prior_findings=[],
        review_revision=revision,
    )
    assert replay.coverage == "complete"
    assert replay.replayed is True
    assert state["dispatches"] == 1
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 1
        candidate = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalar_one()
    assert candidate.candidate_status == "accepted"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_concurrent_callers_dispatch_once_then_replay(
    postgres_product_database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = postgres_product_database
    project_id, plan_id, revision = await _seed(factory)
    _configure(monkeypatch, factory)
    both_entered = asyncio.Event()
    release_dispatch = asyncio.Event()
    entries = 0
    dispatches = 0

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        nonlocal entries, dispatches
        assert model == SECOND_OPINION_MODEL
        entries += 1
        if entries == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), timeout=10)
        payload = _candidate_payload(prompt)
        attempt_id = await _start_and_finish_after_release(
            prompt,
            release_dispatch,
            lambda: _increment_dispatch(),
            operation_id=operation_id,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        )
        return SecondOpinionProviderResult(payload, attempt_id, None)

    def _increment_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    async def invoke():
        return await second_opinion.run(
            None,
            project_id=project_id,
            plan_id=plan_id,
            diff=DIFF,
            prior_findings=[],
            review_revision=revision,
        )

    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)
    tasks = [asyncio.create_task(invoke()), asyncio.create_task(invoke())]
    await asyncio.wait_for(both_entered.wait(), timeout=10)
    done, _pending = await asyncio.wait(
        tasks,
        timeout=10,
        return_when=asyncio.FIRST_COMPLETED,
    )
    assert len(done) == 1
    release_dispatch.set()
    results = await asyncio.gather(*tasks)

    assert entries == 2
    assert dispatches == 1
    assert sorted(result.coverage for result in results) == [
        "complete",
        "incomplete",
    ]
    replay = await invoke()
    assert replay.coverage == "complete"
    assert replay.replayed is True
    assert dispatches == 1
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(LLMCall)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(SecondOpinionProduct)
        ) == 1


async def _start_and_finish_after_release(
    prompt: str,
    release: asyncio.Event,
    mark_dispatch,
    *,
    operation_id: uuid.UUID,
    binding_fingerprint: str,
    payload: dict,
) -> uuid.UUID:
    callbacks = current_attempt_callbacks()
    assert callbacks is not None
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=operation_id,
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="anthropic",
            provider_mode="api",
            requested_model=SECOND_OPINION_MODEL,
            input_fingerprint=fingerprint_input(
                SECOND_OPINION_SYSTEM_PROMPT,
                prompt,
            ),
            estimated_input_tokens=100,
            input_estimate_method="test_v1",
            requested_max_output_tokens=1_024,
            usage_source=UsageSource.PROVIDER_API,
            billing_route="anthropic_messages_api",
        )
    )
    mark_dispatch()
    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    await asyncio.wait_for(release.wait(), timeout=10)
    assert callbacks.finish_candidate is not None
    await callbacks.finish_candidate(
        ticket,
        AttemptOutcome(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PENDING,
            usage=unavailable_usage(source=UsageSource.PROVIDER_API),
            resolved_model=SECOND_OPINION_MODEL,
            finish_reason="end_turn",
        ),
        SemanticCandidate(
            contract_name=SECOND_OPINION_CONTRACT,
            binding_fingerprint=binding_fingerprint,
            payload=payload,
        ),
    )
    return ticket.attempt_id
