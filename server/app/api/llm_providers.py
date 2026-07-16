"""PR-178/179 — multi-provider LLM backend for the Chat tab.

The operator picks which AI answers a chat message: OpenAI, Gemini, Claude
Code, or Atlas (hansol ai). Claude runs on the local Claude Code
subscription by default (no key); Atlas is the hansol agent API (key +
agent ID, two-step session call). Each provider's config is resolved per
request from two sources, DB first then env:

  1. Platform-provisioned DB config: keys in the encrypted ``Secret`` table
     (label ``chat-provider:<suffix>``), models / agent / base_url / mode in
     the global ``PlatformSetting`` row ``chat_providers``.
  2. Env fallback: OPENAI_API_KEY/OPENAI_MODEL, GEMINI_API_KEY/GEMINI_MODEL,
     ATLAS_API_KEY/ATLAS_AGENT_ID/ATLAS_BASE_URL, ANTHROPIC_API_KEY/MNEMOS_CHAT_MODEL.

``resolve_config(db)`` returns ``{provider: {api_key, model, base_url}}``;
the availability/dispatch helpers take that config (they never read env or
DB themselves, which keeps them pure and testable). Calls go over HTTP
(httpx) — no provider SDKs except ``anthropic`` for the Claude direct API.
``provider_chat`` returns the markdown reply or ``None`` (never raises).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib.parse import urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent_sdk import is_agent_sdk_available
from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
from app.models.auth import PlatformSetting, Secret
from app.safety.crypto import decrypt

log = logging.getLogger("mnemos.chat.providers")

_MAX_TOKENS = 3000
_SUBSCRIPTION_CHARS_PER_TOKEN = 8
_HTTP_JSON_OVERHEAD_BYTES = 64 * 1024
_ATLAS_SESSION_RESPONSE_MAX_BYTES = 32 * 1024


class _SubscriptionOutputTooLarge(RuntimeError):
    """The SDK streamed beyond Mnemos's finite client-side output ceiling."""


class _ProviderResponseTooLarge(RuntimeError):
    """A provider response exceeded Mnemos's finite client-side byte ceiling."""


def _provider_input_reservation(system: str, prompt: str) -> int:
    """Return a conservative provider-independent input reservation.

    Provider tokenizers differ, especially for Korean source questions.  The
    UTF-8 byte length is intentionally an upper-bound-style reservation rather
    than a claimed billed-token count.  Actual provider usage, when available,
    belongs in the physical-call ledger and never overwrites this estimate.
    """

    return max(1, len(system.encode("utf-8")) + len(prompt.encode("utf-8")) + 16)


def _reserve_provider_attempt(
    run_budget: LLMRunBudget | None,
    *,
    system: str,
    prompt: str,
    timeout_s: int,
    requested_max_output_tokens: int,
) -> float:
    """Reserve one observable network/SDK attempt and return its timeout."""

    if run_budget is None:
        return float(timeout_s)
    remaining = run_budget.reserve(
        _provider_input_reservation(system, prompt),
        requested_output_tokens=requested_max_output_tokens,
    )
    return max(0.001, min(float(timeout_s), remaining))


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
    headers: dict[str, str],
    payload: dict,
) -> dict:
    """POST JSON and parse one bounded JSON-object response.

    ``httpx.post`` buffers the entire body before returning.  Streaming here
    makes the byte ceiling a memory boundary rather than a check performed
    after an arbitrarily large provider response has already been allocated.
    """

    async with client.stream("POST", url, headers=headers, json=payload) as response:
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

    host = (urlparse(base_url).hostname or "").casefold()
    model_name = model.casefold().rsplit("/", 1)[-1]
    if host == "api.openai.com" or model_name.startswith(("o1", "o3", "o4", "gpt-5")):
        return "max_completion_tokens"
    return "max_tokens"


# Platform-owned provider configuration namespaces.
SETTING_KEY = "chat_providers"  # PlatformSetting row (models/base_url)
SECRET_PREFIX = "chat-provider:"  # Secret label prefix (API keys)
SECRET_KIND = "llm_api_key"

