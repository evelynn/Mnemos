"""Test-only adapter that exercises the real durable LLM lifecycle."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncConnection

from app.extractor.agent import ExtractorResult
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    UsageStatus,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    fingerprint_input,
    require_attempt_callbacks,
)
from app.models.findings import (
    LLMCall,
    LLMBudgetScope,
    LLMProjectBudgetAccount,
)
from app.models.llm import LLMSemanticCandidate


async def create_llm_ledger_tables(connection: AsyncConnection) -> None:
    """Create the minimum lifecycle schema for focused SQLite tests."""

    for table in (
        LLMProjectBudgetAccount.__table__,
        LLMBudgetScope.__table__,
        LLMCall.__table__,
        LLMSemanticCandidate.__table__,
    ):
        await connection.run_sync(
            lambda sync_connection, current=table: current.create(
                sync_connection, checkfirst=True
            )
        )


async def finish_test_provider_result(
    result: ExtractorResult,
    *,
    target_id: str,
    input_text: str = "bounded-test-input",
) -> ExtractorResult:
    """Attach a real v1 terminal attempt to a deterministic fake product."""

    callbacks = require_attempt_callbacks("ledgered test provider")
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=uuid.uuid4(),
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="test",
            provider_mode="api",
            requested_model=result.model_used,
            input_fingerprint=fingerprint_input(
                "ledgered-test-system", input_text
            ),
            estimated_input_tokens=max(1, len(input_text.encode("utf-8"))),
            input_estimate_method="utf8_bytes_upper_v1",
            requested_max_output_tokens=1_024,
            usage_source=UsageSource.PROVIDER_API,
        )
    )
    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    tokens = int(result.tokens_used or 0)
    provider_failed = bool(result.fallback_reason)
    await callbacks.finish(
        ticket,
        AttemptOutcome(
            attempt_status=(
                AttemptStatus.FAILED
                if provider_failed
                else AttemptStatus.COMPLETED
            ),
            result_status=(
                ResultStatus.NOT_APPLICABLE
                if provider_failed
                else ResultStatus.PENDING
            ),
            usage=LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=tokens,
                output_tokens=0,
                total_tokens=tokens,
            ),
            failure_code=result.fallback_reason or None,
            resolved_model=(None if provider_failed else result.model_used),
        ),
    )
    setattr(result, "_llm_call_id", ticket.attempt_id)
    return result
