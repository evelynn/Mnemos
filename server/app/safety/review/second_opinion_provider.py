"""Direct-Anthropic adapter for the Gate-B second-opinion contract.

This safety gate deliberately has no Claude Agent SDK fallback.  A hard
request budget needs a provider-enforced output limit, disabled SDK retries,
an exact resolved model, and provider usage.  The adapter returns only a
canonical v1 payload plus bounded accounting identifiers; raw response text
and exceptions never cross this module.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

from app.extractor.packing import (
    PROVIDER_INPUT_ENVELOPE_TOKENS,
    _approx_tokens,
)
from app.extractor.schema import ExtractorSchemaError, parse_json_object
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageContractError,
    ResultStatus,
    UsageSource,
    normalize_anthropic_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    SemanticCandidate,
    SemanticCandidateReplayRequest,
    current_attempt_callbacks,
    fingerprint_input,
)
from app.llm.privacy import enforce_provider_logging_privacy
from app.llm.pricing import OFFICIAL_ANTHROPIC_API_BASE_URL
from app.safety.review.second_opinion_schema import (
    SECOND_OPINION_CONTRACT,
    SecondOpinionSchemaError,
    normalize_second_opinion_candidate_payload,
)


log = logging.getLogger(__name__)

SECOND_OPINION_MODEL = "claude-sonnet-4-6"
SECOND_OPINION_MAX_OUTPUT_TOKENS = 1_024
SECOND_OPINION_WALL_TIMEOUT_SECONDS = 60.0
MAX_SECOND_OPINION_PROVIDER_OUTPUT_CHARS = 16_000
_LOWER_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

SECOND_OPINION_SYSTEM_PROMPT = (
    "You are Mnemos's independent Gate-B source-code diff reviewer. "
    "Return strict JSON only using contract mnemos.second_opinion.v1: "
    "{contract, findings:[{severity:info|warning|critical, "
    "category:security|correctness|data_loss|contract|performance|test_gap, "
    "message, evidence:[{kind:diff_line,path,side:old|new,line,"
    "content_sha256}]}]}. Report only NEW material concerns not already "
    "covered by prior_findings. Every finding must copy one or more exact "
    "diff_line references supplied in diff_lines. Never invent a path, line, "
    "or digest. Do not paste source code, credentials, or diff text into the "
    "message. Use findings:[] when there is no additional grounded concern."
)

FAILURE_UNAVAILABLE = "second_opinion_provider_unavailable"
FAILURE_CLIENT_INIT = "second_opinion_client_init_failed"
FAILURE_ACCOUNTING = "second_opinion_accounting_failed"
FAILURE_TIMEOUT = "second_opinion_timeout"
FAILURE_HTTP = "second_opinion_http_failed"
FAILURE_USAGE = "second_opinion_usage_invalid"
FAILURE_OUTPUT_LIMIT = "second_opinion_output_limit"
FAILURE_FINISH_REASON = "second_opinion_finish_reason_invalid"
FAILURE_MODEL = "second_opinion_model_mismatch"
FAILURE_EMPTY = "second_opinion_empty_response"
FAILURE_PARSE = "second_opinion_parse_rejected"
FAILURE_SCHEMA = "second_opinion_schema_rejected"


@dataclass(frozen=True, slots=True)
class SecondOpinionProviderResult:
    """Safe handoff from transport/shape validation to semantic grounding."""

    payload: dict[str, Any] | None
    attempt_id: uuid.UUID | None
    failure_code: str | None
    replayed: bool = False

    @property
    def complete(self) -> bool:
        return self.payload is not None and self.failure_code is None


def _safe_provider_field(value: Any, *, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _safe_provider_attribute(
    value: Any, name: str, *, maximum: int
) -> str | None:
    try:
        member = getattr(value, name, None)
    except Exception:  # noqa: BLE001 - hostile provider objects are data
        return None
    return _safe_provider_field(member, maximum=maximum)


async def review_via_anthropic(
    *,
    prompt: str,
    model: str = SECOND_OPINION_MODEL,
    operation_id: uuid.UUID | None = None,
    binding_fingerprint: str | None = None,
) -> SecondOpinionProviderResult:
    """Run one direct, lifecycle-accounted, bounded provider request."""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return SecondOpinionProviderResult(None, None, FAILURE_UNAVAILABLE)

    callbacks = current_attempt_callbacks()
    if callbacks is None:
        log.warning("second opinion refused: lifecycle callbacks unavailable")
        return SecondOpinionProviderResult(None, None, FAILURE_ACCOUNTING)
    finish_candidate = callbacks.finish_candidate
    replay_candidate = callbacks.replay_candidate
    if (
        finish_candidate is None
        or replay_candidate is None
        or not isinstance(operation_id, uuid.UUID)
        or not isinstance(binding_fingerprint, str)
        or _LOWER_SHA256.fullmatch(binding_fingerprint) is None
    ):
        log.warning(
            "second opinion refused: semantic candidate binding unavailable"
        )
        return SecondOpinionProviderResult(None, None, FAILURE_ACCOUNTING)

    try:
        enforce_provider_logging_privacy()
        import anthropic  # type: ignore

        async_anthropic = anthropic.AsyncAnthropic
        timeout_error_type = getattr(anthropic, "APITimeoutError", None)
    except (AttributeError, ImportError):
        log.warning("second opinion unavailable: Anthropic SDK missing")
        return SecondOpinionProviderResult(None, None, FAILURE_UNAVAILABLE)

    try:
        client = async_anthropic(
            api_key=api_key,
            base_url=OFFICIAL_ANTHROPIC_API_BASE_URL,
            timeout=SECOND_OPINION_WALL_TIMEOUT_SECONDS,
            max_retries=0,
        )
    except Exception as exc:  # noqa: BLE001 - never persist provider payload
        log.warning(
            "second opinion client initialization failed: %s",
            type(exc).__name__,
        )
        return SecondOpinionProviderResult(None, None, FAILURE_CLIENT_INIT)

    metadata = AttemptStartMetadata(
        operation_id=operation_id,
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="api",
        requested_model=model,
        input_fingerprint=fingerprint_input(
            SECOND_OPINION_SYSTEM_PROMPT,
            prompt,
        ),
        estimated_input_tokens=(
            _approx_tokens(
                {"system": SECOND_OPINION_SYSTEM_PROMPT, "prompt": prompt}
            )
            + PROVIDER_INPUT_ENVELOPE_TOKENS
        ),
        input_estimate_method="utf8_json_bytes_upper_v1",
        requested_max_output_tokens=SECOND_OPINION_MAX_OUTPUT_TOKENS,
        usage_source=UsageSource.PROVIDER_API,
        billing_route="anthropic_messages_api",
    )
    try:
        ticket = await callbacks.start(metadata)
    except LLMAttemptReplayBlocked:
        try:
            replay = await replay_candidate(
                SemanticCandidateReplayRequest(
                    operation_id=operation_id,
                    attempt_no=1,
                    input_fingerprint=metadata.input_fingerprint,
                    contract_name=SECOND_OPINION_CONTRACT,
                    binding_fingerprint=binding_fingerprint,
                    normalizer=normalize_second_opinion_candidate_payload,
                )
            )
            canonical = normalize_second_opinion_candidate_payload(
                replay.payload
            )
            if canonical != replay.payload:
                raise LLMLifecycleError(
                    "second-opinion replay payload is not canonical"
                )
        except LLMLifecycleError:
            return SecondOpinionProviderResult(
                None,
                None,
                FAILURE_ACCOUNTING,
            )
        except Exception as exc:  # noqa: BLE001 - keep diagnostics static
            log.warning(
                "second opinion candidate replay failed: %s",
                type(exc).__name__,
            )
            return SecondOpinionProviderResult(
                None,
                None,
                FAILURE_ACCOUNTING,
            )
        return SecondOpinionProviderResult(
            canonical,
            replay.attempt_id,
            None,
            replayed=True,
        )
    provider_timeout = min(
        SECOND_OPINION_WALL_TIMEOUT_SECONDS,
        ticket.remaining_seconds,
    )
    if provider_timeout <= 0:
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.TIMEOUT,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(source=UsageSource.PROVIDER_API),
                failure_code=FAILURE_TIMEOUT,
            ),
        )
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_TIMEOUT)

    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=SECOND_OPINION_MAX_OUTPUT_TOKENS,
                system=SECOND_OPINION_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=provider_timeout,
        )
    except asyncio.CancelledError:
        await asyncio.shield(
            callbacks.finish(
                ticket,
                AttemptOutcome(
                    attempt_status=AttemptStatus.CANCELLED,
                    result_status=ResultStatus.NOT_APPLICABLE,
                    usage=unavailable_usage(
                        lost_after_dispatch=True,
                        source=UsageSource.PROVIDER_API,
                    ),
                    failure_code="second_opinion_cancelled",
                ),
            )
        )
        raise
    except Exception as exc:  # noqa: BLE001 - raw provider error is forbidden
        is_timeout = isinstance(exc, TimeoutError) or (
            isinstance(timeout_error_type, type)
            and isinstance(exc, timeout_error_type)
        )
        failure = FAILURE_TIMEOUT if is_timeout else FAILURE_HTTP
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=(
                    AttemptStatus.TIMEOUT if is_timeout else AttemptStatus.FAILED
                ),
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(
                    lost_after_dispatch=True,
                    source=UsageSource.PROVIDER_API,
                ),
                failure_code=failure,
            ),
        )
        log.warning("second opinion provider call failed: %s", type(exc).__name__)
        return SecondOpinionProviderResult(None, ticket.attempt_id, failure)

    resolved_model = _safe_provider_attribute(response, "model", maximum=255)
    provider_request_id = _safe_provider_attribute(response, "id", maximum=255)
    finish_reason = _safe_provider_attribute(response, "stop_reason", maximum=64)
    try:
        usage = normalize_anthropic_usage(response)
    except LLMUsageContractError as exc:
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(source=UsageSource.PROVIDER_API),
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_USAGE,
                finish_reason=finish_reason,
            ),
        )
        log.warning("second opinion usage rejected: %s", type(exc).__name__)
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_USAGE)

    if finish_reason == "max_tokens":
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_OUTPUT_LIMIT,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(
            None, ticket.attempt_id, FAILURE_OUTPUT_LIMIT
        )
    if finish_reason != "end_turn":
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.FAILED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_FINISH_REASON,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(
            None, ticket.attempt_id, FAILURE_FINISH_REASON
        )
    if resolved_model != model:
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.FAILED,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_MODEL,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_MODEL)

    try:
        content = getattr(response, "content", None)
    except Exception:  # noqa: BLE001 - provider envelopes are untrusted
        content = None
    text_blocks: list[str] = []
    text_chars = 0
    if isinstance(content, (list, tuple)):
        for block in content:
            try:
                block_text = getattr(block, "text", None)
                block_type = getattr(block, "type", None)
            except Exception:  # noqa: BLE001 - provider envelopes are untrusted
                continue
            if isinstance(block_text, str) and (
                block_type == "text" or not isinstance(block_type, str)
            ):
                text_chars += len(block_text)
                if text_chars > MAX_SECOND_OPINION_PROVIDER_OUTPUT_CHARS:
                    await callbacks.finish(
                        ticket,
                        AttemptOutcome(
                            attempt_status=AttemptStatus.FAILED,
                            result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
                            usage=usage,
                            resolved_model=resolved_model,
                            provider_request_id=provider_request_id,
                            failure_code=FAILURE_OUTPUT_LIMIT,
                            finish_reason=finish_reason,
                        ),
                    )
                    return SecondOpinionProviderResult(
                        None, ticket.attempt_id, FAILURE_OUTPUT_LIMIT
                    )
                text_blocks.append(block_text)

    text = "\n".join(text_blocks)
    if not text.strip():
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.EMPTY,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_EMPTY,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_EMPTY)

    try:
        parsed = parse_json_object(text)
    except ExtractorSchemaError as exc:
        log.warning("second opinion JSON rejected: %s", type(exc).__name__)
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.PARSE_REJECTED,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_PARSE,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_PARSE)
    try:
        canonical = normalize_second_opinion_candidate_payload(parsed)
    except SecondOpinionSchemaError as exc:
        # Contract diagnostics contain paths and type/length descriptions only.
        log.warning("second opinion schema rejected: %s", exc)
        await callbacks.finish(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.SCHEMA_REJECTED,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=FAILURE_SCHEMA,
                finish_reason=finish_reason,
            ),
        )
        return SecondOpinionProviderResult(None, ticket.attempt_id, FAILURE_SCHEMA)

    await finish_candidate(
        ticket,
        AttemptOutcome(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PENDING,
            usage=usage,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        ),
        SemanticCandidate(
            contract_name=SECOND_OPINION_CONTRACT,
            binding_fingerprint=binding_fingerprint,
            payload=canonical,
        ),
    )
    return SecondOpinionProviderResult(canonical, ticket.attempt_id, None)


__all__ = [
    "SECOND_OPINION_MAX_OUTPUT_TOKENS",
    "SECOND_OPINION_MODEL",
    "SECOND_OPINION_SYSTEM_PROMPT",
    "SECOND_OPINION_WALL_TIMEOUT_SECONDS",
    "SecondOpinionProviderResult",
    "review_via_anthropic",
]
