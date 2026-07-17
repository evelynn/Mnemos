"""L1-L3 summarizer that wraps the Anthropic SDK.

Deliberately minimal in Phase 1:
- Builds prompts from adjacent graph rows (not whole files — spec §2.6).
- Produces the structured schema required by §10.3 (summary/detailed/claims/
  open_questions).
- Runs synchronously per target; the caller schedules invocations per L1-L3
  wave to keep context windows tight.

Backend priority (highest to lowest):
1. ``ANTHROPIC_API_KEY`` set + ``anthropic`` SDK importable → direct API
2. ``claude_agent_sdk`` importable + not opted out → Claude Code subscription
   (PR-125 — Mnemos running inside Claude Code uses the operator's existing
   subscription instead of requiring a separate API key)
3. Deterministic stub — pipeline-safe placeholder for CI / air-gapped

Failure-mode visibility (PR-138 — audit follow-up)
-------------------------------------------------
Pre-PR-138 every silent fallback collapsed into ``model_used="stub"``,
so an operator inspecting a Summary row could not tell "no LLM was
configured" from "Claude SDK timed out after 60s". That made debug
expensive and the §2 "every L1~L3 truthfully labelled" promise
unverifiable. The stub path now records ``model_used`` _and_
``fallback_reason`` (an enum string), plus increments a Prometheus
counter ``mnemos_llm_fallback_total{from,reason}`` so silent timeouts
finally show up on the operations dashboard.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from dataclasses import dataclass, field, replace as dataclass_replace
from typing import Any

from app.extractor.packing import (
    EvidencePromptTooLarge,
    MAX_EVIDENCE_PROMPT_CHARS,
    PROVIDER_INPUT_ENVELOPE_TOKENS,
    _approx_tokens,
    serialize_evidence,
)
from app.extractor.cost import RunBudgetExceeded
from app.extractor.agent_sdk import (
    AGENT_SDK_EMPTY_FAILURE,
    AGENT_SDK_OUTPUT_FAILURE,
    AGENT_SDK_PARSE_FAILURE,
    AGENT_SDK_RESULT_FAILURE,
    AGENT_SDK_SCHEMA_FAILURE,
    AGENT_SDK_TIMEOUT_FAILURE,
    AGENT_SDK_TRANSPORT_FAILURE,
    AGENT_SDK_USAGE_FAILURE,
    _canonical_from_summary_replay,
    _summary_candidate,
    _summary_replay_request,
    current_summary_candidate_context,
    summary_operation_id,
)
from app.extractor.schema import (
    ExtractorSchemaError,
    normalize_extractor_payload,
    parse_json_object,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageContractError,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    normalize_anthropic_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptReplayState,
    AttemptStartMetadata,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    fingerprint_input,
    require_attempt_callbacks,
)
from app.llm.privacy import enforce_provider_logging_privacy
from app.llm.pricing import OFFICIAL_ANTHROPIC_API_BASE_URL

log = logging.getLogger(__name__)

# PR-138 — distinguishable failure-mode labels. Every silent fallback
# stamps one of these onto ExtractorResult.fallback_reason so an
# operator inspecting summaries.model_used + the Prometheus counter
# can tell timeouts from missing-key from JSON-parse-error.
FALLBACK_NO_BACKEND = "no_backend"
FALLBACK_ANTHROPIC_IMPORT = "anthropic_import_error"
FALLBACK_ANTHROPIC_CLIENT_INIT = "anthropic_client_init_error"
FALLBACK_ANTHROPIC_HTTP = "anthropic_http_error"
FALLBACK_ANTHROPIC_TIMEOUT = "anthropic_timeout"
FALLBACK_ANTHROPIC_JSON = "anthropic_json_decode"
FALLBACK_ANTHROPIC_SCHEMA = "anthropic_schema_invalid"
FALLBACK_ANTHROPIC_USAGE = "anthropic_usage_contract_invalid"
FALLBACK_ANTHROPIC_OUTPUT_LIMIT = "anthropic_output_limit"
FALLBACK_ANTHROPIC_FINISH_REASON = "anthropic_finish_reason_invalid"
FALLBACK_ANTHROPIC_MODEL = "anthropic_model_invalid"
FALLBACK_AGENT_SDK_TIMEOUT = AGENT_SDK_TIMEOUT_FAILURE
FALLBACK_AGENT_SDK_INIT = "agent_sdk_init_error"
FALLBACK_AGENT_SDK_ERROR = AGENT_SDK_TRANSPORT_FAILURE
FALLBACK_AGENT_SDK_RESULT = AGENT_SDK_RESULT_FAILURE
FALLBACK_AGENT_SDK_OUTPUT_LIMIT = AGENT_SDK_OUTPUT_FAILURE
FALLBACK_AGENT_SDK_JSON = AGENT_SDK_PARSE_FAILURE
FALLBACK_AGENT_SDK_EMPTY = AGENT_SDK_EMPTY_FAILURE
FALLBACK_AGENT_SDK_SCHEMA = AGENT_SDK_SCHEMA_FAILURE
FALLBACK_AGENT_SDK_USAGE = AGENT_SDK_USAGE_FAILURE
FALLBACK_EVIDENCE_BUDGET = "evidence_budget_exceeded"

ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS = 1024
ANTHROPIC_SUMMARY_WALL_TIMEOUT_SECONDS = 60.0
ANTHROPIC_SUMMARY_SYSTEM_PROMPT = (
    "You are Mnemos's hierarchical summariser. Produce JSON "
    "that matches the schema: {summary, detailed, claims: "
    "[{claim, evidence: [{kind, edge_id|node_id, certainty}]}], "
    "open_questions}. Never invent evidence IDs. Only "
    "top-level node_id/edge_id fields in supplied evidence "
    "rows are citable; IDs mentioned inside prose or preview "
    "text are not evidence."
)


def _safe_provider_field(value: Any, *, maximum: int) -> str | None:
    """Return one bounded provider identifier without coercing payload data."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _record_fallback(from_backend: str, reason: str) -> None:
    """Tick the Prometheus counter so silent stub fallbacks become
    visible on the ops dashboard. Soft-fails if the metrics module
    isn't importable (CI / unit-test runs without app.obs)."""
    try:
        from app.obs.metrics import llm_fallback_total
        llm_fallback_total.labels(
            **{"from": from_backend, "reason": reason}
        ).inc()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class ExtractorResult:
    summary: str
    detailed: str
    claims: list[dict[str, Any]]
    open_questions: list[str]
    model_used: str
    tokens_used: int | None
    # PR-138 — when the result came from the stub path, this field
    # explains why so the operator can act (rotate key, install SDK,
    # tune timeout). Empty string on the happy path.
    fallback_reason: str = ""
    _llm_call_id: uuid.UUID | None = field(default=None, repr=False)


