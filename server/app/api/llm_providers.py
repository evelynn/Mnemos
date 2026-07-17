"""PR-178/179 — multi-provider LLM backend for the Chat tab.

The operator picks which AI answers a chat message: OpenAI, Gemini, Claude,
or Atlas (hansol ai). Ordinary Claude chat requires an Anthropic API key;
the local Agent SDK is not advertised as a key-free provider because it has
neither positive billing provenance nor a per-request output cap. Atlas is
the hansol agent API (key + agent ID, two-step session call). Each provider's
config is resolved per request from two sources, DB first then env:

  1. Platform-provisioned DB config: keys in the encrypted ``Secret`` table
     (label ``chat-provider:<suffix>``), models / agent / base_url / mode in
     the global ``PlatformSetting`` row ``chat_providers``.
  2. Env fallback: OPENAI_API_KEY/OPENAI_MODEL, GEMINI_API_KEY/GEMINI_MODEL,
     ATLAS_API_KEY/ATLAS_AGENT_ID/ATLAS_BASE_URL, ANTHROPIC_API_KEY/MNEMOS_CHAT_MODEL.

``resolve_config(db)`` returns ``{provider: {api_key, model, base_url}}``;
the availability/dispatch helpers take that config (they never read env or
DB themselves, which keeps them pure and testable). Calls go over HTTP
(``httpx``), except for the direct ``anthropic`` client.  Chat deliberately
does not fall back to the Claude Agent SDK: that route has neither a
provider-enforced output cap nor independently attested billing provenance.
``provider_chat`` returns the markdown reply or ``None`` (never raises).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from time import monotonic
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageContractError,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    normalize_anthropic_usage,
    normalize_gemini_usage,
    normalize_openai_chat_usage,
    unavailable_usage,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptReplayState,
    AttemptStartMetadata,
    AttemptTicket,
    CandidateNormalizer,
    LLMAttemptReplayBlocked,
    LLMLifecycleError,
    LLMSemanticCandidateCorrupt,
    PRICE_ATTESTATION_MODEL_MISMATCH,
    SemanticCandidate,
    SemanticCandidateReplayRequest,
    fingerprint_input,
    require_attempt_callbacks,
)
from app.llm.pricing import (
    OFFICIAL_ANTHROPIC_API_BASE_URL,
    OFFICIAL_GEMINI_GENERATE_API_BASE_URL,
    OFFICIAL_OPENAI_CHAT_API_BASE_URL,
    is_price_attested_openai_chat_base_url,
)
from app.models.auth import PlatformSetting, Secret
from app.safety.crypto import decrypt

log = logging.getLogger("mnemos.chat.providers")

_MAX_TOKENS = 3000
_SUBSCRIPTION_CHARS_PER_TOKEN = 8
_HTTP_JSON_OVERHEAD_BYTES = 64 * 1024
_ATLAS_SESSION_RESPONSE_MAX_BYTES = 32 * 1024
ATLAS_GENERATION_DISABLED_REASON = (
    "atlas_generation_disabled_missing_provider_budget_contract"
)


class _ProviderResponseTooLarge(RuntimeError):
    """A provider response exceeded Mnemos's finite client-side byte ceiling."""


class ChatSemanticContractError(ValueError):
    """A provider reply cannot become a bounded Chat semantic candidate."""

    result_status = ResultStatus.SCHEMA_REJECTED


@dataclass(frozen=True, slots=True)
class ChatSemanticCodec:
    """Consumer-owned contract at the provider-to-product boundary.

    The adapter never stores raw provider text. It asks the owning Chat
    surface to parse and normalize that text, atomically commits the resulting
    encrypted candidate with the physical attempt, and can render the same
    canonical candidate after a crash without another provider dispatch.
    """

    contract_name: str
    binding_fingerprint: str
    normalize_response: Callable[[str], Mapping[str, Any]]
    normalizer: CandidateNormalizer
    render_candidate: Callable[[Mapping[str, Any]], str]
    parse_failure_code: str
    schema_failure_code: str

    def __post_init__(self) -> None:
        contract_pattern = re.compile(r"[a-z0-9][a-z0-9._-]{2,95}\Z")
        digest_pattern = re.compile(r"[0-9a-f]{64}\Z")
        failure_pattern = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
        if (
            not isinstance(self.contract_name, str)
            or contract_pattern.fullmatch(self.contract_name) is None
        ):
            raise ChatSemanticContractError(
                "chat semantic codec contract name is invalid"
            )
        if (
            not isinstance(self.binding_fingerprint, str)
            or digest_pattern.fullmatch(self.binding_fingerprint) is None
        ):
            raise ChatSemanticContractError(
                "chat semantic codec binding is invalid"
            )
        if not all(
            callable(callback)
            for callback in (
                self.normalize_response,
                self.normalizer,
                self.render_candidate,
            )
        ):
            raise ChatSemanticContractError(
                "chat semantic codec callbacks are invalid"
            )
        if any(
            not isinstance(code, str)
            or failure_pattern.fullmatch(code) is None
            for code in (
                self.parse_failure_code,
                self.schema_failure_code,
            )
        ):
            raise ChatSemanticContractError(
                "chat semantic codec failure code is invalid"
            )


_CHAT_ANSWER_CONTRACT = "mnemos.chat.answer.v1"


