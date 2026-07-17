"""PR-125 — Claude Agent SDK backend for L1~L3 extractor.

User point made: Mnemos's "L3 LLM summary unverified" excuse was
wrong. Mnemos can run beside Claude Code, whose Agent SDK exposes its local
credential route as a one-shot ``query()`` API. That route may be OAuth,
an API key, or cloud credentials; the v1 ledger starts unknown and classifies
only the SDK init message at terminal finalization.

This module wires the SDK into the extractor. Priority chain:
1. ``ANTHROPIC_API_KEY`` set + anthropic SDK present → use the
   direct Anthropic SDK (existing PR-119 path)
2. ``claude_agent_sdk`` importable AND not opted out → use it
3. Fall back to deterministic stub (PR-119 stub path)

The SDK path is the one that activates when Mnemos is deployed
alongside Claude Code (or on a developer's laptop with the
subscription). Production deployments with a server-side API key
still use path 1.

Operator opt-out: ``MNEMOS_DISABLE_AGENT_SDK=1`` skips path 2
even if the SDK is installed — useful in air-gapped envs.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace as dataclass_replace
from typing import Any, Iterator

from app.extractor.cost import RunBudgetExceeded
from app.extractor.packing import (
    EvidencePromptTooLarge,
    serialize_evidence,
)
from app.extractor.schema import (
    ExtractorSchemaError,
    normalize_extractor_payload,
    parse_json_object,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    normalize_agent_sdk_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptReplayResult,
    AttemptReplayState,
    AttemptStartMetadata,
    AttemptTicket,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    LLMSemanticCandidateUnavailable,
    SemanticCandidate,
    SemanticCandidateReplay,
    SemanticCandidateReplayRequest,
    fingerprint_input,
    require_attempt_callbacks,
)

log = logging.getLogger(__name__)

# The canonical summary schema is intentionally concise (direct Anthropic is
# capped at 1,024 output tokens). Bound untrusted streamed UTF-8 bytes, not
# Python code points: four-byte Unicode must consume four bytes of the memory
# and encrypted-candidate envelope budget.
MAX_AGENT_SUMMARY_OUTPUT_BYTES = 64 * 1024
SUMMARY_CANDIDATE_CONTRACT = "mnemos.summary.v1"


@dataclass(frozen=True, slots=True)
class SummaryCandidateContext:
    """Runner-owned identity needed by both supported Summary adapters."""

    project_id: uuid.UUID
    binding_fingerprint: str


_SUMMARY_CANDIDATE_CONTEXT: contextvars.ContextVar[
    SummaryCandidateContext | None
] = contextvars.ContextVar("mnemos_summary_candidate_context", default=None)


@contextmanager
def use_summary_candidate_context(
    context: SummaryCandidateContext,
) -> Iterator[None]:
    token = _SUMMARY_CANDIDATE_CONTEXT.set(context)
    try:
        yield
    finally:
        _SUMMARY_CANDIDATE_CONTEXT.reset(token)


def current_summary_candidate_context() -> SummaryCandidateContext | None:
    return _SUMMARY_CANDIDATE_CONTEXT.get()


def summary_operation_id(
    context: SummaryCandidateContext,
    *,
    level: int,
    target_id: str,
) -> uuid.UUID:
    """Return one provider-neutral semantic Summary operation identity.

    Provider/model/input request facts belong to the physical attempt row,
    not to the product identity.  Keeping them out of this UUID prevents a
    restarted worker from paying a second provider merely because credential
    routing or model selection changed while the exact grounded target did
    not.
    """

    return uuid.uuid5(
        context.project_id,
        "\x00".join(
            (
                "summary",
                SUMMARY_CANDIDATE_CONTRACT,
                str(level),
                target_id,
                context.binding_fingerprint,
            )
        ),
    )


def normalize_summary_candidate_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the encrypted Summary replay wrapper without schema drift."""

    expected = {
        "contract",
        "summary",
        "detailed",
        "claims",
        "open_questions",
    }
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("summary candidate has an invalid top-level shape")
    if payload.get("contract") != SUMMARY_CANDIDATE_CONTRACT:
        raise ValueError("summary candidate contract mismatch")
    canonical = normalize_extractor_payload(
        {
            "summary": payload.get("summary"),
            "detailed": payload.get("detailed"),
            "claims": payload.get("claims"),
            "open_questions": payload.get("open_questions"),
        }
    )
    return {"contract": SUMMARY_CANDIDATE_CONTRACT, **canonical}


