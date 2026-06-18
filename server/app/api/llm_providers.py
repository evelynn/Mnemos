"""PR-178/179 — multi-provider LLM backend for the Chat tab.

The operator picks which AI answers a chat message: OpenAI, Gemini, Claude
Code, or Atlas (hansol ai). Claude runs on the local Claude Code
subscription by default (no key); Atlas is the hansol agent API (key +
agent ID, two-step session call). Each provider's config is resolved per
request from two sources, DB first then env:

  1. Settings UI (PR-179): keys in the encrypted ``Secret`` table
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
import logging
import os

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.agent_sdk import is_agent_sdk_available
from app.models.auth import PlatformSetting, Secret
from app.safety.crypto import decrypt

log = logging.getLogger("mnemos.chat.providers")

_MAX_TOKENS = 3000

# Where the Settings UI persists provider config.
SETTING_KEY = "chat_providers"          # PlatformSetting row (models/base_url)
SECRET_PREFIX = "chat-provider:"        # Secret label prefix (API keys)
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
    "gemini": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash",
               "gemini-2.0-flash"],
    "claudecode": ["claude-sonnet-4-6", "claude-opus-4-8",
                   "claude-haiku-4-5-20251001"],
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
    """Per-provider ``{api_key, model, base_url}`` — Settings UI (DB) wins
    over env, with a sensible default model when neither sets one."""
    cfg: dict[str, dict] = {
        pid: {"api_key": None, "model": None, "base_url": None}
        for pid in PROVIDER_ORDER
    }
    cfg["openai"].update(
        api_key=_env("OPENAI_API_KEY"), model=_env("OPENAI_MODEL"),
        base_url="https://api.openai.com/v1",
    )
    cfg["gemini"].update(
        api_key=_env("GEMINI_API_KEY"), model=_env("GEMINI_MODEL"),
    )
    cfg["atlas"].update(
        api_key=_env("ATLAS_API_KEY"),
        agent_id=_env("ATLAS_AGENT_ID"),
        base_url=_env("ATLAS_BASE_URL") or "https://ai-atlas.hansol.net/api/v1/public",
    )
    cfg["claudecode"].update(
        api_key=_env("ANTHROPIC_API_KEY"), model=_env("MNEMOS_CHAT_MODEL"),
    )

    # PlatformSetting: model + base_url overrides.
    ps = (
        await db.execute(
            select(PlatformSetting).where(PlatformSetting.key == SETTING_KEY)
        )
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
    cfg["claudecode"]["mode"] = (
        (saved.get("claudecode") or {}).get("mode") or "subscription"
    )

    # Secret: API-key overrides (encrypted at rest).
    by_suffix = {v: k for k, v in _KEY_SUFFIX.items()}
    secrets = (
        await db.execute(
            select(Secret).where(Secret.label.like(SECRET_PREFIX + "%"))
        )
    ).scalars().all()
    for sec in secrets:
        pid = by_suffix.get(sec.label[len(SECRET_PREFIX):])
        if not pid:
            continue
        try:
            key = decrypt(sec.ciphertext, sec.iv)
        except Exception as exc:  # noqa: BLE001
            log.warning("provider key decrypt failed (%s): %s",
                        sec.label, exc.__class__.__name__)
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


def available_providers(cfg: dict[str, dict]) -> list[dict]:
    return [
        {"id": pid, "label": PROVIDER_LABELS[pid],
         "available": is_provider_available(pid, cfg)}
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


async def _gemini_generate(
    *, api_key: str, model: str, system: str, prompt: str, timeout_s: int,
) -> str | None:
    # Auth via the x-goog-api-key header (current docs) — never the ?key=
    # query param, so the key never lands in a URL/log.
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.post(
            url,
            headers={"x-goog-api-key": api_key},
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


async def _atlas_chat(
    *, base_url: str, api_key: str, agent_id: str,
    system: str, prompt: str, timeout_s: int,
) -> str | None:
    """AI-ATLAS public agent API (hansol). Two steps: create a session, then
    post the message; the reply is ``response.message``. Atlas has no system
    role (the agent's behaviour is configured on the Atlas side), so the
    system text is prepended to the message. Auth = ``x-api-key`` header."""
    base = base_url.rstrip("/")
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        s = await client.post(
            f"{base}/agents/{agent_id}/sessions",
            headers=headers, json={"title": "Mnemos"},
        )
        s.raise_for_status()
        session_id = s.json().get("id")
        if not session_id:
            return None
        message = f"{system}\n\n{prompt}" if system else prompt
        r = await client.post(
            f"{base}/agents/{agent_id}/sessions/{session_id}/messages",
            headers=headers, json={"message": message},
        )
        r.raise_for_status()
        return (r.json().get("message") or "").strip() or None


async def _claude_api(
    *, api_key: str, model: str | None, system: str, prompt: str, timeout_s: int,
) -> str | None:
    """Direct Anthropic API — ~10-30s, far faster than the subprocess."""
    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.AsyncAnthropic(api_key=api_key)
        resp = await asyncio.wait_for(
            client.messages.create(
                model=model or "claude-sonnet-4-6",
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
    async def _drain(sink: list[str]) -> None:
        async for msg in query(prompt=prompt, options=opts):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        sink.append(block.text)

    # The Agent SDK's control handshake ("initialize") is flaky on a cold CLI
    # subprocess and raises *before* any tokens stream (observed on a large
    # repo: "Control request timeout: initialize" → 503, while the very next
    # call to the same prompt answers in ~90s). One retry warms it up. A real
    # content timeout (TimeoutError) is not retried — it would just wait again.
    for attempt in range(2):
        out: list[str] = []
        try:
            await asyncio.wait_for(_drain(out), timeout=timeout_s)
        except TimeoutError:
            log.warning("chat: subscription LLM timed out after %ds", timeout_s)
            return None
        except Exception as exc:  # noqa: BLE001
            log.warning("chat: %s: %s (init attempt %d/2)",
                        exc.__class__.__name__, exc, attempt + 1)
            continue
        return "\n".join(out).strip() or None
    return None


async def provider_chat(
    provider: str, cfg: dict[str, dict], *,
    system: str, prompt: str, timeout_s: int = 180,
) -> str | None:
    """Dispatch a one-shot answer to ``provider`` using the resolved
    config. Returns markdown, or ``None`` on any failure (logged)."""
    c = cfg.get(provider) or {}
    try:
        if provider == "openai":
            return await _openai_compatible(
                base_url=c.get("base_url") or "https://api.openai.com/v1",
                api_key=c.get("api_key") or "", model=c.get("model") or "gpt-4o",
                system=system, prompt=prompt, timeout_s=timeout_s,
            )
        if provider == "atlas":
            return await _atlas_chat(
                base_url=c.get("base_url") or "", api_key=c.get("api_key") or "",
                agent_id=c.get("agent_id") or "", system=system, prompt=prompt,
                timeout_s=timeout_s,
            )
        if provider == "gemini":
            return await _gemini_generate(
                api_key=c.get("api_key") or "",
                model=c.get("model") or "gemini-2.5-flash",
                system=system, prompt=prompt, timeout_s=timeout_s,
            )
        if provider == "claudecode":
            mode = c.get("mode") or "subscription"
            key = c.get("api_key")
            if mode == "api" and key:
                reply = await _claude_api(
                    api_key=key, model=c.get("model"),
                    system=system, prompt=prompt, timeout_s=timeout_s,
                )
                if reply is not None:
                    return reply
                # API failed → fall back to the subscription so chat still works.
                return await _claude_subscription(
                    system=system, prompt=prompt, timeout_s=timeout_s
                )
            # Subscription mode (default): use the local Claude Code login.
            reply = await _claude_subscription(
                system=system, prompt=prompt, timeout_s=timeout_s
            )
            if reply is not None:
                return reply
            # Subscription unavailable → use an API key if one is configured.
            if key:
                return await _claude_api(
                    api_key=key, model=c.get("model"),
                    system=system, prompt=prompt, timeout_s=timeout_s,
                )
            return None
        return None
    except httpx.HTTPStatusError as exc:
        log.warning("chat provider %s HTTP %s: %s", provider,
                    exc.response.status_code, exc.response.text[:200])
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat provider %s failed: %s: %s", provider,
                    exc.__class__.__name__, exc)
        return None


# ── Live connection test + model discovery ────────────────────────────
async def test_provider(
    provider: str, cfg: dict[str, dict], *, timeout_s: int = 15,
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
                r = await cl.get(base.rstrip("/") + "/models",
                                 headers={"Authorization": f"Bearer {key}"})
                r.raise_for_status()
                models = [m.get("id") for m in (r.json().get("data") or [])
                          if m.get("id")]
            return {"ok": True, "message": f"OK — {len(models)} models",
                    "models": sorted(models)}

        if provider == "atlas":
            base = c.get("base_url")
            agent = c.get("agent_id")
            if not (base and key and agent):
                return {"ok": False,
                        "message": "missing key / agent ID / base URL",
                        "models": []}
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
            return {"ok": True, "message": f"OK — {len(models)} models",
                    "models": sorted(m for m in models if m)}

        if provider == "claudecode":
            mode = c.get("mode") or "subscription"
            if mode == "subscription" or not key:
                if not is_agent_sdk_available():
                    return {"ok": False,
                            "message": "Claude Code subscription not detected "
                                       "(run Mnemos inside Claude Code)",
                            "models": []}
                # Real round-trip — proves the subscription actually answers,
                # not just that the SDK is importable.
                reply = await _claude_subscription(
                    system="You are a connection test. Reply with exactly: OK",
                    prompt="Reply with the single word OK.",
                    timeout_s=max(60, min(timeout_s, 120)),
                )
                if reply:
                    return {"ok": True,
                            "message": "OK — Claude 구독 응답 확인", "models": []}
                return {"ok": False,
                        "message": "구독 호출 실패 또는 시간 초과", "models": []}
            async with httpx.AsyncClient(timeout=timeout_s) as cl:
                r = await cl.get(
                    "https://api.anthropic.com/v1/models",
                    headers={"x-api-key": key,
                             "anthropic-version": "2023-06-01"},
                )
                r.raise_for_status()
                models = [m.get("id") for m in (r.json().get("data") or [])
                          if m.get("id")]
            return {"ok": True, "message": f"OK — {len(models)} models",
                    "models": sorted(models)}
    except httpx.HTTPStatusError as exc:
        return {"ok": False, "message": f"HTTP {exc.response.status_code}",
                "models": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": exc.__class__.__name__, "models": []}
    return {"ok": False, "message": "unknown provider", "models": []}
