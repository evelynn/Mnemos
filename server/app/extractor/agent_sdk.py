"""PR-125 — Claude Agent SDK backend for L1~L3 extractor.

User point made: Mnemos's "L3 LLM summary unverified" excuse was
wrong. Mnemos runs INSIDE Claude Code, which has a working OAuth
subscription. The Claude Agent SDK exposes that subscription as
a one-shot ``query()`` API — no ANTHROPIC_API_KEY needed.

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

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)


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
    max_evidence_chars: int = 6000,
    timeout_s: int = 60,
) -> dict[str, Any] | None:
    """One-shot Claude call via the local Claude Code subscription.

    Returns the parsed JSON response dict, or None when:
    - the SDK isn't installed
    - the call times out
    - the response isn't parseable JSON

    The caller (Extractor) decides whether to fall back to stub.
    Never raises — failures degrade silently into None so the
    pipeline stays alive.
    """
    if not is_agent_sdk_available():
        return None
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError:
        return None

    prompt = _build_prompt(level, target_id, evidence, max_evidence_chars)
    sys_p = system_prompt or _DEFAULT_SYSTEM
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=sys_p,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
        model=model if model and "claude" in model else None,
    )

    collected_text: list[str] = []
    is_error = False
    try:
        import asyncio
        async def _drain():
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected_text.append(block.text)
                elif isinstance(msg, ResultMessage):
                    nonlocal_marker = msg.is_error
                    return nonlocal_marker
            return False
        is_error = await asyncio.wait_for(_drain(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("agent_sdk: timed out after %ds", timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_sdk: %s: %s", exc.__class__.__name__, exc)
        return None

    if is_error or not collected_text:
        return None

    text = "\n".join(collected_text)
    return _parse_json_response(text)


_DEFAULT_SYSTEM = (
    "You are Mnemos's hierarchical summariser. Produce STRICT JSON "
    "matching this schema and nothing else (no markdown fences, no "
    "explanation): {\"summary\": string, \"detailed\": string, "
    "\"claims\": [{\"claim\": string, \"evidence\": [{\"kind\": "
    "\"node\"|\"edge\", \"node_id\"|\"edge_id\": string, "
    "\"certainty\": \"verified\"|\"asserted\"|\"inferred\"}]}], "
    "\"open_questions\": [string]}. Never invent evidence IDs — "
    "only cite ids that appear in the input evidence."
)


def _build_prompt(level: int, target_id: str, evidence: list[dict[str, Any]],
                  max_chars: int) -> str:
    ev_text = json.dumps(evidence, default=str)[:max_chars]
    return (
        f"L{level} summarisation target: {target_id}\n"
        f"Graph evidence (truncated to {max_chars} chars):\n"
        f"{ev_text}\n\n"
        "Return strict JSON only."
    )


def _parse_json_response(text: str) -> dict[str, Any] | None:
    """Parse the model's reply into our schema dict, tolerant of
    common Claude failure modes:
    - Markdown fenced code block (```json … ```)
    - Leading or trailing prose
    - Trailing comma / minor JSON errors → None
    """
    t = text.strip()
    # Strip markdown fences if present.
    if t.startswith("```"):
        lines = t.splitlines()
        # drop the first ``` line and any trailing ```
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    # If there's leading prose, find the first { and try from there.
    if not t.startswith("{"):
        i = t.find("{")
        if i < 0:
            return None
        t = t[i:]
    # Trim trailing junk after the last }.
    last = t.rfind("}")
    if last >= 0:
        t = t[:last + 1]
    try:
        parsed = json.loads(t)
        if not isinstance(parsed, dict):
            return None
        return parsed
    except json.JSONDecodeError:
        return None
