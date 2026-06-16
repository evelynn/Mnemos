"""PR-178 — multi-provider LLM backend for the Chat tab.

The operator picks which AI answers a chat message: OpenAI, Gemini, Claude
Code (subscription/API), or Atlas (hansol ai). Each provider is configured
by env vars and called over HTTP (httpx) — no provider SDKs are required
except ``anthropic`` (already a dependency) for the Claude direct-API path::

  openai      OPENAI_API_KEY      OPENAI_MODEL  (default gpt-4o)
  gemini      GEMINI_API_KEY      GEMINI_MODEL  (default gemini-2.0-flash)
  atlas       ATLAS_API_KEY + ATLAS_BASE_URL    ATLAS_MODEL  (OpenAI-compatible)
  claudecode  ANTHROPIC_API_KEY (fast direct API) else local
              claude_agent_sdk subscription      MNEMOS_CHAT_MODEL

``provider_chat(provider, system=, prompt=, timeout_s=)`` dispatches and
returns the markdown reply or ``None`` (never raises).
``available_providers()`` reports which providers are configured so the UI
only offers usable ones; ``default_provider()`` is the initial selection.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from app.extractor.agent_sdk import is_agent_sdk_available

log = logging.getLogger("mnemos.chat.providers")

_MAX_TOKENS = 3000


def _env(key: str) -> str | None:
    v = os.environ.get(key)
    return v.strip() if v and v.strip() else None


# ── OpenAI-compatible (OpenAI + Atlas/hansol-ai) ──────────────────────
async def _openai_compatible(
    *, base_url: str, api_key: str, model: str,
    system: str, prompt: str, timeout_s: int,
) -> str | None:
    """A /chat/completions call in the OpenAI wire format — serves both
    OpenAI proper and any OpenAI-compatible endpoint (Atlas)."""
    url = base_url.rstrip("/") + "/chat/completions"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": _MAX_TOKENS,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
            },
        )
        r.raise_for_status()
        choices = r.json().get("choices") or []
        if not choices:
            return None
        return (choices[0].get("message", {}).get("content") or "").strip() or None


# ── Gemini (Google Generative Language REST) ──────────────────────────
async def _gemini_generate(
    *, api_key: str, model: str, system: str, prompt: str, timeout_s: int,
) -> str | None:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            url,
            params={"key": api_key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": _MAX_TOKENS},
            },
        )
        r.raise_for_status()
        cands = r.json().get("candidates") or []
        if not cands:
            return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip() or None


# ── Claude (fast direct API, else local subscription) ─────────────────
async def _claude_api(*, system: str, prompt: str, timeout_s: int) -> str | None:
    """Direct Anthropic API — returns in ~10-30s when ANTHROPIC_API_KEY is
    set (far faster and steadier than spawning the Claude Code subprocess
    per request)."""
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.AsyncAnthropic(api_key=key)
        resp = await asyncio.wait_for(
            client.messages.create(
                model=os.environ.get("MNEMOS_CHAT_MODEL", "claude-sonnet-4-6"),
                system=system,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=timeout_s,
        )
        parts = [
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        ]
        return "\n".join(parts).strip() or None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat: anthropic API failed (%s); trying subscription",
                    exc.__class__.__name__)
        return None


async def _claude_subscription(
    *, system: str, prompt: str, timeout_s: int,
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

    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=system,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
    )
    out: list[str] = []

    async def _drain() -> None:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        out.append(block.text)

    try:
        await asyncio.wait_for(_drain(), timeout=timeout_s)
    except TimeoutError:
        log.warning("chat: subscription LLM timed out after %ds", timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat: %s: %s", exc.__class__.__name__, exc)
        return None
    return "\n".join(out).strip() or None


async def _claude_call(*, system: str, prompt: str, timeout_s: int) -> str | None:
    reply = await _claude_api(system=system, prompt=prompt, timeout_s=timeout_s)
    if reply is not None:
        return reply
    return await _claude_subscription(
        system=system, prompt=prompt, timeout_s=timeout_s
    )


# ── Per-provider availability + dispatch ──────────────────────────────
def _openai_available() -> bool:
    return bool(_env("OPENAI_API_KEY"))


async def _openai_call(*, system: str, prompt: str, timeout_s: int) -> str | None:
    return await _openai_compatible(
        base_url="https://api.openai.com/v1",
        api_key=_env("OPENAI_API_KEY") or "",
        model=os.environ.get("OPENAI_MODEL") or "gpt-4o",
        system=system, prompt=prompt, timeout_s=timeout_s,
    )


def _atlas_available() -> bool:
    return bool(_env("ATLAS_API_KEY") and _env("ATLAS_BASE_URL"))


async def _atlas_call(*, system: str, prompt: str, timeout_s: int) -> str | None:
    return await _openai_compatible(
        base_url=_env("ATLAS_BASE_URL") or "",
        api_key=_env("ATLAS_API_KEY") or "",
        model=os.environ.get("ATLAS_MODEL") or "atlas",
        system=system, prompt=prompt, timeout_s=timeout_s,
    )


def _gemini_available() -> bool:
    return bool(_env("GEMINI_API_KEY"))


async def _gemini_call(*, system: str, prompt: str, timeout_s: int) -> str | None:
    return await _gemini_generate(
        api_key=_env("GEMINI_API_KEY") or "",
        model=os.environ.get("GEMINI_MODEL") or "gemini-2.0-flash",
        system=system, prompt=prompt, timeout_s=timeout_s,
    )


def _claude_available() -> bool:
    return bool(_env("ANTHROPIC_API_KEY")) or is_agent_sdk_available()


# id → {label, available, call}. Order = the UI's order. ``claudecode``
# leads because it works with the operator's Claude Code subscription with
# no extra key.
_PROVIDERS: dict[str, dict] = {
    "claudecode": {
        "label": "Claude Code (구독/API)",
        "available": _claude_available,
        "call": _claude_call,
    },
    "openai": {
        "label": "OpenAI",
        "available": _openai_available,
        "call": _openai_call,
    },
    "gemini": {
        "label": "Gemini",
        "available": _gemini_available,
        "call": _gemini_call,
    },
    "atlas": {
        "label": "Atlas (hansol ai)",
        "available": _atlas_available,
        "call": _atlas_call,
    },
}


def available_providers() -> list[dict]:
    """Every provider with a boolean ``available`` (its config present)."""
    return [
        {"id": pid, "label": p["label"], "available": p["available"]()}
        for pid, p in _PROVIDERS.items()
    ]


def any_provider_available() -> bool:
    return any(p["available"]() for p in _PROVIDERS.values())


def is_provider_available(provider: str) -> bool:
    p = _PROVIDERS.get(provider)
    return bool(p and p["available"]())


def default_provider() -> str:
    """First configured provider — the UI's initial selection."""
    for pid, p in _PROVIDERS.items():
        if p["available"]():
            return pid
    return "claudecode"


async def provider_chat(
    provider: str, *, system: str, prompt: str, timeout_s: int = 180,
) -> str | None:
    """Dispatch a one-shot answer to ``provider``. Returns the markdown
    reply, or ``None`` on any failure (config, HTTP, timeout) — never
    raises. The reason is logged for the operator."""
    p = _PROVIDERS.get(provider)
    if p is None:
        return None
    try:
        return await p["call"](system=system, prompt=prompt, timeout_s=timeout_s)
    except httpx.HTTPStatusError as exc:
        log.warning("chat provider %s HTTP %s: %s", provider,
                    exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat provider %s failed: %s: %s", provider,
                    exc.__class__.__name__, exc)
        return None