def normalize_chat_answer_candidate(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Return the sole canonical free-form Chat answer representation."""

    if not isinstance(payload, Mapping) or set(payload) != {"contract", "reply"}:
        raise ChatSemanticContractError(
            "chat answer candidate must have the exact contract shape"
        )
    if payload.get("contract") != _CHAT_ANSWER_CONTRACT:
        raise ChatSemanticContractError(
            "chat answer candidate contract does not match"
        )
    reply = payload.get("reply")
    if not isinstance(reply, str):
        raise ChatSemanticContractError("chat answer candidate reply must be text")
    canonical_reply = reply.strip()
    if not canonical_reply:
        raise ChatSemanticContractError("chat answer candidate reply is empty")
    return {
        "contract": _CHAT_ANSWER_CONTRACT,
        "reply": canonical_reply,
    }


def _build_chat_answer_candidate(text: str) -> Mapping[str, Any]:
    return normalize_chat_answer_candidate(
        {"contract": _CHAT_ANSWER_CONTRACT, "reply": text}
    )


def _render_chat_answer_candidate(payload: Mapping[str, Any]) -> str:
    canonical = normalize_chat_answer_candidate(payload)
    return str(canonical["reply"])


def chat_answer_semantic_codec(*binding_parts: str) -> ChatSemanticCodec:
    """Build a stable answer codec bound to consumer-owned input identity."""

    if not binding_parts:
        raise ValueError("chat answer semantic binding requires input identity")
    return ChatSemanticCodec(
        contract_name=_CHAT_ANSWER_CONTRACT,
        binding_fingerprint=fingerprint_input(
            "mnemos.chat.answer.binding.v1",
            *binding_parts,
        ),
        normalize_response=_build_chat_answer_candidate,
        normalizer=normalize_chat_answer_candidate,
        render_candidate=_render_chat_answer_candidate,
        parse_failure_code="chat_answer_parse_rejected",
        schema_failure_code="chat_answer_schema_rejected",
    )


@dataclass(slots=True)
class ChatProviderAttemptState:
    """Safe bridge from one provider candidate to its Chat consumer.

    Provider payload text remains the existing return value.  Only the durable
    attempt identity crosses into ``chat.py`` so rewrite parsing or final
    answer acceptance can classify the already-finalized transport result.
    """

    attempt_id: uuid.UUID | None = None
    consumer_result_status: ResultStatus | None = None
    consumer_failure_code: str | None = None


@dataclass(slots=True)
class _ProviderObservation:
    """Canonical, payload-free metadata for one provider response."""

    usage: LLMUsageV1
    attempt_status: AttemptStatus = AttemptStatus.COMPLETED
    result_status: ResultStatus = ResultStatus.PENDING
    resolved_model: str | None = None
    provider_request_id: str | None = None
    failure_code: str | None = None
    finish_reason: str | None = None


def _safe_provider_string(value: object, *, maximum: int) -> str | None:
    """Return bounded provider metadata without logging or coercing it."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > maximum:
        return None
    return candidate


def _provider_input_reservation(system: str, prompt: str) -> int:
    """Return a conservative provider-independent input reservation.

    Provider tokenizers differ, especially for Korean source questions.  The
    UTF-8 byte length is intentionally an upper-bound-style reservation rather
    than a claimed billed-token count.  Actual provider usage, when available,
    belongs in the physical-call ledger and never overwrites this estimate.
    """

    return max(
        1,
        len(system.encode("utf-8"))
        + len(prompt.encode("utf-8"))
        + 256,
    )


def _bound_provider_operation_id(
    namespace: uuid.UUID,
    *,
    provider: str,
    model: str,
    route_fingerprint: str,
    requested_max_output_tokens: int,
    system: str,
    prompt: str,
) -> uuid.UUID:
    """Bind a stable consumer namespace to the complete provider request.

    ``route_fingerprint`` is a one-way digest of non-secret endpoint/agent
    configuration.  API keys are intentionally excluded so ordinary key
    rotation does not invalidate an otherwise identical logical route.
    """

    return uuid.uuid5(
        namespace,
        "\x00".join(
            (
                provider,
                model,
                route_fingerprint,
                str(requested_max_output_tokens),
                fingerprint_input(system, prompt),
            )
        ),
    )


_PROVIDER_DISPATCH_NONCE = object()


@dataclass(frozen=True, slots=True)
class _ProviderDispatchCapability:
    """Module-private guard against accidental leaf calls before STARTED.

    This is not a security boundary against intentionally malicious code in
    the same Python process; such code can already monkeypatch/import private
    internals. Public routes cannot supply callbacks or this capability.
    """

    attempt_id: uuid.UUID
    provider: str
    input_fingerprint: str
    requested_max_output_tokens: int
    deadline_monotonic: float
    _nonce: object


def _issue_provider_dispatch_capability(
    ticket: AttemptTicket,
    *,
    provider: str,
    system: str,
    prompt: str,
    timeout_s: float,
    requested_max_output_tokens: int,
) -> _ProviderDispatchCapability:
    """Mint the leaf-network capability only from a durable start ticket."""

    if (
        not isinstance(ticket, AttemptTicket)
        or not isinstance(ticket.attempt_id, uuid.UUID)
        or provider not in {"openai", "gemini", "anthropic", "atlas"}
        or timeout_s <= 0
        or requested_max_output_tokens <= 0
    ):
        raise LLMLifecycleError("provider dispatch capability is invalid")
    return _ProviderDispatchCapability(
        attempt_id=ticket.attempt_id,
        provider=provider,
        input_fingerprint=fingerprint_input(system, prompt),
        requested_max_output_tokens=requested_max_output_tokens,
        deadline_monotonic=monotonic() + timeout_s,
        _nonce=_PROVIDER_DISPATCH_NONCE,
    )


def _authorized_provider_timeout(
    capability: _ProviderDispatchCapability,
    *,
    provider: str,
    system: str,
    prompt: str,
    timeout_s: float,
    requested_max_output_tokens: int,
) -> float:
    """Validate exact authorized request facts at the physical leaf."""

    if not isinstance(capability, _ProviderDispatchCapability):
        raise LLMLifecycleError(
            "provider network dispatch lacks a STARTED capability"
        )
    remaining = capability.deadline_monotonic - monotonic()
    if (
        capability._nonce is not _PROVIDER_DISPATCH_NONCE
        or capability.provider != provider
        or capability.input_fingerprint != fingerprint_input(system, prompt)
        or capability.requested_max_output_tokens
        != requested_max_output_tokens
    ):
        raise LLMLifecycleError(
            "provider network dispatch lacks a valid STARTED capability"
        )
    if remaining <= 0:
        raise TimeoutError("provider dispatch capability deadline expired")
    return min(float(timeout_s), remaining)


def _provider_response_max_bytes(max_output_tokens: int) -> int:
    """Bound a UTF-8 JSON envelope around a finite text-token response.

    Eight characters per requested token is deliberately conservative for
    visible text.  UTF-8 can use four bytes per character; the fixed allowance
    covers ordinary provider metadata without making the HTTP body unbounded.
    """

    return _HTTP_JSON_OVERHEAD_BYTES + (
        max_output_tokens * _SUBSCRIPTION_CHARS_PER_TOKEN * 4
    )


async def _post_json_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    timeout_s: float,
    headers: dict[str, str],
    payload: dict,
) -> dict:
    """POST JSON and parse one bounded JSON-object response.

    ``httpx.post`` buffers the entire body before returning.  Streaming here
    makes the byte ceiling a memory boundary rather than a check performed
    after an arbitrarily large provider response has already been allocated.
    HTTPX read timeouts reset after each chunk, so an outer asyncio deadline
    also bounds a slow-drip response's total wall time.
    """

    async with asyncio.timeout(timeout_s):
        async with client.stream(
            "POST",
            url,
            headers=headers,
            json=payload,
            timeout=timeout_s,
        ) as response:
            response.raise_for_status()
            raw_length = response.headers.get("content-length")
            if raw_length:
                try:
                    if int(raw_length) > max_bytes:
                        raise _ProviderResponseTooLarge(
                            f"provider response exceeded {max_bytes} bytes"
                        )
                except ValueError:
                    # Invalid transport metadata is ignored; the streamed byte
                    # counter below remains authoritative.
                    pass

            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise _ProviderResponseTooLarge(
                        f"provider response exceeded {max_bytes} bytes"
                    )
                body.extend(chunk)

    decoded = json.loads(body)
    if not isinstance(decoded, dict):
        raise ValueError("provider response JSON must be an object")
    return decoded