def _summary_candidate(
    canonical: Mapping[str, Any],
    *,
    context: SummaryCandidateContext,
) -> SemanticCandidate:
    payload = normalize_summary_candidate_payload(
        {"contract": SUMMARY_CANDIDATE_CONTRACT, **dict(canonical)}
    )
    return SemanticCandidate(
        contract_name=SUMMARY_CANDIDATE_CONTRACT,
        binding_fingerprint=context.binding_fingerprint,
        payload=payload,
    )


def _summary_replay_request(
    metadata: AttemptStartMetadata,
    *,
    context: SummaryCandidateContext,
) -> SemanticCandidateReplayRequest:
    return SemanticCandidateReplayRequest(
        operation_id=metadata.operation_id,
        attempt_no=metadata.attempt_no,
        input_fingerprint=metadata.input_fingerprint,
        contract_name=SUMMARY_CANDIDATE_CONTRACT,
        binding_fingerprint=context.binding_fingerprint,
        normalizer=normalize_summary_candidate_payload,
    )


def _canonical_from_summary_replay(
    replay: SemanticCandidateReplay | AttemptReplayResult,
) -> dict[str, Any]:
    if (
        isinstance(replay, AttemptReplayResult)
        and replay.state != AttemptReplayState.TERMINAL_WITH_CANDIDATE
    ):
        raise LLMSemanticCandidateUnavailable(
            "summary attempt has no recoverable candidate"
        )
    authoritative = (
        replay.candidate_status in {"candidate", "accepted"}
        and replay.result_status in {ResultStatus.PENDING, ResultStatus.ACCEPTED}
    )
    if (
        replay.attempt_status != AttemptStatus.COMPLETED
        or not authoritative
    ):
        raise LLMSemanticCandidateUnavailable(
            "summary candidate is not a recoverable replay product"
        )
    if replay.payload is None:
        raise LLMSemanticCandidateUnavailable(
            "summary replay candidate payload is unavailable"
        )
    payload = normalize_summary_candidate_payload(replay.payload)
    payload.pop("contract")
    return payload
# Claude Agent SDK/Claude Code exposes no per-request ``max_tokens`` for this
# query path.  The default production model is therefore budgeted at its full
# documented provider output ceiling before dispatch.  This is intentionally
# conservative: the byte cap above bounds retained memory, not billed or
# subscription-side token generation.
AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS = 128_000
AGENT_SDK_BUDGETED_MODEL = "claude-sonnet-4-6"
# The opaque SDK may add subscription-side/system context that Mnemos cannot
# count or cap per request.  A hard input budget therefore reserves the exact
# model context ceiling; smaller prompt estimates would be a false guarantee.
# With the default 120K run cap this route is deliberately disabled.  An
# operator must explicitly authorize at least the full 1M-token ceiling.
AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS = 1_000_000

AGENT_SDK_EMPTY_FAILURE = "agent_sdk_empty_response"
AGENT_SDK_PARSE_FAILURE = "agent_sdk_json_decode"
AGENT_SDK_SCHEMA_FAILURE = "agent_sdk_schema_invalid"
AGENT_SDK_USAGE_FAILURE = "agent_sdk_usage_contract_invalid"
AGENT_SDK_OUTPUT_FAILURE = "agent_sdk_output_limit"
AGENT_SDK_RESULT_FAILURE = "agent_sdk_result_error"
AGENT_SDK_FINISH_FAILURE = "agent_sdk_finish_reason_invalid"
AGENT_SDK_ASSISTANT_FAILURE = "agent_sdk_assistant_contract_invalid"
AGENT_SDK_TRANSPORT_FAILURE = "agent_sdk_error"
AGENT_SDK_TIMEOUT_FAILURE = "agent_sdk_timeout"

AGENT_SDK_TERMINAL_ACCEPTED = "accepted"
AGENT_SDK_TERMINAL_OUTPUT_LIMIT = "output_limit"
AGENT_SDK_TERMINAL_ERROR = "error"
AGENT_SDK_TERMINAL_INVALID = "invalid"


