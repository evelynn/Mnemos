"""Focused contract tests for provider usage and the expand-only ledger."""

from __future__ import annotations

import importlib.util
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMPhysicalAttemptV1,
    LLMPurpose,
    LLMUsageContractError,
    ResultStatus,
    UsageSource,
    UsageStatus,
    normalize_agent_sdk_usage,
    normalize_anthropic_usage,
    normalize_gemini_usage,
    normalize_openai_chat_usage,
    unavailable_usage,
)
from app.models.findings import LLMCall


_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0036_llm_physical_attempt_contract.py"
)


def test_openai_chat_usage_preserves_cache_and_reasoning_components() -> None:
    usage = normalize_openai_chat_usage(
        {
            "id": "chatcmpl-safe-id",
            "choices": [{"message": {"content": "must-not-be-retained"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 30,
                "total_tokens": 130,
                "prompt_tokens_details": {"cached_tokens": 40},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        }
    )

    assert usage.status == UsageStatus.REPORTED_COMPLETE
    assert usage.input_tokens == 100
    assert usage.output_tokens == 30
    assert usage.total_tokens == 130
    assert usage.provider_total_tokens == 130
    assert usage.cache_read_input_tokens == 40
    assert usage.reasoning_output_tokens == 10
    assert "must-not-be-retained" not in repr(usage)
    assert "prompt" not in asdict(usage)


def test_anthropic_usage_keeps_uncached_cache_and_thinking_semantics() -> None:
    usage = normalize_anthropic_usage(
        {
            "usage": {
                "input_tokens": 20,
                "cache_creation_input_tokens": 7,
                "cache_read_input_tokens": 73,
                "output_tokens": 11,
                "output_tokens_details": {"thinking_tokens": 4},
            }
        }
    )

    assert usage.status == UsageStatus.REPORTED_COMPLETE
    assert usage.uncached_input_tokens == 20
    assert usage.cache_write_input_tokens == 7
    assert usage.cache_read_input_tokens == 73
    assert usage.input_tokens == 100
    assert usage.output_tokens == 11
    assert usage.reasoning_output_tokens == 4
    assert usage.total_tokens == 111


@dataclass(slots=True)
class _AgentResult:
    usage: dict[str, Any]
    total_cost_usd: float
    num_turns: int
    result: str = "must-not-be-retained"


def test_agent_sdk_result_normalizes_usage_cost_and_turn_count() -> None:
    result = _AgentResult(
        usage={
            "input_tokens": 5,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 4,
        },
        total_cost_usd=0.0125,
        num_turns=2,
    )

    usage = normalize_agent_sdk_usage(result)

    assert usage.source == UsageSource.AGENT_SDK
    assert usage.input_tokens == 10
    assert usage.output_tokens == 4
    assert usage.total_tokens == 14
    assert usage.provider_turn_count == 2
    assert usage.reported_cost_usd is None
    assert usage.estimated_cost_usd == Decimal("0.0125")
    assert usage.pricing_version == (
        "claude-agent-sdk-0.2.118-client-estimate"
    )
    assert result.result not in repr(usage)


def test_gemini_usage_does_not_double_count_tool_prompt_tokens() -> None:
    usage = normalize_gemini_usage(
        {
            "usageMetadata": {
                "promptTokenCount": 50,
                "cachedContentTokenCount": 20,
                "candidatesTokenCount": 7,
                "thoughtsTokenCount": 3,
                "toolUsePromptTokenCount": 12,
                "totalTokenCount": 60,
            }
        }
    )

    assert usage.input_tokens == 50
    assert usage.visible_output_tokens == 7
    assert usage.reasoning_output_tokens == 3
    assert usage.output_tokens == 10
    assert usage.tool_input_tokens == 12
    assert usage.total_tokens == 60


def test_unavailable_usage_is_explicit_null_not_zero_and_is_idempotent() -> None:
    unavailable = unavailable_usage()
    lost = unavailable_usage(lost_after_dispatch=True)

    assert unavailable.status == UsageStatus.UNAVAILABLE
    assert unavailable.total_tokens is None
    assert lost.status == UsageStatus.LOST_AFTER_DISPATCH
    assert normalize_openai_chat_usage(unavailable) is unavailable
    assert normalize_anthropic_usage(unavailable) is unavailable
    assert normalize_gemini_usage(unavailable) is unavailable


@pytest.mark.parametrize(
    ("normalizer", "payload"),
    [
        (
            normalize_openai_chat_usage,
            {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
        ),
        (
            normalize_openai_chat_usage,
            {"prompt_tokens": "1", "completion_tokens": 1, "total_tokens": 2},
        ),
        (
            normalize_openai_chat_usage,
            {"prompt_tokens": 1, "completion_tokens": -1, "total_tokens": 0},
        ),
        (
            normalize_openai_chat_usage,
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 3},
        ),
        (
            normalize_openai_chat_usage,
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        ),
        (
            normalize_openai_chat_usage,
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "completion_tokens_details": {"reasoning_tokens": 2},
            },
        ),
        (
            normalize_openai_chat_usage,
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "input_tokens": 1,
            },
        ),
        (
            normalize_anthropic_usage,
            {"input_tokens": 1, "output_tokens": "1"},
        ),
        (
            normalize_anthropic_usage,
            {
                "input_tokens": 1,
                "output_tokens": 1,
                "output_tokens_details": {"thinking_tokens": 2},
            },
        ),
        (
            normalize_anthropic_usage,
            {"input_tokens": 1, "output_tokens": 1, "prompt_tokens": 1},
        ),
        (
            normalize_gemini_usage,
            {
                "promptTokenCount": 1,
                "candidatesTokenCount": 1,
                "totalTokenCount": 3,
            },
        ),
        (
            normalize_gemini_usage,
            {
                "promptTokenCount": 1,
                "cachedContentTokenCount": 2,
                "candidatesTokenCount": 1,
                "totalTokenCount": 2,
            },
        ),
        (
            normalize_gemini_usage,
            {"promptTokenCount": 1, "candidatesTokenCount": False},
        ),
        (
            normalize_gemini_usage,
            {"promptTokenCount": 1, "candidatesTokenCount": 1, "input_tokens": 1},
        ),
    ],
)
def test_normalizers_reject_ambiguous_or_impossible_usage(
    normalizer: Any, payload: dict[str, Any]
) -> None:
    with pytest.raises(LLMUsageContractError):
        normalizer(payload)


def test_agent_sdk_rejects_string_cost_instead_of_coercing_it() -> None:
    result = _AgentResult(
        usage={"input_tokens": 1, "output_tokens": 1},
        total_cost_usd=0.1,
        num_turns=1,
    )
    result.total_cost_usd = "0.1"  # type: ignore[assignment]

    with pytest.raises(LLMUsageContractError):
        normalize_agent_sdk_usage(result)


def _started_attempt() -> LLMPhysicalAttemptV1:
    now = datetime.now(timezone.utc)
    return LLMPhysicalAttemptV1(
        attempt_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        budget_scope_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        purpose=LLMPurpose.FLOW,
        target_id="flow:checkout",
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="subscription",
        requested_model="claude-sonnet-4-6",
        input_fingerprint="a" * 64,
        estimated_input_tokens=100,
        input_estimate_method="utf8_bytes_upper_bound",
        requested_max_output_tokens=128,
        attempt_status=AttemptStatus.STARTED,
        result_status=ResultStatus.PENDING,
        started_at=now,
        usage=unavailable_usage(),
    )


def test_attempt_envelope_contains_only_safe_metadata_and_validates_lifecycle() -> None:
    attempt = _started_attempt()

    assert attempt.contract_version == "mnemos.llm_physical_attempt.v1"
    assert not hasattr(attempt, "prompt")
    assert not hasattr(attempt, "output")
    with pytest.raises(LLMUsageContractError):
        LLMPhysicalAttemptV1(
            **{
                name: getattr(attempt, name)
                for name in attempt.__dataclass_fields__
                if name not in {"contract_version", "input_fingerprint"}
            },
            input_fingerprint="not-a-fingerprint",
        )


def test_legacy_llm_call_shape_still_inserts_with_contract_v0() -> None:
    engine = create_engine("sqlite://")
    LLMCall.__table__.create(engine)
    call_id = uuid.uuid4()
    project_id = uuid.uuid4()

    with engine.begin() as connection:
        connection.execute(
            LLMCall.__table__.insert().values(
                id=call_id,
                project_id=project_id,
                analysis_run_id=None,
                target_id="legacy-target",
                level=1,
                model_used="legacy-model",
                tokens_used=None,
                status="completed",
                fallback_reason=None,
                generated_at=datetime.now(timezone.utc),
            )
        )
        row = connection.execute(
            select(LLMCall.__table__).where(LLMCall.id == call_id)
        ).mappings().one()

    assert row["contract_version"] == 0
    assert row["operation_id"] is None
    assert row["usage_status"] is None


def test_database_constraints_reject_impossible_v1_usage() -> None:
    engine = create_engine("sqlite://")
    LLMCall.__table__.create(engine)
    attempt_id = uuid.uuid4()
    now = datetime.now(timezone.utc)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            LLMCall.__table__.insert().values(
                id=attempt_id,
                project_id=uuid.uuid4(),
                target_id="summary:bad",
                level=1,
                model_used="model",
                status="completed",
                generated_at=now,
                contract_version=1,
                operation_id=uuid.uuid4(),
                budget_scope_id=uuid.uuid4(),
                purpose="summary",
                attempt_no=1,
                attempt_kind="primary",
                provider="anthropic",
                provider_mode="api",
                requested_model="model",
                input_fingerprint="b" * 64,
                estimated_input_tokens=10,
                input_estimate_method="test",
                requested_max_output_tokens=10,
                attempt_status="completed",
                result_status="accepted",
                started_at=now,
                completed_at=now,
                duration_ms=1,
                usage_status="reported_complete",
                usage_source="provider_api",
                input_tokens=5,
                cache_read_input_tokens=6,
                output_tokens=2,
                total_tokens=7,
                provider_total_tokens=7,
            )
        )