class Extractor:
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.model = model
        self._client: Any = None
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")
        # Tracks why this Extractor instance is about to fall back so
        # _stub() can stamp the right reason without weaving an extra
        # arg through every callsite.
        self._pending_reason = FALLBACK_NO_BACKEND
        self._pending_from = "none"
        self._pending_tokens: int | None = None
        # A task-local lifecycle callback sets this only after the durable
        # STARTED row commits.  The runner uses it to classify grounding or to
        # close a timeout without sharing mutable callback state globally.
        self._llm_call_id: uuid.UUID | None = None

    async def summarize(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult:
        """Summarise ``target_id`` from a bounded list of evidence rows."""
        # Reset per-call so a previous summarize()'s reason doesn't
        # leak into this one.
        self._pending_reason = FALLBACK_NO_BACKEND
        self._pending_from = "none"
        self._pending_tokens = None
        self._llm_call_id = None

        # All provider paths share one complete-JSON evidence contract.  A
        # caller that bypasses the runner's packer still fails closed before
        # importing or invoking any paid backend.
        try:
            serialize_evidence(evidence)
        except EvidencePromptTooLarge:
            self._pending_from = "extractor"
            self._pending_reason = FALLBACK_EVIDENCE_BUDGET
            _record_fallback(self._pending_from, self._pending_reason)
            return self._stub(
                level,
                target_id,
                [],
                FALLBACK_EVIDENCE_BUDGET,
            )

        # Path 1: direct Anthropic SDK if operator provided a key.
        if self._api_key:
            result = await self._summarize_via_anthropic_sdk(level, target_id, evidence)
            if result is not None:
                return result
            # Once a remote API request was attempted, never spend again on a
            # second backend for the same target.  Import failure happens
            # before a request and may safely try the isolated Agent SDK;
            # HTTP/JSON/schema failures become an explicit stub.
            if self._pending_reason != FALLBACK_ANTHROPIC_IMPORT:
                _record_fallback(self._pending_from, self._pending_reason)
                stub = self._stub(
                    level, target_id, evidence, self._pending_reason
                )
                stub.tokens_used = self._pending_tokens
                if self._llm_call_id is not None:
                    stub._llm_call_id = self._llm_call_id
                return stub

        # Path 2 (PR-125): Claude Agent SDK uses the active Claude Code
        # credential context. OAuth needs no API key, but API/cloud
        # credentials can take precedence; init provenance is classified in
        # the durable ledger rather than assumed here.
        from app.extractor.agent_sdk import (
            AgentSDKAttemptState,
            AgentSDKOutputLimitError,
            AgentSDKPreDispatchError,
            AgentSDKResultError,
            AgentSDKTransportError,
            AgentSDKUsageError,
            is_agent_sdk_available,
            summarize_via_agent_sdk,
        )
        if is_agent_sdk_available():
            # A direct-SDK import failure is pre-dispatch and may safely fall
            # through here.  Once this backend is selected, its outcome must
            # not inherit the earlier backend's reason.
            self._pending_from = "agent_sdk"
            self._pending_reason = FALLBACK_NO_BACKEND
            attempt_state = AgentSDKAttemptState()
            try:
                parsed = await summarize_via_agent_sdk(
                    model=self.model,
                    level=level,
                    target_id=target_id,
                    evidence=evidence,
                    attempt_state=attempt_state,
                )
            except (LLMLifecycleError, RunBudgetExceeded):
                # Accounting refused the call before provider dispatch.  The
                # owning runner must preserve that control-plane outcome.
                raise
            except AgentSDKPreDispatchError as exc:
                log.warning("agent_sdk setup failed: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_INIT
                parsed = None
            except AgentSDKOutputLimitError as exc:
                log.warning("agent_sdk output rejected: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_OUTPUT_LIMIT
                parsed = None
            except AgentSDKResultError as exc:
                log.warning("agent_sdk result failed: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_RESULT
                parsed = None
            except AgentSDKUsageError as exc:
                log.warning("agent_sdk usage failed: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_USAGE
                parsed = None
            except AgentSDKTransportError as exc:
                log.warning("agent_sdk transport failed: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_ERROR
                parsed = None
            except TimeoutError as exc:
                log.warning("agent_sdk timeout: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_TIMEOUT
                parsed = None
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_sdk path failed: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_ERROR
                parsed = None
            if attempt_state.attempt_id is not None:
                self._llm_call_id = attempt_state.attempt_id
            if attempt_state.total_tokens is not None:
                self._pending_tokens = attempt_state.total_tokens
            elif attempt_state.usage is not None:
                self._pending_tokens = attempt_state.usage.total_tokens
            if attempt_state.failure_code is not None:
                self._pending_reason = attempt_state.failure_code
            if parsed is None and self._pending_reason == FALLBACK_NO_BACKEND:
                # is_agent_sdk_available() was True, summarize_via_
                # agent_sdk() returned None without raising — that's
                # the "parser saw bad JSON" path.
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_JSON
            if parsed is not None:
                # ``summarize_via_agent_sdk`` already returns this canonical
                # shape.  Re-normalizing is intentionally idempotent and
                # protects this public boundary from alternate adapters or
                # test doubles returning an unchecked dict.
                try:
                    canonical = normalize_extractor_payload(parsed)
                except ExtractorSchemaError as exc:
                    log.warning("agent_sdk schema rejected: %s", exc)
                    self._pending_from = "agent_sdk"
                    self._pending_reason = FALLBACK_AGENT_SDK_JSON
                    canonical = None
                if canonical is None:
                    parsed = None
                else:
                    parsed = canonical
            if parsed is not None:
                return ExtractorResult(
                    summary=parsed["summary"],
                    detailed=parsed["detailed"],
                    claims=parsed["claims"],
                    open_questions=parsed["open_questions"],
                    model_used=(
                        f"{attempt_state.resolved_model or self.model}:agent_sdk"
                    ),
                    tokens_used=self._pending_tokens,
                    _llm_call_id=self._llm_call_id,
                )

        # Path 3: stub fallback — record the reason that brought us
        # here so the operator can tell apart "no backend configured"
        # from "timed out".
        if self._pending_reason != FALLBACK_NO_BACKEND:
            _record_fallback(self._pending_from, self._pending_reason)
        stub = self._stub(level, target_id, evidence, self._pending_reason)
        if self._pending_reason != FALLBACK_NO_BACKEND:
            stub.tokens_used = self._pending_tokens
        if self._llm_call_id is not None:
            stub._llm_call_id = self._llm_call_id
        return stub

    async def _summarize_via_anthropic_sdk(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult | None:
        """Direct Anthropic SDK call. Returns None on import/parse
        failure so the caller can try the next backend."""
        try:
            enforce_provider_logging_privacy()
            import anthropic  # type: ignore

            AsyncAnthropic = anthropic.AsyncAnthropic
            timeout_error_type = getattr(anthropic, "APITimeoutError", None)
        except (AttributeError, ImportError):
            log.warning("anthropic SDK missing; trying agent_sdk")
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_IMPORT
            return None

        if self._client is None:
            try:
                self._client = AsyncAnthropic(
                    api_key=self._api_key,
                    base_url=OFFICIAL_ANTHROPIC_API_BASE_URL,
                    timeout=60.0,
                    max_retries=0,
                )
            except Exception as exc:  # noqa: BLE001
                # The client may reject local configuration, but it cannot
                # have dispatched ``messages.create`` yet.  Keep this
                # distinct from an HTTP failure so the runner can release its
                # preflight reservation instead of inventing an attempt.
                log.warning(
                    "anthropic client initialization failed: %s",
                    type(exc).__name__,
                )
                self._pending_from = "anthropic"
                self._pending_reason = FALLBACK_ANTHROPIC_CLIENT_INIT
                return None

        prompt = self._prompt(level, target_id, evidence)
        callbacks = require_attempt_callbacks("direct Anthropic summary")
        candidate_context = current_summary_candidate_context()
        if candidate_context is not None and (
            callbacks.finish_candidate is None
            or (
                callbacks.replay_attempt is None
                and callbacks.replay_candidate is None
            )
        ):
            raise LLMLifecycleError(
                "summary semantic candidate callbacks are unavailable"
            )
        provider_timeout = ANTHROPIC_SUMMARY_WALL_TIMEOUT_SECONDS
        metadata = AttemptStartMetadata(
            operation_id=uuid.uuid4(),
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="anthropic",
            provider_mode="api",
            requested_model=self.model,
            input_fingerprint=fingerprint_input(
                ANTHROPIC_SUMMARY_SYSTEM_PROMPT,
                prompt,
            ),
            estimated_input_tokens=(
                _approx_tokens(
                    {
                        "system": ANTHROPIC_SUMMARY_SYSTEM_PROMPT,
                        "prompt": prompt,
                    }
                )
                + PROVIDER_INPUT_ENVELOPE_TOKENS
            ),
            input_estimate_method="utf8_json_bytes_upper_v1",
            requested_max_output_tokens=(ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS),
            usage_source=UsageSource.PROVIDER_API,
            billing_route="anthropic_messages_api",
        )
        if candidate_context is not None:
            metadata = dataclass_replace(
                metadata,
                operation_id=summary_operation_id(
                    candidate_context,
                    level=level,
                    target_id=target_id,
                ),
            )
        try:
            ticket = await callbacks.start(metadata)
        except LLMAttemptReplayBlocked:
            if candidate_context is None or (
                callbacks.replay_attempt is None
                and callbacks.replay_candidate is None
            ):
                raise
            replay_request = _summary_replay_request(
                metadata, context=candidate_context
            )
            if callbacks.replay_attempt is not None:
                replay = await callbacks.replay_attempt(replay_request)
                if replay.state == AttemptReplayState.STARTED:
                    raise LLMAttemptReplayBlocked(
                        "summary operation is still in progress"
                    )
                if replay.state == AttemptReplayState.ABSENT:
                    raise LLMLifecycleError(
                        "summary duplicate attempt disappeared"
                    )
                if replay.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
                    self._llm_call_id = replay.attempt_id
                    self._pending_tokens = replay.total_tokens
                    self._pending_from = "anthropic"
                    self._pending_reason = (
                        replay.failure_code or FALLBACK_ANTHROPIC_HTTP
                    )
                    return None
            else:
                replayer = callbacks.replay_candidate
                if replayer is None:  # pragma: no cover - guarded above
                    raise LLMLifecycleError(
                        "summary semantic replayer disappeared"
                    )
                replay = await replayer(replay_request)
            if replay.candidate_status == "rejected":
                self._llm_call_id = replay.attempt_id
                self._pending_tokens = replay.total_tokens
                self._pending_from = "anthropic"
                self._pending_reason = (
                    replay.failure_code or "ungrounded_response"
                )
                return None
            canonical = _canonical_from_summary_replay(replay)
            self._llm_call_id = replay.attempt_id
            self._pending_tokens = replay.total_tokens
            return ExtractorResult(
                summary=canonical["summary"],
                detailed=canonical["detailed"],
                claims=canonical["claims"],
                open_questions=canonical["open_questions"],
                model_used=replay.resolved_model or self.model,
                tokens_used=replay.total_tokens,
                _llm_call_id=replay.attempt_id,
            )
        self._llm_call_id = ticket.attempt_id
        provider_timeout = min(
            provider_timeout,
            ticket.remaining_seconds,
        )

        if provider_timeout <= 0:
            await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.TIMEOUT,
                        result_status=ResultStatus.NOT_APPLICABLE,
                        usage=unavailable_usage(
                            lost_after_dispatch=False,
                            source=UsageSource.PROVIDER_API,
                        ),
                        failure_code=FALLBACK_ANTHROPIC_TIMEOUT,
                    ),
                )
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_TIMEOUT
            return None

        if callbacks.mark_provider_dispatch is not None:
            callbacks.mark_provider_dispatch()
        try:
            response = await asyncio.wait_for(
                self._client.messages.create(
                    model=self.model,
                    max_tokens=ANTHROPIC_SUMMARY_MAX_OUTPUT_TOKENS,
                    system=ANTHROPIC_SUMMARY_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=provider_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            is_provider_timeout = isinstance(exc, TimeoutError) or (
                isinstance(timeout_error_type, type)
                and isinstance(exc, timeout_error_type)
            )
            attempt_status = (
                AttemptStatus.TIMEOUT
                if is_provider_timeout
                else AttemptStatus.FAILED
            )
            failure_code = (
                FALLBACK_ANTHROPIC_TIMEOUT
                if is_provider_timeout
                else FALLBACK_ANTHROPIC_HTTP
            )
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=attempt_status,
                        result_status=ResultStatus.NOT_APPLICABLE,
                        usage=unavailable_usage(
                            lost_after_dispatch=True,
                            source=UsageSource.PROVIDER_API,
                        ),
                        failure_code=failure_code,
                    ),
                )
            # Provider errors may embed request bodies or source snippets.
            log.warning("anthropic call failed: %s", type(exc).__name__)
            self._pending_from = "anthropic"
            self._pending_reason = failure_code
            return None

        canonical_usage: LLMUsageV1 = unavailable_usage(
            source=UsageSource.PROVIDER_API
        )
        resolved_model = _safe_provider_field(
            getattr(response, "model", None), maximum=255
        )
        provider_request_id = _safe_provider_field(
            getattr(response, "id", None), maximum=255
        )
        finish_reason = _safe_provider_field(
            getattr(response, "stop_reason", None), maximum=64
        )
        if callbacks is not None and ticket is not None:
            try:
                canonical_usage = normalize_anthropic_usage(response)
            except LLMUsageContractError as exc:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.COMPLETED,
                        result_status=ResultStatus.NOT_APPLICABLE,
                        usage=canonical_usage,
                        resolved_model=resolved_model,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_USAGE,
                        finish_reason=finish_reason,
                    ),
                )
                log.warning(
                    "anthropic usage contract rejected: %s",
                    type(exc).__name__,
                )
                self._pending_from = "anthropic"
                self._pending_reason = FALLBACK_ANTHROPIC_USAGE
                return None
            self._pending_tokens = canonical_usage.total_tokens
        else:
            # Compatibility path for callers that have not installed the
            # task-local durable lifecycle yet.
            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            self._pending_tokens = input_tokens + output_tokens
        if finish_reason == "max_tokens":
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.COMPLETED,
                        result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
                        usage=canonical_usage,
                        resolved_model=resolved_model,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_OUTPUT_LIMIT,
                        finish_reason=finish_reason,
                    ),
                )
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_OUTPUT_LIMIT
            return None
        if finish_reason != "end_turn":
            # This is a one-shot structured-output boundary.  A valid-looking
            # JSON block is not grounding-eligible when the provider reports
            # a tool request, refusal, pause, stop sequence, or no terminal
            # reason at all.
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.FAILED,
                        result_status=ResultStatus.NOT_APPLICABLE,
                        usage=canonical_usage,
                        resolved_model=resolved_model,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_FINISH_REASON,
                        finish_reason=finish_reason,
                    ),
                )
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_FINISH_REASON
            return None
        if resolved_model != self.model:
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.FAILED,
                        result_status=ResultStatus.NOT_APPLICABLE,
                        usage=canonical_usage,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_MODEL,
                        finish_reason=finish_reason,
                    ),
                )
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_MODEL
            return None
        # Anthropic returns tagged blocks. Tool/thinking/image blocks do not
        # expose text; ignore them and reject an envelope with no text rather
        # than raising or stringifying provider metadata.
        content = getattr(response, "content", None)
        text_blocks: list[str] = []
        if isinstance(content, (list, tuple)):
            for block in content:
                block_text = getattr(block, "text", None)
                block_type = getattr(block, "type", None)
                if (
                    isinstance(block_text, str)
                    and (
                        block_type == "text"
                        or not isinstance(block_type, str)
                    )
                ):
                    text_blocks.append(block_text)
        text = "\n".join(text_blocks)
        try:
            parsed = parse_json_object(text)
        except ExtractorSchemaError as exc:
            log.warning("anthropic: non-JSON response")
            log.debug("anthropic parse diagnostic: %s", exc)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_JSON
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.COMPLETED,
                        result_status=ResultStatus.PARSE_REJECTED,
                        usage=canonical_usage,
                        resolved_model=resolved_model,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_JSON,
                        finish_reason=finish_reason,
                    ),
                )
            return None

        try:
            parsed = normalize_extractor_payload(parsed)
        except ExtractorSchemaError as exc:
            # Do not store or partially salvage malformed model output.  The
            # diagnostic reports paths and sizes only; raw source/model text
            # is deliberately absent from logs.
            log.warning("anthropic schema rejected: %s", exc)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_SCHEMA
            if callbacks is not None and ticket is not None:
                await callbacks.finish(
                    ticket,
                    AttemptOutcome(
                        attempt_status=AttemptStatus.COMPLETED,
                        result_status=ResultStatus.SCHEMA_REJECTED,
                        usage=canonical_usage,
                        resolved_model=resolved_model,
                        provider_request_id=provider_request_id,
                        failure_code=FALLBACK_ANTHROPIC_SCHEMA,
                        finish_reason=finish_reason,
                    ),
                )
            return None

        if callbacks is not None and ticket is not None:
            outcome = AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.PENDING,
                usage=canonical_usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                finish_reason=finish_reason,
            )
            if candidate_context is not None:
                if callbacks.finish_candidate is None:
                    raise LLMLifecycleError(
                        "summary semantic candidate finalizer is unavailable"
                    )
                await callbacks.finish_candidate(
                    ticket,
                    outcome,
                    _summary_candidate(parsed, context=candidate_context),
                )
            else:
                await callbacks.finish(ticket, outcome)

        result = ExtractorResult(
            summary=parsed["summary"],
            detailed=parsed["detailed"],
            claims=parsed["claims"],
            open_questions=parsed["open_questions"],
            model_used=resolved_model or self.model,
            tokens_used=self._pending_tokens,
        )
        if self._llm_call_id is not None:
            result._llm_call_id = self._llm_call_id
        return result

    @staticmethod
    def _prompt(level: int, target_id: str, evidence: list[dict[str, Any]]) -> str:
        evidence_json = serialize_evidence(
            evidence,
            max_chars=MAX_EVIDENCE_PROMPT_CHARS,
        )
        return (
            f"L{level} summarisation target: {target_id}\n"
            "Graph evidence (complete JSON):\n"
            f"{evidence_json}\n\n"
            "Cite only top-level node_id/edge_id fields from those rows. "
            "Return strict JSON."
        )

    @staticmethod
    def _stub(
        level: int, target_id: str, evidence: list[dict[str, Any]],
        reason: str = FALLBACK_NO_BACKEND,
    ) -> ExtractorResult:
        # PR-138 — distinguishable model_used per fallback reason so
        # ``SELECT model_used FROM summaries`` answers "why did this
        # row miss the LLM?" without grepping logs.
        model_label = "stub" if reason == FALLBACK_NO_BACKEND \
            else f"stub:{reason}"
        summary = (
            f"[{model_label} L{level}] {target_id} summarised from "
            f"{len(evidence)} evidence rows."
        )
        claims = [
            {
                "claim": f"{target_id} is referenced in graph",
                "evidence": [
                    {"kind": "node", "node_id": target_id, "certainty": "asserted"}
                ],
            }
        ]
        return ExtractorResult(
            summary=summary,
            detailed=summary,
            claims=claims,
            open_questions=[],
            model_used=model_label,
            tokens_used=0,
            fallback_reason=reason,
        )