class AgentSDKPreDispatchError(RuntimeError):
    """Local SDK setup failed before the query boundary."""


class AgentSDKTransportError(RuntimeError):
    """The SDK query boundary failed after dispatch was authorized."""


class AgentSDKResultError(RuntimeError):
    """The SDK returned an explicit error result."""


class AgentSDKOutputLimitError(RuntimeError):
    """Streamed response crossed Mnemos's retained-output ceiling."""


class AgentSDKUsageError(RuntimeError):
    """The SDK result carried an invalid accounting dialect."""


@dataclass(frozen=True, slots=True)
class AgentSDKTerminalContract:
    """Privacy-safe terminal classification for one SDK result message."""

    disposition: str
    finish_reason: str | None


@dataclass(slots=True)
class AgentSDKAssistantContract:
    """Collect the resolved model without retaining assistant payload data."""

    expected_model: str | None = None
    resolved_model: str | None = None
    valid: bool = True

    def observe(self, message: Any) -> bool:
        """Validate one real ``AssistantMessage`` metadata envelope.

        The SDK 0.2.118 contract puts ``model`` and ``error`` on every
        assistant message.  A missing/oversized model, an explicit assistant
        error, or a model switch within one query invalidates the response.
        Raw model/error values are intentionally never retained or logged.
        """

        if not self.valid:
            return False
        try:
            assistant_error = getattr(message, "error", None)
        except Exception:  # noqa: BLE001
            assistant_error = "unreadable"
        candidate = _safe_provider_attribute(message, "model", maximum=255)
        message_valid = assistant_error is None and candidate is not None
        if not message_valid:
            # Preserve a single bounded actual model for diagnostics even when
            # the assistant reports an error. Missing or conflicting models
            # remain unknown and can never be presented as resolved.
            self.resolved_model = candidate
            self.valid = False
            return False
        if self.resolved_model is not None and candidate != self.resolved_model:
            self.resolved_model = None
            self.valid = False
            return False
        if self.expected_model is not None and candidate != self.expected_model:
            # The physical ceiling was reserved for the requested model.  A
            # provider-side alias/routing change is therefore not merely
            # descriptive metadata: accepting it would make that reservation
            # contract unprovable.  Preserve only the bounded actual model for
            # the attempt ledger and reject the content.
            self.resolved_model = candidate
            self.valid = False
            return False
        if self.resolved_model is None:
            self.resolved_model = candidate
        return True


@dataclass(slots=True)
class AgentSDKAuthContract:
    """Observe Agent SDK init auth provenance without retaining its value."""

    _saw_api: bool = False
    _saw_unattested: bool = False
    _saw_invalid: bool = False

    def observe(self, message: Any) -> None:
        """Inspect only the ``SystemMessage(init)`` auth discriminator.

        Agent SDK 0.2.118 exposes ``data.apiKeySource``.  The exact value
        A bounded non-empty source identifies an API/cloud credential source.
        The value ``none`` does *not* positively attest OAuth/subscription:
        cloud, auth-token, and other precedence paths can also omit an API-key
        source. Values are reduced to booleans immediately and never stored or
        logged. Missing, invalid, unattested, or mixed observations remain
        ``unknown`` and fail closed.
        """

        try:
            subtype = getattr(message, "subtype", None)
        except Exception:  # noqa: BLE001
            return
        if subtype != "init":
            return
        try:
            data = getattr(message, "data", None)
            source = data.get("apiKeySource") if isinstance(data, Mapping) else None
        except Exception:  # noqa: BLE001
            source = None
        if source == "none":
            self._saw_unattested = True
        elif (
            isinstance(source, str)
            and bool(source.strip())
            and len(source) <= 128
        ):
            self._saw_api = True
        else:
            self._saw_invalid = True

    @property
    def provider_mode(self) -> str:
        if self._saw_invalid or self._saw_unattested:
            return "unknown"
        if self._saw_api:
            return "api"
        return "unknown"


@dataclass(slots=True)
class AgentSDKAttemptState:
    """Safe adapter metadata returned beside the existing dict API.

    The public adapter historically returned ``dict | None``.  Keeping that
    shape avoids forcing standalone/live callers to know about persistence,
    while the production ``Extractor`` can still attach the canonical
    attempt identity and known usage to its result or fallback.  Prompt and
    provider output are deliberately absent.
    """

    attempt_id: uuid.UUID | None = None
    usage: LLMUsageV1 | None = None
    total_tokens: int | None = None
    failure_code: str | None = None
    resolved_model: str | None = None