def _openai_output_limit_field(*, base_url: str, model: str) -> str:
    """Choose the current OpenAI field without breaking legacy proxies.

    OpenAI's official Chat API deprecates ``max_tokens`` and rejects it for
    o-series models.  Custom OpenAI-compatible endpoints often still expose
    only the legacy field, so retain it unless the model itself requires the
    current contract.
    """

    model_name = model.casefold().rsplit("/", 1)[-1]
    if is_price_attested_openai_chat_base_url(base_url) or model_name.startswith(
        ("o1", "o3", "o4", "gpt-5")
    ):
        return "max_completion_tokens"
    return "max_tokens"


# Platform-owned provider configuration namespaces.
SETTING_KEY = "chat_providers"  # PlatformSetting row (models/base_url)
SECRET_PREFIX = "chat-provider:"  # Secret label prefix (API keys)
SECRET_KIND = "llm_api_key"

PROVIDER_ORDER = ["claudecode", "openai", "gemini", "atlas"]
PROVIDER_LABELS = {
    "claudecode": "Claude (Anthropic API 키 필수)",
    "openai": "OpenAI",
    "gemini": "Gemini",
    "atlas": "Atlas (hansol ai)",
}
# provider id → the Secret label suffix that holds its API key.
_KEY_SUFFIX = {
    "openai": "openai",
    "gemini": "gemini",
    "atlas": "atlas",
    "claudecode": "anthropic",
}
_DEFAULT_MODEL = {
    "openai": "gpt-4o-2024-11-20",
    "gemini": "gemini-2.5-flash",
    "atlas": "",
    "claudecode": "claude-sonnet-4-6",
}
# Current models offered in the Settings dropdowns (Context7-sourced, 2026).
# The free-text field and the live "test" model fetch both override these,
# so a stale entry here never blocks a newer model.
SUGGESTED_MODELS = {
    "openai": ["gpt-4o-2024-11-20"],
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash", "gemini-2.0-flash"],
    "claudecode": ["claude-sonnet-4-6", "claude-opus-4-8", "claude-haiku-4-5-20251001"],
    "atlas": [],
}


def secret_label(provider: str) -> str:
    """The ``Secret.label`` that stores ``provider``'s API key."""
    return SECRET_PREFIX + _KEY_SUFFIX[provider]


def _env(key: str) -> str | None:
    v = os.environ.get(key)
    return v.strip() if v and v.strip() else None


