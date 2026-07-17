"""Canonical metadata for one physical LLM dispatch.

Provider response objects use incompatible token dialects.  This module is
the single normalization boundary for those *accounting* fields.  It never
accepts or retains prompts, source text, model output, or raw exceptions.

The functions are intentionally pure and strict:

* already-canonical input is returned unchanged;
* booleans, numeric strings, negative counts, and non-finite costs fail;
* conflicting provider totals and impossible subset relationships fail;
* absent usage remains explicitly unavailable rather than becoming zero.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, ClassVar


class LLMUsageContractError(ValueError):
    """A provider usage payload cannot be normalized without guessing."""


class UsageStatus(StrEnum):
    REPORTED_COMPLETE = "reported_complete"
    REPORTED_PARTIAL = "reported_partial"
    UNAVAILABLE = "unavailable"
    LOST_AFTER_DISPATCH = "lost_after_dispatch"
    LEGACY_TOTAL_ONLY = "legacy_total_only"
    LEGACY_UNKNOWN = "legacy_unknown"


class UsageSource(StrEnum):
    PROVIDER_API = "provider_api"
    AGENT_SDK = "agent_sdk"
    CONFIGURED_ESTIMATE = "configured_estimate"
    LEGACY = "legacy"
    UNKNOWN = "unknown"


class LLMPurpose(StrEnum):
    SUMMARY = "summary"
    AGENT_EXTRACT = "agent_extract"
    FLOW = "flow"
    CHAT_REWRITE = "chat_rewrite"
    CHAT_ANSWER = "chat_answer"
    SECOND_OPINION = "second_opinion"


class AttemptKind(StrEnum):
    PRIMARY = "primary"
    RETRY = "retry"
    FALLBACK = "fallback"


class AttemptStatus(StrEnum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ResultStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    EMPTY = "empty"
    PARSE_REJECTED = "parse_rejected"
    SCHEMA_REJECTED = "schema_rejected"
    GROUNDING_REJECTED = "grounding_rejected"
    OUTPUT_LIMIT_REJECTED = "output_limit_rejected"
    NOT_APPLICABLE = "not_applicable"


_TOKEN_FIELDS = (
    "input_tokens",
    "uncached_input_tokens",
    "output_tokens",
    "visible_output_tokens",
    "total_tokens",
    "cache_read_input_tokens",
    "cache_write_input_tokens",
    "reasoning_output_tokens",
    "tool_input_tokens",
    "provider_total_tokens",
    "provider_turn_count",
)
_MISSING = object()
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


def _strict_optional_int(value: Any, field_name: str) -> int | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise LLMUsageContractError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise LLMUsageContractError(f"{field_name} must be non-negative")
    return value


def _strict_optional_decimal(value: Any, field_name: str) -> Decimal | None:
    if value is _MISSING or value is None:
        return None
    if isinstance(value, bool) or isinstance(value, str):
        raise LLMUsageContractError(f"{field_name} must be a non-negative number")
    if not isinstance(value, (int, float, Decimal)):
        raise LLMUsageContractError(f"{field_name} must be a non-negative number")
    if isinstance(value, float) and not math.isfinite(value):
        raise LLMUsageContractError(f"{field_name} must be finite")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LLMUsageContractError(f"{field_name} must be finite") from exc
    if not normalized.is_finite() or normalized < 0:
        raise LLMUsageContractError(f"{field_name} must be non-negative and finite")
    return normalized


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, _MISSING)
    try:
        return getattr(value, name)
    except AttributeError:
        return _MISSING


def _has_any(value: Any, names: set[str]) -> bool:
    return any(_member(value, name) is not _MISSING for name in names)


def _unwrap(
    payload: Any,
    wrapper_name: str,
    recognized_fields: set[str],
) -> tuple[Any | None, Any]:
    """Return ``(usage, outer)`` while rejecting two competing locations."""

    nested = _member(payload, wrapper_name)
    if nested is _MISSING:
        return payload, payload
    if _has_any(payload, recognized_fields):
        raise LLMUsageContractError(
            f"usage fields appear both at the top level and in {wrapper_name}"
        )
    if nested is None:
        return None, payload
    if not isinstance(nested, Mapping) and not hasattr(nested, "__dict__"):
        raise LLMUsageContractError(f"{wrapper_name} must be an object")
    return nested, payload


def _optional_detail(payload: Any, field_name: str) -> Any | None:
    detail = _member(payload, field_name)
    if detail is _MISSING or detail is None:
        return None
    if not isinstance(detail, Mapping) and not hasattr(detail, "__dict__"):
        raise LLMUsageContractError(f"{field_name} must be an object")
    return detail


@dataclass(frozen=True, slots=True)
class LLMUsageV1:
    """Provider-neutral usage, with raw components kept as distinct facts.

    ``input_tokens`` is the effective input total, including cache reads and
    writes where the provider reports them.  ``output_tokens`` includes hidden
    reasoning when the provider's own output total includes it.  Provider-only
    components remain separate so no consumer has to reverse a lossy sum.
    """

    contract_version: ClassVar[str] = "mnemos.llm_usage.v1"

    status: UsageStatus
    source: UsageSource
    input_tokens: int | None = None
    uncached_input_tokens: int | None = None
    output_tokens: int | None = None
    visible_output_tokens: int | None = None
    total_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_write_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    tool_input_tokens: int | None = None
    provider_total_tokens: int | None = None
    provider_turn_count: int | None = None
    reported_cost_usd: Decimal | None = None
    estimated_cost_usd: Decimal | None = None
    pricing_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, UsageStatus):
            raise LLMUsageContractError("status must be a UsageStatus")
        if not isinstance(self.source, UsageSource):
            raise LLMUsageContractError("source must be a UsageSource")
        for name in _TOKEN_FIELDS:
            _strict_optional_int(getattr(self, name), name)
        if self.reported_cost_usd is not None:
            if not isinstance(self.reported_cost_usd, Decimal):
                raise LLMUsageContractError("reported_cost_usd must be canonical Decimal")
            _strict_optional_decimal(self.reported_cost_usd, "reported_cost_usd")
        if self.estimated_cost_usd is not None:
            if not isinstance(self.estimated_cost_usd, Decimal):
                raise LLMUsageContractError(
                    "estimated_cost_usd must be canonical Decimal"
                )
            _strict_optional_decimal(
                self.estimated_cost_usd, "estimated_cost_usd"
            )
        if (self.estimated_cost_usd is None) != (self.pricing_version is None):
            raise LLMUsageContractError(
                "estimated cost requires a pricing version"
            )
        if self.pricing_version is not None and (
            not isinstance(self.pricing_version, str)
            or not 1 <= len(self.pricing_version) <= 64
            or self.pricing_version.strip() != self.pricing_version
        ):
            raise LLMUsageContractError("pricing_version is invalid")

        if self.input_tokens is not None:
            for name in (
                "uncached_input_tokens",
                "cache_read_input_tokens",
                "cache_write_input_tokens",
            ):
                component = getattr(self, name)
                if component is not None and component > self.input_tokens:
                    raise LLMUsageContractError(f"{name} cannot exceed input_tokens")
            known_input_components = [
                value
                for value in (
                    self.uncached_input_tokens,
                    self.cache_read_input_tokens,
                    self.cache_write_input_tokens,
                )
                if value is not None
            ]
            if known_input_components and sum(known_input_components) > self.input_tokens:
                raise LLMUsageContractError(
                    "known input components cannot exceed input_tokens"
                )

        if self.output_tokens is not None:
            for name in ("visible_output_tokens", "reasoning_output_tokens"):
                component = getattr(self, name)
                if component is not None and component > self.output_tokens:
                    raise LLMUsageContractError(f"{name} cannot exceed output_tokens")
            if (
                self.visible_output_tokens is not None
                and self.reasoning_output_tokens is not None
                and self.visible_output_tokens + self.reasoning_output_tokens
                > self.output_tokens
            ):
                raise LLMUsageContractError(
                    "visible and reasoning output cannot exceed output_tokens"
                )

        if self.input_tokens is not None and self.output_tokens is not None:
            expected_total = self.input_tokens + self.output_tokens
            if self.total_tokens != expected_total:
                raise LLMUsageContractError(
                    "total_tokens must equal input_tokens + output_tokens"
                )
        elif self.total_tokens is not None:
            if self.input_tokens is not None and self.total_tokens < self.input_tokens:
                raise LLMUsageContractError("total_tokens cannot be below input_tokens")
            if self.output_tokens is not None and self.total_tokens < self.output_tokens:
                raise LLMUsageContractError("total_tokens cannot be below output_tokens")

        if (
            self.provider_total_tokens is not None
            and self.total_tokens is not None
            and self.provider_total_tokens != self.total_tokens
        ):
            raise LLMUsageContractError(
                "provider_total_tokens conflicts with canonical total_tokens"
            )

        measurements = [getattr(self, name) for name in _TOKEN_FIELDS]
        has_measurement = any(value is not None for value in measurements) or (
            self.reported_cost_usd is not None
            or self.estimated_cost_usd is not None
        )
        if self.status == UsageStatus.REPORTED_COMPLETE:
            if any(
                value is None
                for value in (self.input_tokens, self.output_tokens, self.total_tokens)
            ):
                raise LLMUsageContractError(
                    "reported_complete requires input, output, and total tokens"
                )
        elif self.status == UsageStatus.REPORTED_PARTIAL:
            if not has_measurement:
                raise LLMUsageContractError(
                    "reported_partial requires at least one reported measurement"
                )
        elif self.status == UsageStatus.LEGACY_TOTAL_ONLY:
            legacy_details = (
                self.input_tokens,
                self.uncached_input_tokens,
                self.output_tokens,
                self.visible_output_tokens,
                self.cache_read_input_tokens,
                self.cache_write_input_tokens,
                self.reasoning_output_tokens,
                self.tool_input_tokens,
                self.provider_turn_count,
                self.reported_cost_usd,
                self.estimated_cost_usd,
                self.pricing_version,
            )
            if self.total_tokens is None or any(
                value is not None for value in legacy_details
            ):
                raise LLMUsageContractError(
                    "legacy_total_only requires only a known total"
                )
        elif self.status in {
            UsageStatus.UNAVAILABLE,
            UsageStatus.LOST_AFTER_DISPATCH,
            UsageStatus.LEGACY_UNKNOWN,
        } and has_measurement:
            raise LLMUsageContractError(
                f"{self.status.value} cannot contain reported measurements"
            )
        if self.status in {
            UsageStatus.LEGACY_TOTAL_ONLY,
            UsageStatus.LEGACY_UNKNOWN,
        } and self.source != UsageSource.LEGACY:
            raise LLMUsageContractError("legacy usage status requires legacy source")
        if self.source == UsageSource.LEGACY and self.status not in {
            UsageStatus.LEGACY_TOTAL_ONLY,
            UsageStatus.LEGACY_UNKNOWN,
        }:
            raise LLMUsageContractError("legacy usage source requires legacy status")


def unavailable_usage(
    *,
    lost_after_dispatch: bool = False,
    source: UsageSource = UsageSource.UNKNOWN,
) -> LLMUsageV1:
    """Return an explicit unknown usage fact; never synthesize zero tokens."""

    if not isinstance(lost_after_dispatch, bool):
        raise LLMUsageContractError("lost_after_dispatch must be boolean")
    return LLMUsageV1(
        status=(
            UsageStatus.LOST_AFTER_DISPATCH
            if lost_after_dispatch
            else UsageStatus.UNAVAILABLE
        ),
        source=source,
    )


def _status_for(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None,
    *,
    has_other_measurement: bool = False,
) -> UsageStatus:
    if input_tokens is not None and output_tokens is not None and total_tokens is not None:
        return UsageStatus.REPORTED_COMPLETE
    if any(value is not None for value in (input_tokens, output_tokens, total_tokens)):
        return UsageStatus.REPORTED_PARTIAL
    if has_other_measurement:
        return UsageStatus.REPORTED_PARTIAL
    return UsageStatus.UNAVAILABLE


def normalize_openai_chat_usage(payload: Any) -> LLMUsageV1:
    """Normalize current OpenAI Chat Completions usage fields."""

    if isinstance(payload, LLMUsageV1):
        return payload
    if payload is None:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    chat_fields = {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_tokens_details",
        "completion_tokens_details",
    }
    usage, _outer = _unwrap(payload, "usage", chat_fields)
    if usage is None:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    if _has_any(
        usage,
        {"input_tokens", "output_tokens", "input_tokens_details", "output_tokens_details"},
    ):
        raise LLMUsageContractError(
            "OpenAI Chat usage contains a conflicting Responses API dialect"
        )

    input_tokens = _strict_optional_int(_member(usage, "prompt_tokens"), "prompt_tokens")
    output_tokens = _strict_optional_int(
        _member(usage, "completion_tokens"), "completion_tokens"
    )
    provider_total = _strict_optional_int(_member(usage, "total_tokens"), "total_tokens")
    prompt_details = _optional_detail(usage, "prompt_tokens_details")
    completion_details = _optional_detail(usage, "completion_tokens_details")
    cache_read = (
        _strict_optional_int(_member(prompt_details, "cached_tokens"), "cached_tokens")
        if prompt_details is not None
        else None
    )
    reasoning = (
        _strict_optional_int(
            _member(completion_details, "reasoning_tokens"), "reasoning_tokens"
        )
        if completion_details is not None
        else None
    )
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else provider_total
    )
    status = _status_for(
        input_tokens,
        output_tokens,
        total_tokens,
        has_other_measurement=cache_read is not None or reasoning is not None,
    )
    if status == UsageStatus.UNAVAILABLE:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    return LLMUsageV1(
        status=status,
        source=UsageSource.PROVIDER_API,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        reasoning_output_tokens=reasoning,
        provider_total_tokens=provider_total,
    )


def normalize_anthropic_usage(
    payload: Any,
    *,
    source: UsageSource = UsageSource.PROVIDER_API,
    reported_cost_usd: Any = _MISSING,
    provider_turn_count: Any = _MISSING,
) -> LLMUsageV1:
    """Normalize Anthropic Messages or an Anthropic-shaped SDK usage object."""

    if isinstance(payload, LLMUsageV1):
        return payload
    if (
        payload is None
        and reported_cost_usd is _MISSING
        and provider_turn_count is _MISSING
    ):
        return unavailable_usage(source=source)
    anthropic_fields = {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens_details",
        "total_tokens",
    }
    usage, outer = _unwrap(payload, "usage", anthropic_fields) if payload is not None else (None, None)

    if reported_cost_usd is _MISSING and outer is not None:
        reported_cost_usd = _member(outer, "total_cost_usd")
    if provider_turn_count is _MISSING and outer is not None:
        provider_turn_count = _member(outer, "num_turns")
    cost = _strict_optional_decimal(reported_cost_usd, "total_cost_usd")
    turns = _strict_optional_int(provider_turn_count, "num_turns")

    if usage is None:
        status = _status_for(None, None, None, has_other_measurement=cost is not None or turns is not None)
        if status == UsageStatus.UNAVAILABLE:
            return unavailable_usage(source=source)
        return LLMUsageV1(
            status=status,
            source=source,
            provider_turn_count=turns,
            reported_cost_usd=cost,
        )

    if _has_any(
        usage,
        {
            "prompt_tokens",
            "completion_tokens",
            "promptTokenCount",
            "candidatesTokenCount",
        },
    ):
        raise LLMUsageContractError("Anthropic usage contains a conflicting dialect")

    uncached = _strict_optional_int(_member(usage, "input_tokens"), "input_tokens")
    cache_write = _strict_optional_int(
        _member(usage, "cache_creation_input_tokens"),
        "cache_creation_input_tokens",
    )
    cache_read = _strict_optional_int(
        _member(usage, "cache_read_input_tokens"), "cache_read_input_tokens"
    )
    output_tokens = _strict_optional_int(_member(usage, "output_tokens"), "output_tokens")
    output_details = _optional_detail(usage, "output_tokens_details")
    reasoning = (
        _strict_optional_int(
            _member(output_details, "thinking_tokens"), "thinking_tokens"
        )
        if output_details is not None
        else None
    )
    provider_total = _strict_optional_int(_member(usage, "total_tokens"), "total_tokens")
    input_tokens = (
        uncached + (cache_write or 0) + (cache_read or 0)
        if uncached is not None
        else None
    )
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else provider_total
    )
    status = _status_for(
        input_tokens,
        output_tokens,
        total_tokens,
        has_other_measurement=any(
            value is not None
            for value in (cache_write, cache_read, reasoning, cost, turns)
        ),
    )
    if status == UsageStatus.UNAVAILABLE:
        return unavailable_usage(source=source)
    return LLMUsageV1(
        status=status,
        source=source,
        input_tokens=input_tokens,
        uncached_input_tokens=uncached,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        cache_write_input_tokens=cache_write,
        reasoning_output_tokens=reasoning,
        provider_total_tokens=provider_total,
        provider_turn_count=turns,
        reported_cost_usd=cost,
    )


def normalize_agent_sdk_usage(result_message: Any) -> LLMUsageV1:
    """Normalize a Claude Agent SDK ``ResultMessage`` without importing it."""

    estimate = _strict_optional_decimal(
        _member(result_message, "total_cost_usd"),
        "total_cost_usd",
    )
    usage = normalize_anthropic_usage(
        result_message,
        source=UsageSource.AGENT_SDK,
        # SDK total_cost_usd is a client-side price-table estimate, not a
        # provider billing fact.
        reported_cost_usd=None,
    )
    if estimate is None:
        return usage
    pricing_version = "claude-agent-sdk-0.2.118-client-estimate"
    if usage.status == UsageStatus.UNAVAILABLE:
        return LLMUsageV1(
            status=UsageStatus.REPORTED_PARTIAL,
            source=UsageSource.AGENT_SDK,
            estimated_cost_usd=estimate,
            pricing_version=pricing_version,
        )
    return replace(
        usage,
        estimated_cost_usd=estimate,
        pricing_version=pricing_version,
    )


def normalize_gemini_usage(payload: Any) -> LLMUsageV1:
    """Normalize Gemini ``usageMetadata`` without double-counting tool input."""

    if isinstance(payload, LLMUsageV1):
        return payload
    if payload is None:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    gemini_fields = {
        "promptTokenCount",
        "cachedContentTokenCount",
        "candidatesTokenCount",
        "toolUsePromptTokenCount",
        "thoughtsTokenCount",
        "totalTokenCount",
    }
    usage, _outer = _unwrap(payload, "usageMetadata", gemini_fields)
    if usage is None:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    if _has_any(
        usage,
        {
            "prompt_tokens",
            "completion_tokens",
            "input_tokens",
            "output_tokens",
        },
    ):
        raise LLMUsageContractError("Gemini usage contains a conflicting dialect")

    input_tokens = _strict_optional_int(
        _member(usage, "promptTokenCount"), "promptTokenCount"
    )
    cache_read = _strict_optional_int(
        _member(usage, "cachedContentTokenCount"), "cachedContentTokenCount"
    )
    visible = _strict_optional_int(
        _member(usage, "candidatesTokenCount"), "candidatesTokenCount"
    )
    reasoning = _strict_optional_int(
        _member(usage, "thoughtsTokenCount"), "thoughtsTokenCount"
    )
    tool_input = _strict_optional_int(
        _member(usage, "toolUsePromptTokenCount"), "toolUsePromptTokenCount"
    )
    provider_total = _strict_optional_int(
        _member(usage, "totalTokenCount"), "totalTokenCount"
    )
    output_tokens = (
        (visible or 0) + (reasoning or 0)
        if visible is not None or reasoning is not None
        else None
    )
    total_tokens = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else provider_total
    )
    status = _status_for(
        input_tokens,
        output_tokens,
        total_tokens,
        has_other_measurement=cache_read is not None or tool_input is not None,
    )
    if status == UsageStatus.UNAVAILABLE:
        return unavailable_usage(source=UsageSource.PROVIDER_API)
    return LLMUsageV1(
        status=status,
        source=UsageSource.PROVIDER_API,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        visible_output_tokens=visible,
        total_tokens=total_tokens,
        cache_read_input_tokens=cache_read,
        reasoning_output_tokens=reasoning,
        tool_input_tokens=tool_input,
        provider_total_tokens=provider_total,
    )


@dataclass(frozen=True, slots=True)
class LLMPhysicalAttemptV1:
    """Safe outer metadata envelope for one provider dispatch.

    Purpose-specific input and output intentionally do not appear in this
    value.  A future adapter returns them alongside this envelope, not inside
    the durable accounting fact.
    """

    contract_version: ClassVar[str] = "mnemos.llm_physical_attempt.v1"

    attempt_id: uuid.UUID
    operation_id: uuid.UUID
    budget_scope_id: uuid.UUID
    project_id: uuid.UUID
    purpose: LLMPurpose
    target_id: str
    attempt_no: int
    attempt_kind: AttemptKind
    provider: str
    provider_mode: str
    requested_model: str
    input_fingerprint: str
    estimated_input_tokens: int
    input_estimate_method: str
    requested_max_output_tokens: int
    attempt_status: AttemptStatus
    result_status: ResultStatus
    started_at: datetime
    usage: LLMUsageV1
    analysis_run_id: uuid.UUID | None = None
    parent_attempt_id: uuid.UUID | None = None
    resolved_model: str | None = None
    provider_request_id: str | None = None
    failure_code: str | None = None
    finish_reason: str | None = None
    completed_at: datetime | None = None
    duration_ms: int | None = None

    def __post_init__(self) -> None:
        for name in (
            "attempt_id",
            "operation_id",
            "budget_scope_id",
            "project_id",
        ):
            if not isinstance(getattr(self, name), uuid.UUID):
                raise LLMUsageContractError(f"{name} must be a UUID")
        if self.analysis_run_id is not None and not isinstance(
            self.analysis_run_id, uuid.UUID
        ):
            raise LLMUsageContractError("analysis_run_id must be a UUID")
        if self.parent_attempt_id is not None:
            if not isinstance(self.parent_attempt_id, uuid.UUID):
                raise LLMUsageContractError("parent_attempt_id must be a UUID")
            if self.parent_attempt_id == self.attempt_id:
                raise LLMUsageContractError("an attempt cannot be its own parent")
        for value, enum_type, name in (
            (self.purpose, LLMPurpose, "purpose"),
            (self.attempt_kind, AttemptKind, "attempt_kind"),
            (self.attempt_status, AttemptStatus, "attempt_status"),
            (self.result_status, ResultStatus, "result_status"),
        ):
            if not isinstance(value, enum_type):
                raise LLMUsageContractError(f"{name} has the wrong enum type")
        if not isinstance(self.usage, LLMUsageV1):
            raise LLMUsageContractError("usage must be LLMUsageV1")
        for name, limit in (
            ("target_id", 4096),
            ("provider", 32),
            ("provider_mode", 32),
            ("requested_model", 255),
            ("input_estimate_method", 32),
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise LLMUsageContractError(f"{name} must be a bounded non-empty string")
        for name, limit in (
            ("resolved_model", 255),
            ("provider_request_id", 255),
            ("failure_code", 64),
            ("finish_reason", 64),
        ):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > limit
            ):
                raise LLMUsageContractError(f"{name} must be a bounded string")
        if not isinstance(self.input_fingerprint, str) or not _HEX64.fullmatch(
            self.input_fingerprint
        ):
            raise LLMUsageContractError(
                "input_fingerprint must be 64 lowercase hexadecimal characters"
            )
        if (
            isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no < 1
        ):
            raise LLMUsageContractError("attempt_no must be a positive integer")
        estimated = _strict_optional_int(
            self.estimated_input_tokens, "estimated_input_tokens"
        )
        requested = _strict_optional_int(
            self.requested_max_output_tokens, "requested_max_output_tokens"
        )
        if estimated is None or requested is None or requested < 1:
            raise LLMUsageContractError(
                "attempt token reservations must be present and output must be positive"
            )
        if not isinstance(self.started_at, datetime) or self.started_at.tzinfo is None:
            raise LLMUsageContractError("started_at must be timezone-aware")
        if self.completed_at is not None and (
            not isinstance(self.completed_at, datetime)
            or self.completed_at.tzinfo is None
            or self.completed_at < self.started_at
        ):
            raise LLMUsageContractError(
                "completed_at must be timezone-aware and not precede started_at"
            )
        duration = _strict_optional_int(self.duration_ms, "duration_ms")
        if self.attempt_status == AttemptStatus.STARTED:
            if self.completed_at is not None or duration is not None:
                raise LLMUsageContractError(
                    "a started attempt cannot have terminal timing"
                )
            if self.result_status != ResultStatus.PENDING:
                raise LLMUsageContractError("a started attempt result must be pending")
        elif self.completed_at is None or duration is None:
            raise LLMUsageContractError(
                "a terminal attempt requires completed_at and duration_ms"
            )
        if (
            self.result_status == ResultStatus.ACCEPTED
            and self.attempt_status != AttemptStatus.COMPLETED
        ):
            raise LLMUsageContractError(
                "only a completed provider attempt can have an accepted result"
            )