def _discard_agent_sdk_stderr(_line: str) -> None:
    """Drain untrusted Claude CLI diagnostics without emitting raw data."""


def agent_sdk_isolation_options() -> dict[str, Any]:
    """Return a fresh SDK 0.2.118 isolation boundary for one-shot analysis.

    ``allowed_tools=[]`` controls auto-approval; it does not disable the
    Agent SDK's default tools.  Explicitly empty every extension/configuration
    source so user/project settings, skills, MCP servers, subagents, and
    plugins cannot widen a source-analysis request behind Mnemos's budget and
    grounding boundaries.
    """

    return {
        "tools": [],
        "allowed_tools": [],
        "mcp_servers": {},
        "strict_mcp_config": True,
        "agents": {},
        "setting_sources": [],
        "skills": [],
        "plugins": [],
        "stderr": _discard_agent_sdk_stderr,
        "extra_args": {"no-session-persistence": None},
        # The SDK child inherits the server environment. Force every
        # content-bearing OTel opt-in off so ambient deployment flags cannot
        # export prompts, tool payloads, or raw request/response bodies.
        "env": {
            "OTEL_LOG_USER_PROMPTS": "0",
            "OTEL_LOG_ASSISTANT_RESPONSES": "0",
            "OTEL_LOG_TOOL_DETAILS": "0",
            "OTEL_LOG_TOOL_CONTENT": "0",
            "OTEL_LOG_RAW_API_BODIES": "0",
            "DEBUG_CLAUDE_AGENT_SDK": "0",
            "DEBUG": "0",
            "DEBUG_SDK": "0",
            "CLAUDE_DEBUG": "0",
            "ANTHROPIC_LOG": "off",
            "CLAUDE_CODE_TEE_SDK_STDOUT": "0",
        },
    }


def inspect_agent_sdk_result(message: Any | None) -> AgentSDKTerminalContract:
    """Fail closed on the SDK's authoritative terminal result envelope."""

    if message is None:
        return AgentSDKTerminalContract(AGENT_SDK_TERMINAL_INVALID, None)
    try:
        is_error = getattr(message, "is_error", None)
    except Exception:  # noqa: BLE001
        is_error = None
    finish_reason = _safe_provider_attribute(
        message, "stop_reason", maximum=64
    )
    subtype = _safe_provider_attribute(message, "subtype", maximum=64)
    if not isinstance(is_error, bool) or is_error:
        disposition = AGENT_SDK_TERMINAL_ERROR
    elif subtype != "success":
        disposition = AGENT_SDK_TERMINAL_INVALID
    elif finish_reason == "end_turn":
        disposition = AGENT_SDK_TERMINAL_ACCEPTED
    elif finish_reason == "max_tokens":
        disposition = AGENT_SDK_TERMINAL_OUTPUT_LIMIT
    else:
        disposition = AGENT_SDK_TERMINAL_INVALID
    return AgentSDKTerminalContract(disposition, finish_reason)


def is_agent_sdk_available() -> bool:
    """True iff the Claude Agent SDK can be imported AND the
    operator hasn't opted out."""
    if os.environ.get("MNEMOS_DISABLE_AGENT_SDK") == "1":
        return False
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False