class _RecordedOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_llm_attempt_0036", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0036_migration_is_expand_only_and_backfills_without_provider_guessing() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder

    migration.upgrade()

    assert migration.down_revision == "0035_reset_token_invalidation"
    added = {
        args[1].name
        for name, args, _kwargs in recorder.calls
        if name == "add_column"
    }
    assert {
        "contract_version",
        "operation_id",
        "budget_scope_id",
        "purpose",
        "attempt_no",
        "provider",
        "input_tokens",
        "uncached_input_tokens",
        "visible_output_tokens",
        "reported_cost_usd",
        "reserved_cost_usd",
    } <= added
    update_sql = " ".join(
        str(args[0])
        for name, args, _kwargs in recorder.calls
        if name == "execute"
    ).lower()
    assert "operation_id = id" in update_sql
    assert "usage_status" in update_sql
    assert "legacy_total_only" in update_sql
    assert "legacy_unknown" in update_sql
    assert "provider =" not in update_sql
    assert "budget_scope_id =" not in update_sql
    assert "tokens_used = 0" not in update_sql


def test_0036_downgrade_guard_precedes_every_destructive_operation() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder

    migration.downgrade()

    names = [name for name, _args, _kwargs in recorder.calls]
    assert names[0] == "execute"
    guard = " ".join(str(recorder.calls[0][1][0]).lower().split())
    assert "contract_version = 1" in guard
    assert "cannot downgrade 0036" in guard
    assert "usage" in guard
    assert "components, cost provenance, or retry lineage" in guard
    assert names.index("execute") < names.index("drop_index")
    assert names.index("execute") < names.index("drop_column")


def test_model_and_migration_expose_the_same_contract_columns() -> None:
    migration = _load_migration()
    model_columns = set(LLMCall.__table__.columns.keys())

    assert set(migration._ADDED_COLUMNS) <= model_columns
    constraint_names = {
        constraint.name for constraint in LLMCall.__table__.constraints
    }
    assert {
        "uq_llm_calls_operation_attempt",
        "ck_llm_calls_contract_version",
        "ck_llm_calls_token_values",
        "ck_llm_calls_usage_relations",
        "ck_llm_calls_v1_shape",
        "ck_llm_calls_v1_enums",
        "ck_llm_calls_v1_lifecycle",
    } <= constraint_names
    index_names = {index.name for index in LLMCall.__table__.indexes}
    assert "ix_llm_calls_budget_scope" in index_names
    assert "ix_llm_calls_project_started" in index_names