# ── Config resolution (DB over env) ───────────────────────────────────
async def resolve_config(db: AsyncSession) -> dict[str, dict]:
    """Per-provider config — platform-owned DB values win over environment."""
    cfg: dict[str, dict] = {
        pid: {"api_key": None, "model": None, "base_url": None} for pid in PROVIDER_ORDER
    }
    cfg["openai"].update(
        api_key=_env("OPENAI_API_KEY"),
        model=_env("OPENAI_MODEL"),
        base_url=OFFICIAL_OPENAI_CHAT_API_BASE_URL,
    )
    cfg["gemini"].update(
        api_key=_env("GEMINI_API_KEY"),
        model=_env("GEMINI_MODEL"),
    )
    cfg["atlas"].update(
        api_key=_env("ATLAS_API_KEY"),
        agent_id=_env("ATLAS_AGENT_ID"),
        base_url=_env("ATLAS_BASE_URL") or "https://ai-atlas.hansol.net/api/v1/public",
    )
    cfg["claudecode"].update(
        api_key=_env("ANTHROPIC_API_KEY"),
        model=_env("MNEMOS_CHAT_MODEL"),
    )

    # PlatformSetting: model + base_url overrides.
    ps = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == SETTING_KEY))
    ).scalar_one_or_none()
    saved = (ps.value if ps else None) or {}
    for pid in PROVIDER_ORDER:
        s = saved.get(pid) or {}
        if s.get("model"):
            cfg[pid]["model"] = s["model"]
        if pid == "atlas" and s.get("base_url"):
            cfg[pid]["base_url"] = s["base_url"]
        if pid == "atlas" and s.get("agent_id"):
            cfg[pid]["agent_id"] = s["agent_id"]
    # ``subscription`` is retained only as a legacy stored value. Chat never
    # dispatches it; ``api`` is the sole supported Claude Chat route.
    cfg["claudecode"]["mode"] = (
        (saved.get("claudecode") or {}).get("mode") or "api"
    )

    # Secret: API-key overrides (encrypted at rest).
    by_suffix = {v: k for k, v in _KEY_SUFFIX.items()}
    secrets = (
        (
            await db.execute(
                select(Secret)
                .where(
                    Secret.label.like(SECRET_PREFIX + "%"),
                    Secret.organization_id.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    by_label: dict[str, list[Secret]] = {}
    for sec in secrets:
        by_label.setdefault(sec.label, []).append(sec)
    for suffix, pid in by_suffix.items():
        label = SECRET_PREFIX + suffix
        matches = by_label.get(label, [])
        if len(matches) != 1:
            if len(matches) > 1:
                log.error("duplicate platform provider key label %s; using env", label)
            continue
        sec = matches[0]
        try:
            key = decrypt(sec.ciphertext, sec.iv)
        except Exception as exc:  # noqa: BLE001
            log.warning("provider key decrypt failed (%s): %s", sec.label, exc.__class__.__name__)
            continue
        if key and key.strip():
            cfg[pid]["api_key"] = key.strip()

    for pid in PROVIDER_ORDER:
        if not cfg[pid]["model"]:
            cfg[pid]["model"] = _DEFAULT_MODEL.get(pid) or None
    return cfg


# ── Availability + selection (operate on a resolved config) ───────────
def is_provider_available(provider: str, cfg: dict[str, dict]) -> bool:
    c = cfg.get(provider) or {}
    if provider == "claudecode":
        # Importability is not an auth/cost attestation. Claude Chat is the
        # bounded direct Messages API only.
        return bool(c.get("api_key")) and (c.get("mode") or "api") == "api"
    if provider == "atlas":
        # The current public-agent contract exposes no provider-enforced
        # output-token cap, immutable price, usage, resolved model, or request
        # receipt. A client byte ceiling cannot bound work already generated
        # provider-side, so generation remains fail-disabled.
        return False
    if provider in ("openai", "gemini"):
        return bool(c.get("api_key"))
    return False


def output_token_limit_capability(provider: str, cfg: dict[str, dict]) -> dict:
    """Describe whether ``max_output_tokens`` survives the selected path.

    OpenAI, Gemini, and the direct Anthropic API expose a request field. Atlas
    does not, so its logical two-POST route has bounded client-side response
    bytes/characters but no claimed provider generation cap.
    """

    if provider == "openai":
        c = cfg.get(provider) or {}
        field = _openai_output_limit_field(
            base_url=c.get("base_url") or OFFICIAL_OPENAI_CHAT_API_BASE_URL,
            model=c.get("model") or "gpt-4o-2024-11-20",
        )
        return {
            "output_token_limit_enforcement": "provider",
            "output_token_limit_detail": f"openai_{field}",
        }
    if provider == "gemini":
        return {
            "output_token_limit_enforcement": "provider",
            "output_token_limit_detail": f"{provider}_request_field",
        }
    if provider == "atlas":
        return {
            "output_token_limit_enforcement": "unsupported",
            "output_token_limit_detail": ATLAS_GENERATION_DISABLED_REASON,
        }
    if provider == "claudecode":
        c = cfg.get(provider) or {}
        if c.get("api_key") and (c.get("mode") or "api") == "api":
            return {
                "output_token_limit_enforcement": "provider",
                "output_token_limit_detail": "anthropic_messages_max_tokens",
            }
        return {
            "output_token_limit_enforcement": "unsupported",
            "output_token_limit_detail": "anthropic_api_mode_and_key_required",
        }
    return {
        "output_token_limit_enforcement": "unsupported",
        "output_token_limit_detail": "unknown_provider",
    }


def available_providers(cfg: dict[str, dict]) -> list[dict]:
    return [
        {
            "id": pid,
            "label": PROVIDER_LABELS[pid],
            "available": is_provider_available(pid, cfg),
            **output_token_limit_capability(pid, cfg),
        }
        for pid in PROVIDER_ORDER
    ]


def any_provider_available(cfg: dict[str, dict]) -> bool:
    return any(is_provider_available(pid, cfg) for pid in PROVIDER_ORDER)


def default_provider(cfg: dict[str, dict]) -> str:
    for pid in PROVIDER_ORDER:
        if is_provider_available(pid, cfg):
            return pid
    return "claudecode"


# ── HTTP calls ────────────────────────────────────────────────────────
async def _openai_compatible(
    *,
    base_url: str,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    timeout_s: float,
    max_output_tokens: int,
    _dispatch: _ProviderDispatchCapability,
    _observation: _ProviderObservation | None = None,
) -> str | None:
    """A /chat/completions call in the OpenAI wire format — serves both
    OpenAI proper and any OpenAI-compatible endpoint (Atlas)."""
    url = base_url.rstrip("/") + "/chat/completions"
    output_limit_field = _openai_output_limit_field(base_url=base_url, model=model)
    attempt_timeout = _authorized_provider_timeout(
        _dispatch,
        provider="openai",
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=max_output_tokens,
    )
    async with httpx.AsyncClient(timeout=attempt_timeout) as client:
        response = await _post_json_bounded(
            client,
            url,
            max_bytes=_provider_response_max_bytes(max_output_tokens),
            timeout_s=attempt_timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            payload={
                "model": model,
                output_limit_field: max_output_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        try:
            resolved_model = _safe_provider_string(
                response.get("model"), maximum=255
            )
            if _observation is not None:
                _observation.resolved_model = resolved_model
                _observation.provider_request_id = _safe_provider_string(
                    response.get("id"), maximum=255
                )
            if (
                _observation is not None
                and
                is_price_attested_openai_chat_base_url(base_url)
                and resolved_model != model
            ):
                if _observation is not None:
                    _observation.result_status = ResultStatus.NOT_APPLICABLE
                    _observation.failure_code = PRICE_ATTESTATION_MODEL_MISMATCH
                return None
            usage = normalize_openai_chat_usage(response)
        except LLMUsageContractError:
            if _observation is not None:
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_usage_contract_invalid"
            return None
        if _observation is not None:
            _observation.usage = usage
        choices = response.get("choices") or []
        if not choices:
            if _observation is not None:
                _observation.result_status = ResultStatus.EMPTY
                _observation.failure_code = "chat_empty_response"
            return None
        choice = choices[0]
        if not isinstance(choice, dict):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        finish_reason = _safe_provider_string(
            choice.get("finish_reason"), maximum=64
        )
        if _observation is not None:
            _observation.finish_reason = finish_reason
        if finish_reason == "length":
            if _observation is not None:
                _observation.result_status = ResultStatus.OUTPUT_LIMIT_REJECTED
                _observation.failure_code = "chat_output_limit"
            log.warning("chat: OpenAI-compatible provider hit its output limit")
            return None
        if finish_reason != "stop":
            if _observation is not None:
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_finish_reason_invalid"
            log.warning("chat: OpenAI-compatible provider rejected terminal result")
            return None
        message = choice.get("message")
        if not isinstance(message, dict):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        content = message.get("content")
        if not isinstance(content, str):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        text = content.strip()
        if not text:
            if _observation is not None:
                _observation.result_status = ResultStatus.EMPTY
                _observation.failure_code = "chat_empty_response"
            return None
        return text


async def _gemini_generate(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    timeout_s: float,
    max_output_tokens: int,
    _dispatch: _ProviderDispatchCapability,
    _observation: _ProviderObservation | None = None,
) -> str | None:
    # Auth via the x-goog-api-key header (current docs) — never the ?key=
    # query param, so the key never lands in a URL/log.
    url = (
        f"{OFFICIAL_GEMINI_GENERATE_API_BASE_URL}/models/"
        f"{model}:generateContent"
    )
    attempt_timeout = _authorized_provider_timeout(
        _dispatch,
        provider="gemini",
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=max_output_tokens,
    )
    async with httpx.AsyncClient(timeout=attempt_timeout) as client:
        response = await _post_json_bounded(
            client,
            url,
            max_bytes=_provider_response_max_bytes(max_output_tokens),
            timeout_s=attempt_timeout,
            headers={"x-goog-api-key": api_key},
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            },
        )
        try:
            resolved_model = _safe_provider_string(
                response.get("modelVersion"), maximum=255
            )
            if resolved_model is not None and resolved_model.startswith("models/"):
                resolved_model = resolved_model.removeprefix("models/")
            if _observation is not None:
                _observation.resolved_model = resolved_model
                _observation.provider_request_id = _safe_provider_string(
                    response.get("responseId"), maximum=255
                )
            if _observation is not None and resolved_model != model:
                if _observation is not None:
                    _observation.result_status = ResultStatus.NOT_APPLICABLE
                    _observation.failure_code = PRICE_ATTESTATION_MODEL_MISMATCH
                return None
            usage = normalize_gemini_usage(response)
        except LLMUsageContractError:
            if _observation is not None:
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_usage_contract_invalid"
            return None
        if _observation is not None:
            _observation.usage = usage
        cands = response.get("candidates") or []
        if not cands:
            if _observation is not None:
                _observation.result_status = ResultStatus.EMPTY
                _observation.failure_code = "chat_empty_response"
            return None
        candidate = cands[0]
        if not isinstance(candidate, dict):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        finish_reason = _safe_provider_string(
            candidate.get("finishReason"), maximum=64
        )
        if _observation is not None:
            _observation.finish_reason = finish_reason
        if finish_reason == "MAX_TOKENS":
            if _observation is not None:
                _observation.result_status = ResultStatus.OUTPUT_LIMIT_REJECTED
                _observation.failure_code = "chat_output_limit"
            log.warning("chat: Gemini hit its output limit")
            return None
        if finish_reason != "STOP":
            if _observation is not None:
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_finish_reason_invalid"
            log.warning("chat: Gemini returned a rejected terminal result")
            return None
        content = candidate.get("content")
        if not isinstance(content, dict):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts:
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                if _observation is not None:
                    _observation.result_status = ResultStatus.SCHEMA_REJECTED
                    _observation.failure_code = "chat_response_schema_invalid"
                return None
            texts.append(part["text"])
        text = "".join(texts).strip()
        if not text:
            if _observation is not None:
                _observation.result_status = ResultStatus.EMPTY
                _observation.failure_code = "chat_empty_response"
            return None
        return text


async def _atlas_chat(
    *,
    base_url: str,
    api_key: str,
    agent_id: str,
    system: str,
    prompt: str,
    timeout_s: float,
    requested_max_output_tokens: int,
    _dispatch: _ProviderDispatchCapability,
    _observation: _ProviderObservation | None = None,
) -> str | None:
    """AI-ATLAS public agent API (hansol). Two steps: create a session, then
    post the message; the reply is ``response.message``. Atlas has no system
    role (the agent's behaviour is configured on the Atlas side), so the
    system text is prepended to the message. Auth = ``x-api-key`` header."""
    output_char_ceiling = (
        requested_max_output_tokens * _SUBSCRIPTION_CHARS_PER_TOKEN
    )
    base = base_url.rstrip("/")
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    # Atlas requires a remote session before its message endpoint.  Reserve
    # before that external dispatch: this deliberately fails safe if a crash
    # leaves it unclear whether the message was subsequently accepted.
    attempt_timeout = _authorized_provider_timeout(
        _dispatch,
        provider="atlas",
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=requested_max_output_tokens,
    )
    deadline = monotonic() + attempt_timeout

    def _remaining_timeout() -> float | None:
        remaining = deadline - monotonic()
        return remaining if remaining > 0 else None

    async with httpx.AsyncClient(timeout=attempt_timeout) as client:
        session_timeout = _remaining_timeout()
        if session_timeout is None:
            if _observation is not None:
                _observation.attempt_status = AttemptStatus.TIMEOUT
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_timeout"
            return None
        session_response = await _post_json_bounded(
            client,
            f"{base}/agents/{agent_id}/sessions",
            max_bytes=_ATLAS_SESSION_RESPONSE_MAX_BYTES,
            timeout_s=session_timeout,
            headers=headers,
            payload={"title": "Mnemos"},
        )
        session_id = _safe_provider_string(
            session_response.get("id"), maximum=255
        )
        if session_id is None:
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_atlas_session_invalid"
            return None
        message = f"{system}\n\n{prompt}" if system else prompt
        message_timeout = _remaining_timeout()
        if message_timeout is None:
            if _observation is not None:
                _observation.attempt_status = AttemptStatus.TIMEOUT
                _observation.result_status = ResultStatus.NOT_APPLICABLE
                _observation.failure_code = "chat_timeout"
                _observation.usage = unavailable_usage(
                    lost_after_dispatch=True,
                    source=UsageSource.PROVIDER_API,
                )
            log.warning("chat: Atlas deadline expired before message dispatch")
            return None
        answer_response = await _post_json_bounded(
            client,
            f"{base}/agents/{agent_id}/sessions/{session_id}/messages",
            max_bytes=_provider_response_max_bytes(requested_max_output_tokens),
            timeout_s=message_timeout,
            headers=headers,
            payload={"message": message},
        )
        answer = answer_response.get("message")
        if not isinstance(answer, str):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        if len(answer) > output_char_ceiling:
            if _observation is not None:
                _observation.attempt_status = AttemptStatus.FAILED
                _observation.result_status = ResultStatus.OUTPUT_LIMIT_REJECTED
                _observation.failure_code = "chat_output_limit"
            raise _ProviderResponseTooLarge(
                f"Atlas output exceeded {output_char_ceiling} chars"
            )
        text = answer.strip()
        if not text:
            if _observation is not None:
                _observation.result_status = ResultStatus.EMPTY
                _observation.failure_code = "chat_empty_response"
            return None
        return text


async def _claude_api(
    *,
    api_key: str,
    model: str | None,
    system: str,
    prompt: str,
    timeout_s: float,
    max_output_tokens: int,
    _dispatch: _ProviderDispatchCapability,
    _observation: _ProviderObservation | None = None,
) -> str | None:
    """Direct Anthropic API — ~10-30s, far faster than the subprocess."""
    from app.llm.privacy import enforce_provider_logging_privacy

    enforce_provider_logging_privacy()
    import anthropic  # noqa: PLC0415

    # The SDK defaults to hidden transport retries. Mnemos owns physical
    # attempt accounting, so one reservation maps to one dispatch.
    client = anthropic.AsyncAnthropic(
        api_key=api_key,
        base_url=OFFICIAL_ANTHROPIC_API_BASE_URL,
        max_retries=0,
    )
    attempt_timeout = _authorized_provider_timeout(
        _dispatch,
        provider="anthropic",
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=max_output_tokens,
    )
    resp = await asyncio.wait_for(
        client.messages.create(
            model=model or "claude-sonnet-4-6",
            system=system,
            max_tokens=max_output_tokens,
            messages=[{"role": "user", "content": prompt}],
        ),
        timeout=attempt_timeout,
    )
    stop_reason = _safe_provider_string(
        getattr(resp, "stop_reason", None), maximum=64
    )
    resolved_model = _safe_provider_string(
        getattr(resp, "model", None), maximum=255
    )
    if _observation is not None:
        _observation.resolved_model = resolved_model
        _observation.provider_request_id = _safe_provider_string(
            getattr(resp, "id", None), maximum=255
        )
        _observation.finish_reason = stop_reason
    if (
        _observation is not None
        and resolved_model != (model or "claude-sonnet-4-6")
    ):
        if _observation is not None:
            _observation.result_status = ResultStatus.NOT_APPLICABLE
            _observation.failure_code = PRICE_ATTESTATION_MODEL_MISMATCH
        return None
    try:
        usage = normalize_anthropic_usage(resp)
    except LLMUsageContractError:
        if _observation is not None:
            _observation.result_status = ResultStatus.NOT_APPLICABLE
            _observation.failure_code = "chat_usage_contract_invalid"
        return None
    if _observation is not None:
        _observation.usage = usage
    if stop_reason == "max_tokens":
        if _observation is not None:
            _observation.result_status = ResultStatus.OUTPUT_LIMIT_REJECTED
            _observation.failure_code = "chat_output_limit"
        log.warning("chat: anthropic API hit its output limit")
        return None
    if stop_reason != "end_turn":
        if _observation is not None:
            _observation.result_status = ResultStatus.NOT_APPLICABLE
            _observation.failure_code = "chat_finish_reason_invalid"
        log.warning("chat: anthropic API returned a rejected terminal result")
        return None
    content = getattr(resp, "content", None)
    if not isinstance(content, list):
        if _observation is not None:
            _observation.result_status = ResultStatus.SCHEMA_REJECTED
            _observation.failure_code = "chat_response_schema_invalid"
        return None
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", "") != "text":
            continue
        text = getattr(block, "text", None)
        if not isinstance(text, str):
            if _observation is not None:
                _observation.result_status = ResultStatus.SCHEMA_REJECTED
                _observation.failure_code = "chat_response_schema_invalid"
            return None
        parts.append(text)
    answer = "\n".join(parts).strip()
    if not answer:
        if _observation is not None:
            _observation.result_status = ResultStatus.EMPTY
            _observation.failure_code = "chat_empty_response"
        return None
    return answer


async def provider_chat(
    provider: str,
    cfg: dict[str, dict],
    *,
    system: str,
    prompt: str,
    timeout_s: int = 180,
    max_output_tokens: int = _MAX_TOKENS,
    run_budget: LLMRunBudget | None = None,
    operation_id: uuid.UUID | None = None,
    attempt_state: ChatProviderAttemptState | None = None,
    semantic_codec: ChatSemanticCodec | None = None,
) -> str | None:
    """Dispatch a one-shot answer to ``provider`` using the resolved
    config. Returns markdown, or ``None`` on any failure (logged).

    ``max_output_tokens`` is provider-enforced by OpenAI, Gemini, and direct
    Anthropic calls. Atlas is one conservative logical attempt covering its
    required session+message POST pair and has client-side byte/character
    ceilings. If a v1 callback is installed, STARTED is committed before the
    first physical dispatch and every terminal path finalizes that row.
    """
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= _MAX_TOKENS
    ):
        raise ValueError(f"max_output_tokens must be an integer in 1..{_MAX_TOKENS}")
    c = cfg.get(provider) or {}
    callbacks = None
    if attempt_state is not None:
        attempt_state.attempt_id = None
        attempt_state.consumer_result_status = None
        attempt_state.consumer_failure_code = None

    if provider == "openai":
        canonical_provider = "openai"
        requested_model = (
            _safe_provider_string(c.get("model"), maximum=255)
            or "gpt-4o-2024-11-20"
        )
        base_url = c.get("base_url") or OFFICIAL_OPENAI_CHAT_API_BASE_URL
        billing_route = (
            "openai_chat_completions_api"
            if is_price_attested_openai_chat_base_url(base_url)
            else None
        )
        route_fingerprint = fingerprint_input(
            "mnemos.chat.provider-route.v2",
            canonical_provider,
            str(base_url),
        )
    elif provider == "gemini":
        canonical_provider = "gemini"
        requested_model = (
            _safe_provider_string(c.get("model"), maximum=255)
            or "gemini-2.5-flash"
        )
        base_url = None
        billing_route = "gemini_generate_content_api"
        route_fingerprint = fingerprint_input(
            "mnemos.chat.provider-route.v2",
            canonical_provider,
            OFFICIAL_GEMINI_GENERATE_API_BASE_URL,
        )
    elif provider == "atlas":
        log.warning("chat: %s", ATLAS_GENERATION_DISABLED_REASON)
        return None
    elif provider == "claudecode":
        if (c.get("mode") or "api") != "api" or not c.get("api_key"):
            return None
        canonical_provider = "anthropic"
        requested_model = (
            _safe_provider_string(c.get("model"), maximum=255)
            or "claude-sonnet-4-6"
        )
        base_url = None
        billing_route = "anthropic_messages_api"
        route_fingerprint = fingerprint_input(
            "mnemos.chat.provider-route.v2",
            canonical_provider,
            f"{OFFICIAL_ANTHROPIC_API_BASE_URL}/v1/messages",
        )
    else:
        return None

    # No provider adapter owns persistence by itself.  The Chat request must
    # install a task-local durable lifecycle capability before this public
    # dispatch boundary; otherwise an internal/direct caller could spend
    # tokens without first committing a physical-attempt row.
    callbacks = require_attempt_callbacks("Chat provider dispatch")
    if operation_id is None:
        raise LLMLifecycleError("Chat attempt requires a stable operation id")
    if callbacks.finish_candidate is None or (
        callbacks.replay_attempt is None
        and callbacks.replay_candidate is None
    ):
        raise LLMLifecycleError(
            "Chat provider dispatch requires semantic candidate capabilities"
        )
    if semantic_codec is None:
        # Direct adapter callers still receive the same crash-durable contract.
        # The product route supplies a stronger project/purpose binding below.
        semantic_codec = chat_answer_semantic_codec(system, prompt)

    estimate = _provider_input_reservation(system, prompt)
    input_fingerprint = fingerprint_input(system, prompt)
    bound_operation_id = _bound_provider_operation_id(
        operation_id,
        provider=canonical_provider,
        model=requested_model,
        route_fingerprint=route_fingerprint,
        requested_max_output_tokens=max_output_tokens,
        system=system,
        prompt=prompt,
    )
    # Production budget reservation belongs to the durable start callback.
    # Keeping a local reservation here would make an already-stored exact
    # candidate unreplayable under a smaller/exhausted fresh request budget.
    local_timeout = float(timeout_s)
    ticket: AttemptTicket | None = None
    if callbacks is not None:
        try:
            ticket = await callbacks.start(
                AttemptStartMetadata(
                    operation_id=bound_operation_id,
                    attempt_no=1,
                    attempt_kind=AttemptKind.PRIMARY,
                    provider=canonical_provider,
                    provider_mode="api",
                    requested_model=requested_model,
                    input_fingerprint=input_fingerprint,
                    estimated_input_tokens=estimate,
                    input_estimate_method="utf8_bytes_upper_v1",
                    requested_max_output_tokens=max_output_tokens,
                    usage_source=UsageSource.PROVIDER_API,
                    billing_route=billing_route,
                )
            )
        except LLMAttemptReplayBlocked:
            replay_request = SemanticCandidateReplayRequest(
                operation_id=bound_operation_id,
                attempt_no=1,
                input_fingerprint=input_fingerprint,
                contract_name=semantic_codec.contract_name,
                binding_fingerprint=semantic_codec.binding_fingerprint,
                normalizer=semantic_codec.normalizer,
            )
            if callbacks.replay_attempt is not None:
                replay = await callbacks.replay_attempt(replay_request)
                if replay.state == AttemptReplayState.STARTED:
                    raise LLMAttemptReplayBlocked(
                        "Chat operation is still in progress"
                    )
                if replay.state == AttemptReplayState.ABSENT:
                    raise LLMSemanticCandidateCorrupt(
                        "Chat duplicate attempt disappeared"
                    )
                if attempt_state is not None:
                    attempt_state.attempt_id = replay.attempt_id
                    attempt_state.consumer_result_status = replay.result_status
                    attempt_state.consumer_failure_code = replay.failure_code
                if replay.state == AttemptReplayState.TERMINAL_WITHOUT_CANDIDATE:
                    return None
            else:
                replayer = callbacks.replay_candidate
                if replayer is None:  # pragma: no cover - guarded above
                    raise LLMLifecycleError("Chat semantic replayer disappeared")
                replay = await replayer(replay_request)
            if attempt_state is not None:
                attempt_state.attempt_id = replay.attempt_id
                attempt_state.consumer_result_status = replay.result_status
                attempt_state.consumer_failure_code = (
                    "chat_candidate_rejected"
                    if replay.candidate_status == "rejected"
                    else None
                )
            if replay.candidate_status not in {"candidate", "accepted"}:
                return None
            if replay.result_status not in {
                ResultStatus.PENDING,
                ResultStatus.ACCEPTED,
            }:
                raise LLMSemanticCandidateCorrupt(
                    "Chat replay candidate classification is inconsistent"
                )
            try:
                return semantic_codec.render_candidate(replay.payload)
            except Exception:
                raise LLMSemanticCandidateCorrupt(
                    "Chat replay candidate rendering failed"
                ) from None
        remaining_seconds = ticket.remaining_seconds
        if remaining_seconds <= 0:
            outcome = AttemptOutcome(
                attempt_status=AttemptStatus.TIMEOUT,
                result_status=ResultStatus.NOT_APPLICABLE,
                usage=unavailable_usage(
                    lost_after_dispatch=False,
                    source=UsageSource.PROVIDER_API,
                ),
                failure_code="chat_timeout",
            )
            await callbacks.finish(ticket, outcome)
            if attempt_state is not None:
                attempt_state.attempt_id = ticket.attempt_id
                attempt_state.consumer_result_status = outcome.result_status
                attempt_state.consumer_failure_code = outcome.failure_code
            return None
        local_timeout = min(local_timeout, remaining_seconds)

    if ticket is None:
        raise LLMLifecycleError(
            "Chat provider dispatch has no durable STARTED ticket"
        )
    dispatch_capability = _issue_provider_dispatch_capability(
        ticket,
        provider=canonical_provider,
        system=system,
        prompt=prompt,
        timeout_s=local_timeout,
        requested_max_output_tokens=max_output_tokens,
    )

    observation = _ProviderObservation(
        usage=unavailable_usage(source=UsageSource.PROVIDER_API)
    )

    def _outcome() -> AttemptOutcome:
        return AttemptOutcome(
            attempt_status=observation.attempt_status,
            result_status=observation.result_status,
            usage=observation.usage,
            resolved_model=observation.resolved_model,
            provider_request_id=observation.provider_request_id,
            failure_code=observation.failure_code,
            finish_reason=observation.finish_reason,
        )

    async def _finish(candidate: SemanticCandidate | None = None) -> None:
        if callbacks is None or ticket is None:
            return
        if candidate is not None:
            if callbacks.finish_candidate is None:  # pragma: no cover - guarded above
                raise LLMLifecycleError(
                    "Chat semantic candidate finalizer is unavailable"
                )
            await callbacks.finish_candidate(ticket, _outcome(), candidate)
            return
        await callbacks.finish(ticket, _outcome())

    def _set_attempt_state() -> None:
        if ticket is not None and attempt_state is not None:
            attempt_state.attempt_id = ticket.attempt_id
            attempt_state.consumer_result_status = observation.result_status
            attempt_state.consumer_failure_code = observation.failure_code

    if callbacks is not None and callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    try:
        if provider == "openai":
            reply = await _openai_compatible(
                base_url=base_url,
                api_key=c.get("api_key") or "",
                model=requested_model,
                system=system,
                prompt=prompt,
                timeout_s=local_timeout,
                max_output_tokens=max_output_tokens,
                _dispatch=dispatch_capability,
                _observation=observation,
            )
        elif provider == "atlas":
            reply = await _atlas_chat(
                base_url=base_url,
                api_key=c.get("api_key") or "",
                agent_id=c.get("agent_id") or "",
                system=system,
                prompt=prompt,
                timeout_s=local_timeout,
                requested_max_output_tokens=max_output_tokens,
                _dispatch=dispatch_capability,
                _observation=observation,
            )
        elif provider == "gemini":
            reply = await _gemini_generate(
                api_key=c.get("api_key") or "",
                model=requested_model,
                system=system,
                prompt=prompt,
                timeout_s=local_timeout,
                max_output_tokens=max_output_tokens,
                _dispatch=dispatch_capability,
                _observation=observation,
            )
        else:
            reply = await _claude_api(
                api_key=c.get("api_key") or "",
                model=requested_model,
                system=system,
                prompt=prompt,
                timeout_s=local_timeout,
                max_output_tokens=max_output_tokens,
                _dispatch=dispatch_capability,
                _observation=observation,
            )
    except asyncio.CancelledError:
        observation.attempt_status = AttemptStatus.CANCELLED
        observation.result_status = ResultStatus.NOT_APPLICABLE
        observation.failure_code = "chat_cancelled"
        observation.usage = unavailable_usage(
            lost_after_dispatch=True,
            source=UsageSource.PROVIDER_API,
        )
        await asyncio.shield(_finish())
        raise
    except (TimeoutError, httpx.TimeoutException):
        observation.attempt_status = AttemptStatus.TIMEOUT
        observation.result_status = ResultStatus.NOT_APPLICABLE
        observation.failure_code = "chat_timeout"
        observation.usage = unavailable_usage(
            lost_after_dispatch=True,
            source=UsageSource.PROVIDER_API,
        )
        log.warning("chat provider %s timed out", provider)
        await _finish()
        return None
    except _ProviderResponseTooLarge:
        observation.attempt_status = AttemptStatus.FAILED
        observation.result_status = ResultStatus.OUTPUT_LIMIT_REJECTED
        observation.failure_code = "chat_output_limit"
        observation.usage = unavailable_usage(
            lost_after_dispatch=True,
            source=UsageSource.PROVIDER_API,
        )
        log.warning("chat provider %s exceeded its retained response limit", provider)
        await _finish()
        return None
    except json.JSONDecodeError:
        observation.attempt_status = AttemptStatus.COMPLETED
        observation.result_status = ResultStatus.PARSE_REJECTED
        observation.failure_code = "chat_response_parse_invalid"
        log.warning("chat provider %s returned invalid JSON", provider)
        await _finish()
        return None
    except httpx.HTTPStatusError as exc:
        observation.attempt_status = AttemptStatus.FAILED
        observation.result_status = ResultStatus.NOT_APPLICABLE
        observation.failure_code = "chat_http_error"
        observation.usage = unavailable_usage(
            lost_after_dispatch=True,
            source=UsageSource.PROVIDER_API,
        )
        log.warning(
            "chat provider %s HTTP %s",
            provider,
            exc.response.status_code,
        )
        await _finish()
        return None
    except (LLMLifecycleError, RunBudgetExceeded):
        raise
    except Exception as exc:  # noqa: BLE001
        observation.attempt_status = AttemptStatus.FAILED
        observation.result_status = ResultStatus.NOT_APPLICABLE
        observation.failure_code = "chat_transport_error"
        observation.usage = unavailable_usage(
            lost_after_dispatch=True,
            source=UsageSource.PROVIDER_API,
        )
        log.warning("chat provider %s failed: %s", provider, exc.__class__.__name__)
        await _finish()
        return None

    if reply is None and observation.result_status == ResultStatus.PENDING:
        observation.result_status = ResultStatus.NOT_APPLICABLE
        observation.failure_code = "chat_provider_rejected"
    if reply is None:
        await _finish()
        _set_attempt_state()
        return None

    try:
        normalized_payload = semantic_codec.normalizer(
            semantic_codec.normalize_response(reply)
        )
        canonical_reply = semantic_codec.render_candidate(normalized_payload)
        candidate = SemanticCandidate(
            contract_name=semantic_codec.contract_name,
            binding_fingerprint=semantic_codec.binding_fingerprint,
            payload=normalized_payload,
        )
    except Exception as exc:  # noqa: BLE001
        result_status = getattr(exc, "result_status", ResultStatus.SCHEMA_REJECTED)
        if result_status not in {
            ResultStatus.PARSE_REJECTED,
            ResultStatus.SCHEMA_REJECTED,
        }:
            result_status = ResultStatus.SCHEMA_REJECTED
        observation.result_status = result_status
        observation.failure_code = (
            semantic_codec.parse_failure_code
            if result_status == ResultStatus.PARSE_REJECTED
            else semantic_codec.schema_failure_code
        )
        await _finish()
        _set_attempt_state()
        log.warning("chat provider semantic response contract rejected")
        return None

    await _finish(candidate)
    _set_attempt_state()
    return canonical_reply


# ── Live connection test + model discovery ────────────────────────────
async def test_provider(
    provider: str,
    cfg: dict[str, dict],
    *,
    timeout_s: int = 15,
) -> dict:
    """Validate the provider's key/base_url with a cheap models-list call.
    Returns ``{ok, message, models}`` — ``models`` lets the Settings UI
    refresh its dropdown with what the account can actually use."""
    c = cfg.get(provider) or {}
    key = c.get("api_key")
    try:
        if provider == "openai":
            base = c.get("base_url") or OFFICIAL_OPENAI_CHAT_API_BASE_URL
            if not key:
                return {"ok": False, "message": "missing key", "models": []}
            if not is_price_attested_openai_chat_base_url(base):
                # Do not send a platform API key to an arbitrary compatible
                # endpoint. Custom OpenAI wire-compatible generation is not
                # price-attested and is already denied by the paid-dispatch
                # lifecycle, so its unaudited live probe is denied as well.
                return {
                    "ok": False,
                    "message": "OpenAI base URL is not price-attested",
                    "models": [],
                }
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    base.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
                )
                r.raise_for_status()
                models = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            return {"ok": True, "message": f"OK — {len(models)} models", "models": sorted(models)}

        if provider == "atlas":
            base = c.get("base_url")
            agent = c.get("agent_id")
            if not (base and key and agent):
                return {"ok": False, "message": "missing key / agent ID / base URL", "models": []}
            # Atlas exposes no read-only discovery probe in this contract.
            # Creating a session is an external mutation and could initialize
            # provider-side agent work without a project/attempt identity.
            return {
                "ok": False,
                "message": (
                    "Atlas live test disabled — no read-only, unattributed "
                    "connection probe is available"
                ),
                "models": [],
            }

        if provider == "gemini":
            if not key:
                return {"ok": False, "message": "missing key", "models": []}
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    f"{OFFICIAL_GEMINI_GENERATE_API_BASE_URL}/models",
                    headers={"x-goog-api-key": key},
                )
                r.raise_for_status()
                models = [
                    (m.get("name") or "").removeprefix("models/")
                    for m in (r.json().get("models") or [])
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])
                ]
            return {
                "ok": True,
                "message": f"OK — {len(models)} models",
                "models": sorted(m for m in models if m),
            }

        if provider == "claudecode":
            mode = c.get("mode") or "api"
            if mode != "api":
                # Importability cannot prove subscription billing, and this
                # connection-test endpoint has no durable physical-attempt
                # ledger.  Do not perform an unattributed provider call even
                # when the SDK happens to be installed.
                return {
                    "ok": False,
                    "message": (
                        "Agent SDK Chat 비활성 — Anthropic API 모드와 키가 필요함; "
                        "내부 SDK 호출은 인증·비용 출처 확인 및 128K 예약 필요"
                    ),
                    "models": [],
                }
            if not key:
                return {"ok": False, "message": "missing key", "models": []}
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    f"{OFFICIAL_ANTHROPIC_API_BASE_URL}/v1/models",
                    headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                )
                r.raise_for_status()
                models = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
            return {"ok": True, "message": f"OK — {len(models)} models", "models": sorted(models)}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "message": f"HTTP {exc.response.status_code}", "models": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": exc.__class__.__name__, "models": []}
    return {"ok": False, "message": "unknown provider", "models": []}