PROVIDER_ORDER = ["claudecode", "openai", "gemini", "atlas"]
PROVIDER_LABELS = {
    "claudecode": "Claude Code (구독/API)",
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
    "openai": "gpt-4o",
    "gemini": "gemini-2.5-flash",
    "atlas": "",
    "claudecode": "claude-sonnet-4-6",
}
# Current models offered in the Settings dropdowns (Context7-sourced, 2026).
# The free-text field and the live "test" model fetch both override these,
# so a stale entry here never blocks a newer model.
SUGGESTED_MODELS = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-5.4", "o3"],
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
        base_url="https://api.openai.com/v1",
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
    # Claude runs on the local Claude Code subscription by default; "api"
    # switches it to the direct Anthropic API (which needs a key).
    cfg["claudecode"]["mode"] = (saved.get("claudecode") or {}).get("mode") or "subscription"

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
        return bool(c.get("api_key")) or is_agent_sdk_available()
    if provider == "atlas":
        return bool(c.get("api_key") and c.get("agent_id") and c.get("base_url"))
    if provider in ("openai", "gemini"):
        return bool(c.get("api_key"))
    return False


def output_token_limit_capability(provider: str, cfg: dict[str, dict]) -> dict:
    """Describe whether ``max_output_tokens`` survives the selected path.

    OpenAI, Gemini, and the direct Anthropic API expose a request field.  The
    Claude subscription SDK and Atlas public-agent endpoint do not, so Mnemos
    enforces conservative client byte/character ceilings and rejects the
    whole response on overflow.  Claude's provider id can fall back between
    direct and subscription modes, so API-key configurations are labelled
    ``partial``.
    """

    if provider == "openai":
        c = cfg.get(provider) or {}
        field = _openai_output_limit_field(
            base_url=c.get("base_url") or "https://api.openai.com/v1",
            model=c.get("model") or "gpt-4o",
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
            "output_token_limit_enforcement": "client",
            "output_token_limit_detail": "atlas_client_byte_and_character_ceiling",
        }
    if provider == "claudecode":
        c = cfg.get(provider) or {}
        if c.get("api_key"):
            return {
                "output_token_limit_enforcement": "partial",
                "output_token_limit_detail": (
                    "anthropic_api_token_field_or_subscription_client_char_ceiling"
                ),
            }
        return {
            "output_token_limit_enforcement": "client",
            "output_token_limit_detail": ("claude_subscription_sdk_client_char_ceiling"),
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
    timeout_s: int,
    max_output_tokens: int,
    run_budget: LLMRunBudget | None = None,
) -> str | None:
    """A /chat/completions call in the OpenAI wire format — serves both
    OpenAI proper and any OpenAI-compatible endpoint (Atlas)."""
    url = base_url.rstrip("/") + "/chat/completions"
    output_limit_field = _openai_output_limit_field(base_url=base_url, model=model)
    attempt_timeout = _reserve_provider_attempt(
        run_budget,
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
        choices = response.get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message", {}).get("content") or "").strip() or None


async def _gemini_generate(
    *,
    api_key: str,
    model: str,
    system: str,
    prompt: str,
    timeout_s: int,
    max_output_tokens: int,
    run_budget: LLMRunBudget | None = None,
) -> str | None:
    # Auth via the x-goog-api-key header (current docs) — never the ?key=
    # query param, so the key never lands in a URL/log.
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    attempt_timeout = _reserve_provider_attempt(
        run_budget,
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
            headers={"x-goog-api-key": api_key},
            payload={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": max_output_tokens},
            },
        )
        cands = response.get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip() or None


async def _atlas_chat(
    *,
    base_url: str,
    api_key: str,
    agent_id: str,
    system: str,
    prompt: str,
    timeout_s: int,
    requested_max_output_tokens: int,
    run_budget: LLMRunBudget | None = None,
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
    attempt_timeout = _reserve_provider_attempt(
        run_budget,
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=requested_max_output_tokens,
    )
    async with httpx.AsyncClient(timeout=attempt_timeout) as client:
        session_response = await _post_json_bounded(
            client,
            f"{base}/agents/{agent_id}/sessions",
            max_bytes=_ATLAS_SESSION_RESPONSE_MAX_BYTES,
            headers=headers,
            payload={"title": "Mnemos"},
        )
        session_id = session_response.get("id")
        if not session_id:
            return None
        message = f"{system}\n\n{prompt}" if system else prompt
        answer_response = await _post_json_bounded(
            client,
            f"{base}/agents/{agent_id}/sessions/{session_id}/messages",
            max_bytes=_provider_response_max_bytes(requested_max_output_tokens),
            headers=headers,
            payload={"message": message},
        )
        answer = answer_response.get("message")
        if not isinstance(answer, str):
            return None
        if len(answer) > output_char_ceiling:
            raise _ProviderResponseTooLarge(
                f"Atlas output exceeded {output_char_ceiling} chars"
            )
        return answer.strip() or None


async def _claude_api(
    *,
    api_key: str,
    model: str | None,
    system: str,
    prompt: str,
    timeout_s: int,
    max_output_tokens: int,
    run_budget: LLMRunBudget | None = None,
) -> str | None:
    """Direct Anthropic API — ~10-30s, far faster than the subprocess."""
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.AsyncAnthropic(api_key=api_key)
        attempt_timeout = _reserve_provider_attempt(
            run_budget,
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
        parts = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"]
        return "\n".join(parts).strip() or None
    except RunBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("chat: anthropic API failed (%s); trying subscription", exc.__class__.__name__)
        return None


async def _claude_subscription(
    *,
    system: str,
    prompt: str,
    timeout_s: int,
    requested_max_output_tokens: int = _MAX_TOKENS,
    run_budget: LLMRunBudget | None = None,
) -> str | None:
    """Local Claude Code subscription — no API key, but ~60-180s/call."""
    if not is_agent_sdk_available():
        return None
    try:
        from claude_agent_sdk import (  # noqa: PLC0415
            AssistantMessage,
            ClaudeAgentOptions,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    output_char_ceiling = requested_max_output_tokens * _SUBSCRIPTION_CHARS_PER_TOKEN
    log.info(
        "chat: Claude subscription uses client output ceiling=%d chars "
        "for requested max_output_tokens=%d",
        output_char_ceiling,
        requested_max_output_tokens,
    )
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=system,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
    )

    async def _drain(sink: list[str]) -> None:
        collected_chars = 0
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        separator_chars = 1 if sink else 0
                        next_chars = collected_chars + separator_chars + len(block.text)
                        if next_chars > output_char_ceiling:
                            raise _SubscriptionOutputTooLarge(
                                f"subscription output exceeded {output_char_ceiling} chars"
                            )
                        sink.append(block.text)
                        collected_chars = next_chars

    # The Agent SDK's control handshake ("initialize") is flaky on a cold CLI
    # subprocess and raises *before* any tokens stream (observed on a large
    # repo: "Control request timeout: initialize" → 503, while the very next
    # call to the same prompt answers in ~90s). One retry warms it up. A real
    # content timeout (TimeoutError) is not retried — it would just wait again.
    for attempt in range(2):
        out: list[str] = []
        try:
            attempt_timeout = _reserve_provider_attempt(
                run_budget,
                system=system,
                prompt=prompt,
                timeout_s=timeout_s,
                requested_max_output_tokens=requested_max_output_tokens,
            )
            await asyncio.wait_for(_drain(out), timeout=attempt_timeout)
        except RunBudgetExceeded:
            raise
        except TimeoutError:
            log.warning("chat: subscription LLM timed out after %ds", timeout_s)
            return None
        except _SubscriptionOutputTooLarge:
            # Do not return a sliced model answer and do not retry after any
            # content has streamed: both would weaken grounding and can double
            # the provider work that this budget exists to bound.
            log.warning(
                "chat: subscription LLM exceeded client output ceiling=%d chars",
                output_char_ceiling,
            )
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "chat: subscription init attempt %d/2 failed: %s",
                attempt + 1,
                exc.__class__.__name__,
            )
            if out:
                return None
            continue
        return "\n".join(out).strip() or None
    return None


async def provider_chat(
    provider: str,
    cfg: dict[str, dict],
    *,
    system: str,
    prompt: str,
    timeout_s: int = 180,
    max_output_tokens: int = _MAX_TOKENS,
    run_budget: LLMRunBudget | None = None,
) -> str | None:
    """Dispatch a one-shot answer to ``provider`` using the resolved
    config. Returns markdown, or ``None`` on any failure (logged).

    ``max_output_tokens`` is provider-enforced by OpenAI, Gemini, and direct
    Anthropic calls.  Claude subscription and Atlas lack a token field, so
    Mnemos rejects whole responses beyond conservative client-side ceilings.
    """
    if (
        isinstance(max_output_tokens, bool)
        or not isinstance(max_output_tokens, int)
        or not 1 <= max_output_tokens <= _MAX_TOKENS
    ):
        raise ValueError(f"max_output_tokens must be an integer in 1..{_MAX_TOKENS}")
    c = cfg.get(provider) or {}
    try:
        if provider == "openai":
            return await _openai_compatible(
                base_url=c.get("base_url") or "https://api.openai.com/v1",
                api_key=c.get("api_key") or "",
                model=c.get("model") or "gpt-4o",
                system=system,
                prompt=prompt,
                timeout_s=timeout_s,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
        if provider == "atlas":
            return await _atlas_chat(
                base_url=c.get("base_url") or "",
                api_key=c.get("api_key") or "",
                agent_id=c.get("agent_id") or "",
                system=system,
                prompt=prompt,
                timeout_s=timeout_s,
                requested_max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
        if provider == "gemini":
            return await _gemini_generate(
                api_key=c.get("api_key") or "",
                model=c.get("model") or "gemini-2.5-flash",
                system=system,
                prompt=prompt,
                timeout_s=timeout_s,
                max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
        if provider == "claudecode":
            mode = c.get("mode") or "subscription"
            key = c.get("api_key")
            if mode == "api" and key:
                reply = await _claude_api(
                    api_key=key,
                    model=c.get("model"),
                    system=system,
                    prompt=prompt,
                    timeout_s=timeout_s,
                    max_output_tokens=max_output_tokens,
                    run_budget=run_budget,
                )
                if reply is not None:
                    return reply
                # API failed → fall back to the subscription so chat still works.
                return await _claude_subscription(
                    system=system,
                    prompt=prompt,
                    timeout_s=timeout_s,
                    requested_max_output_tokens=max_output_tokens,
                    run_budget=run_budget,
                )
            # Subscription mode (default): use the local Claude Code login.
            reply = await _claude_subscription(
                system=system,
                prompt=prompt,
                timeout_s=timeout_s,
                requested_max_output_tokens=max_output_tokens,
                run_budget=run_budget,
            )
            if reply is not None:
                return reply
            # Subscription unavailable → use an API key if one is configured.
            if key:
                return await _claude_api(
                    api_key=key,
                    model=c.get("model"),
                    system=system,
                    prompt=prompt,
                    timeout_s=timeout_s,
                    max_output_tokens=max_output_tokens,
                    run_budget=run_budget,
                )
            return None
        return None
    except httpx.HTTPStatusError as exc:
        log.warning(
            "chat provider %s HTTP %s",
            provider,
            exc.response.status_code,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat provider %s failed: %s", provider, exc.__class__.__name__)
        return None


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
            base = c.get("base_url") or "https://api.openai.com/v1"
            if not key:
                return {"ok": False, "message": "missing key", "models": []}
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
            # Creating a session validates the key + agent + base URL.
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.post(
                    base.rstrip("/") + f"/agents/{agent}/sessions",
                    headers={"x-api-key": key, "Content-Type": "application/json"},
                    json={"title": "Mnemos connection test"},
                )
                r.raise_for_status()
                name = r.json().get("agent_name") or agent
            return {"ok": True, "message": f"OK — agent: {name}", "models": []}

        if provider == "gemini":
            if not key:
                return {"ok": False, "message": "missing key", "models": []}
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
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
            mode = c.get("mode") or "subscription"
            if mode == "subscription" or not key:
                if not is_agent_sdk_available():
                    return {
                        "ok": False,
                        "message": "Claude Code subscription not detected "
                        "(run Mnemos inside Claude Code)",
                        "models": [],
                    }
                # Real round-trip — proves the subscription actually answers,
                # not just that the SDK is importable.
                reply = await _claude_subscription(
                    system="You are a connection test. Reply with exactly: OK",
                    prompt="Reply with the single word OK.",
                    timeout_s=max(60, min(timeout_s, 120)),
                )
                if reply:
                    return {"ok": True, "message": "OK — Claude 구독 응답 확인", "models": []}
                return {"ok": False, "message": "구독 호출 실패 또는 시간 초과", "models": []}
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    "https://api.anthropic.com/v1/models",
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