async def summarize_via_agent_sdk(
    *,
    model: str,
    level: int,
    target_id: str,
    evidence: list[dict[str, Any]],
    system_prompt: str | None = None,
    max_evidence_chars: int = 16000,
    timeout_s: int = 60,
    on_provider_start: Callable[[], Awaitable[float]] | None = None,
    on_provider_dispatch: Callable[[], None] | None = None,
    attempt_state: AgentSDKAttemptState | None = None,
) -> dict[str, Any] | None:
    """One-shot Claude call via the local Claude Code credential context.

    Returns the parsed JSON response dict, or None when the response is not
    parseable JSON.  Missing/racing SDK setup is a typed pre-dispatch error.

    A timeout is raised to the owning Extractor so it remains distinguishable
    from malformed JSON.  Durable accounting-hook failures also propagate and
    authorize no provider call.
    """
    if not is_agent_sdk_available():
        raise AgentSDKPreDispatchError("agent_sdk_unavailable")
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        raise AgentSDKPreDispatchError("agent_sdk_import_race") from None

    try:
        prompt = _build_prompt(level, target_id, evidence, max_evidence_chars)
    except (EvidencePromptTooLarge, ValueError):
        log.warning("agent_sdk: evidence exceeds complete-JSON prompt budget")
        raise AgentSDKPreDispatchError("agent_sdk_prompt_rejected") from None
    callbacks = require_attempt_callbacks("Claude Agent SDK summary")
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
    if model != AGENT_SDK_BUDGETED_MODEL:
        # Claude Code exposes no request-level max_tokens knob.  Only the
        # production model whose documented 128K ceiling is reserved may use
        # the durable summary path; otherwise the hard output budget would be
        # based on an invented cap.
        raise AgentSDKPreDispatchError("agent_sdk_model_unsupported")
    sys_p = system_prompt or _DEFAULT_SYSTEM
    try:
        opts = ClaudeAgentOptions(
            **agent_sdk_isolation_options(),
            disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
            system_prompt=sys_p,
            max_turns=1,
            permission_mode="dontAsk",
            cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
            model=model if model and "claude" in model else None,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_sdk: options initialization failed: %s", type(exc).__name__)
        raise AgentSDKPreDispatchError("agent_sdk_options_error") from None

    collected_text: list[str] = []
    collected_bytes = 0
    ticket: AttemptTicket | None = None
    assistant_contract = AgentSDKAssistantContract(expected_model=model)
    auth_contract = AgentSDKAuthContract()

    # All local validation/options work is complete.  The v1 start callback
    # commits both the durable scope reservation and STARTED row before the
    # query boundary.  ``operation_id`` is intentionally provisional: the
    # runner replaces it with its stable project/run/target identity.
    metadata = AttemptStartMetadata(
        operation_id=uuid.uuid4(),
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="unknown",
        requested_model=model,
        input_fingerprint=fingerprint_input(sys_p, prompt),
        estimated_input_tokens=AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS,
        input_estimate_method="model_context_ceiling_v1",
        requested_max_output_tokens=AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
        usage_source=UsageSource.AGENT_SDK,
        billing_route="claude_agent_sdk",
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
                raise LLMSemanticCandidateUnavailable(
                    "summary duplicate attempt disappeared"
                )
            if replay.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
                if attempt_state is not None:
                    attempt_state.attempt_id = replay.attempt_id
                    attempt_state.total_tokens = replay.total_tokens
                    attempt_state.resolved_model = replay.resolved_model
                    attempt_state.failure_code = replay.failure_code
                    attempt_state.usage = replay.usage
                return None
        else:
            replayer = callbacks.replay_candidate
            if replayer is None:  # pragma: no cover - guarded above
                raise LLMLifecycleError("summary semantic replayer disappeared")
            replay = await replayer(replay_request)
        if replay.candidate_status == "rejected":
            if attempt_state is not None:
                attempt_state.attempt_id = replay.attempt_id
                attempt_state.total_tokens = replay.total_tokens
                attempt_state.resolved_model = replay.resolved_model
                attempt_state.failure_code = replay.failure_code
                attempt_state.usage = getattr(replay, "usage", None)
            return None
        canonical = _canonical_from_summary_replay(replay)
        if attempt_state is not None:
            attempt_state.attempt_id = replay.attempt_id
            attempt_state.total_tokens = replay.total_tokens
            attempt_state.resolved_model = replay.resolved_model
        return canonical
    except (LLMLifecycleError, RunBudgetExceeded, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise LLMLifecycleError(
            "agent SDK attempt start failed before dispatch"
        ) from exc
    if attempt_state is not None:
        attempt_state.attempt_id = ticket.attempt_id
    provider_timeout = min(float(timeout_s), ticket.remaining_seconds)

    async def _finish(
        *,
        attempt_status: AttemptStatus,
        result_status: ResultStatus,
        usage: LLMUsageV1,
        failure_code: str | None = None,
        resolved_model: str | None = None,
        provider_request_id: str | None = None,
        finish_reason: str | None = None,
        candidate: SemanticCandidate | None = None,
    ) -> None:
        if attempt_state is not None:
            attempt_state.usage = usage
            attempt_state.total_tokens = usage.total_tokens
            attempt_state.failure_code = failure_code
            attempt_state.resolved_model = resolved_model
        if ticket is None:
            return
        try:
            outcome = AttemptOutcome(
                attempt_status=attempt_status,
                result_status=result_status,
                usage=usage,
                resolved_model=resolved_model,
                provider_request_id=provider_request_id,
                failure_code=failure_code,
                finish_reason=finish_reason,
                provider_mode=auth_contract.provider_mode,
            )
            if candidate is not None:
                if callbacks.finish_candidate is None:
                    raise LLMLifecycleError(
                        "summary semantic candidate finalizer is unavailable"
                    )
                await callbacks.finish_candidate(ticket, outcome, candidate)
            else:
                await callbacks.finish(ticket, outcome)
        except (LLMLifecycleError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001
            raise LLMLifecycleError(
                "agent SDK attempt finalization failed"
            ) from exc

    if provider_timeout <= 0:
        await _finish(
            attempt_status=AttemptStatus.TIMEOUT,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=False,
                source=UsageSource.AGENT_SDK,
            ),
            failure_code=AGENT_SDK_TIMEOUT_FAILURE,
        )
        raise TimeoutError("agent_sdk_timeout")

    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    # Optional observer only. It runs after the durable STARTED capability and
    # cannot substitute for it (legacy adapter tests/telemetry use this hook).
    if on_provider_dispatch is not None:
        on_provider_dispatch()
    try:
        async def _drain():
            nonlocal collected_bytes
            async for msg in query(prompt=prompt, options=opts):
                auth_contract.observe(msg)
                if isinstance(msg, AssistantMessage):
                    if not assistant_contract.observe(msg):
                        continue
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected_bytes += len(block.text.encode("utf-8"))
                            if collected_bytes > MAX_AGENT_SUMMARY_OUTPUT_BYTES:
                                raise AgentSDKOutputLimitError(
                                    "agent_sdk_output_too_large"
                                )
                            collected_text.append(block.text)
                elif isinstance(msg, ResultMessage):
                    return msg
            return None

        result_message = await asyncio.wait_for(
            _drain(), timeout=provider_timeout
        )
    except asyncio.TimeoutError:
        await _finish(
            attempt_status=AttemptStatus.TIMEOUT,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
            failure_code=AGENT_SDK_TIMEOUT_FAILURE,
        )
        log.warning("agent_sdk: timed out after %.3fs", provider_timeout)
        raise TimeoutError("agent_sdk_timeout") from None
    except AgentSDKOutputLimitError:
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
            failure_code=AGENT_SDK_OUTPUT_FAILURE,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
            failure_code=AGENT_SDK_TRANSPORT_FAILURE,
        )
        log.warning("agent_sdk: %s", type(exc).__name__)
        raise AgentSDKTransportError("agent_sdk_transport_error") from None

    if result_message is None:
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=unavailable_usage(
                lost_after_dispatch=True,
                source=UsageSource.AGENT_SDK,
            ),
            failure_code=AGENT_SDK_RESULT_FAILURE,
        )
        raise AgentSDKResultError("agent_sdk_missing_result")

    resolved_model = assistant_contract.resolved_model
    # ResultMessage.session_id is an Agent/Claude Code conversation identity,
    # not a provider request/message ID. Keep provider_request_id unknown
    # rather than mislabeling a many-request session.
    provider_request_id = None
    terminal = inspect_agent_sdk_result(result_message)
    finish_reason = terminal.finish_reason

    try:
        usage = normalize_agent_sdk_usage(result_message)
    except Exception as exc:  # noqa: BLE001
        usage = unavailable_usage(source=UsageSource.AGENT_SDK)
        await _finish(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            failure_code=AGENT_SDK_USAGE_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        log.warning("agent_sdk: usage rejected: %s", type(exc).__name__)
        raise AgentSDKUsageError(AGENT_SDK_USAGE_FAILURE) from None

    if terminal.disposition == AGENT_SDK_TERMINAL_ERROR:
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            failure_code=AGENT_SDK_RESULT_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        raise AgentSDKResultError("agent_sdk_result_error")
    if terminal.disposition == AGENT_SDK_TERMINAL_OUTPUT_LIMIT:
        await _finish(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.OUTPUT_LIMIT_REJECTED,
            usage=usage,
            failure_code=AGENT_SDK_OUTPUT_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        raise AgentSDKOutputLimitError("agent_sdk_provider_output_limit")
    if terminal.disposition != AGENT_SDK_TERMINAL_ACCEPTED:
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            failure_code=AGENT_SDK_FINISH_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        raise AgentSDKResultError("agent_sdk_finish_reason_invalid")
    if not assistant_contract.valid:
        await _finish(
            attempt_status=AttemptStatus.FAILED,
            result_status=ResultStatus.NOT_APPLICABLE,
            usage=usage,
            failure_code=AGENT_SDK_ASSISTANT_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        raise AgentSDKResultError("agent_sdk_assistant_contract_invalid")
    if not collected_text:
        await _finish(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.EMPTY,
            usage=usage,
            failure_code=AGENT_SDK_EMPTY_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        return None

    text = "\n".join(collected_text)
    try:
        parsed = parse_json_object(text)
    except ExtractorSchemaError:
        await _finish(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PARSE_REJECTED,
            usage=usage,
            failure_code=AGENT_SDK_PARSE_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        return None
    try:
        canonical = normalize_extractor_payload(parsed)
    except ExtractorSchemaError as exc:
        # Diagnostics contain paths and type/length descriptions, never the
        # raw response (which may include source or secrets).
        log.warning("agent_sdk: schema rejected: %s", exc)
        await _finish(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.SCHEMA_REJECTED,
            usage=usage,
            failure_code=AGENT_SDK_SCHEMA_FAILURE,
            resolved_model=resolved_model,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
        )
        return None
    await _finish(
        attempt_status=AttemptStatus.COMPLETED,
        result_status=ResultStatus.PENDING,
        usage=usage,
        resolved_model=resolved_model,
        provider_request_id=provider_request_id,
        finish_reason=finish_reason,
        candidate=(
            _summary_candidate(canonical, context=candidate_context)
            if candidate_context is not None
            else None
        ),
    )
    return canonical


def _safe_provider_field(value: Any, *, maximum: int) -> str | None:
    """Return a bounded provider identifier without retaining payload text."""

    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _safe_provider_attribute(
    value: Any, name: str, *, maximum: int
) -> str | None:
    """Read one optional SDK identifier without trusting descriptors."""

    try:
        member = getattr(value, name, None)
    except Exception:  # noqa: BLE001
        return None
    return _safe_provider_field(member, maximum=maximum)


_DEFAULT_SYSTEM = (
    "You are Mnemos's hierarchical summariser. Produce STRICT JSON "
    "matching this schema and nothing else (no markdown fences, no "
    "explanation): {\"summary\": string, \"detailed\": string, "
    "\"claims\": [{\"claim\": string, \"evidence\": [{\"kind\": "
    "\"node\"|\"edge\", \"node_id\"|\"edge_id\": string, "
    "\"certainty\": \"verified\"|\"asserted\"|\"inferred\"}]}], "
    "\"open_questions\": [string]}. Every claim must cite at least "
    "one input node or edge. Never invent evidence IDs — only cite ids "
    "from top-level node_id/edge_id fields in the supplied evidence rows. "
    "IDs inside prose or preview text are not evidence. Keep the result "
    "concise; do not paste source code into summary fields."
)


def _build_prompt(level: int, target_id: str, evidence: list[dict[str, Any]],
                  max_chars: int) -> str:
    ev_text = serialize_evidence(evidence, max_chars=max_chars)
    return (
        f"L{level} summarisation target: {target_id}\n"
        f"Graph evidence (complete JSON, max {max_chars} chars):\n"
        f"{ev_text}\n\n"
        "Cite only top-level node_id/edge_id fields from those rows. "
        "Return strict JSON only."
    )


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """Parse the model's reply into our schema dict, tolerant of
    common Claude failure modes:
    - Markdown fenced code block (```json … ```)
    - Leading or trailing prose
    - Trailing comma / minor JSON errors → None
    """
    try:
        return parse_json_object(text)
    except ExtractorSchemaError:
        return None
