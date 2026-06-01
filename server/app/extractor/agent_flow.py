"""PR-143 — cross-tier flow / process analysis via Claude Code.

Mnemos's deeper promise (spec §2: "boundaries are joined by contracts")
is to explain how a *process* runs end-to-end across tiers — frontend →
backend → database — not just to list symbols per file. An operator
asking "what happens when a user places an order?" wants the whole
trace: which request crosses each boundary, every field and flag value
in those signals, what each value MEANS, where it is formed, and which
rows it reads/writes.

This module hands the relevant source slices from every tier to the
operator's Claude Code subscription and asks for that structured trace.
The result is persisted as a level-4 Summary (``flow:<slug>``) so it
joins the knowledge graph and is queryable like any other summary.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from app.extractor.agent_sdk import _parse_json_response, is_agent_sdk_available

log = logging.getLogger(__name__)

FLOW_LEVEL = 4  # L1-L3 are symbol/module/component; L4 is a cross-tier flow.

_FLOW_SYSTEM = (
    "You are a senior systems analyst tracing one end-to-end process across "
    "a multi-tier system (frontend, backend, database). You receive source "
    "slices from each tier and return ONLY a JSON object — no prose, no "
    "markdown fences. Trace the named process across every boundary. For "
    "each signal that crosses a boundary (HTTP request/response, function "
    "call, SQL statement) enumerate its fields and flags, and for EACH flag "
    "or enumerated value give its concrete values and what each value MEANS "
    "and where it is set/derived. Be precise and grounded in the given "
    "source; do not invent fields that aren't there. Note anything you "
    "cannot determine in open_questions."
)


def _build_flow_prompt(entry: str, sources: list[dict[str, Any]]) -> str:
    parts = [
        f"Process to trace: {entry}\n",
        "Return a JSON object with this exact shape:\n",
        "{\n"
        '  "summary": "<=200 chars one-line of the whole flow",\n'
        '  "detailed": "step-by-step prose of the end-to-end flow",\n'
        '  "steps": [\n'
        '    {"order": <int>, "tier": "frontend|backend|database",\n'
        '     "component": "<file or symbol>", "action": "<what happens>",\n'
        '     "signal": {"kind": "http_request|http_response|function_call|sql",\n'
        '                "name": "<e.g. POST /api/orders>",\n'
        '                "fields": [{"name": "<field>", "type": "<type>",\n'
        '                            "values": ["<allowed values if enum/flag>"],\n'
        '                            "meaning": "<what it means / how formed>"}]}}\n'
        "  ],\n"
        '  "flags": [{"name": "<flag>", "type": "<type>",\n'
        '             "values": [{"value": <v>, "meaning": "<meaning>"}],\n'
        '             "set_at": "<tier/component>", "used_at": "<tier/component>"}],\n'
        '  "data_touched": [{"entity": "<table>", "op": "INSERT|UPDATE|SELECT|DELETE",\n'
        '                    "columns": ["<col>"]}],\n'
        '  "open_questions": ["<unknowns>"]\n'
        "}\n\n"
        "Source slices by tier:\n",
    ]
    for s in sources:
        parts.append(
            f"\n----- tier={s.get('tier','?')} file={s.get('label','?')} "
            f"({s.get('language','?')}) -----\n"
            "````\n"
            f"{s.get('code','')}\n"
            "````\n"
        )
    return "".join(parts)


def _normalise(parsed: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model output into the stored shape; tolerate missing keys."""
    return {
        "summary": str(parsed.get("summary", ""))[:1000],
        "detailed": str(parsed.get("detailed", "")),
        "steps": parsed.get("steps") if isinstance(parsed.get("steps"), list) else [],
        "flags": parsed.get("flags") if isinstance(parsed.get("flags"), list) else [],
        "data_touched": parsed.get("data_touched")
        if isinstance(parsed.get("data_touched"), list)
        else [],
        "open_questions": parsed.get("open_questions")
        if isinstance(parsed.get("open_questions"), list)
        else [],
    }


async def analyze_flow_via_agent_sdk(
    *,
    entry: str,
    sources: list[dict[str, Any]],
    model: str = "claude-sonnet-4-6",
    timeout_s: int = 300,
    max_total_chars: int = 28_000,
) -> dict[str, Any] | None:
    """Trace one cross-tier process via the Claude Code subscription.
    Returns the normalised flow dict, or None on any failure (never raises)."""
    if not is_agent_sdk_available() or not sources:
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

    # Budget the combined source so a huge slice can't blow the context.
    budget = max_total_chars
    trimmed: list[dict[str, Any]] = []
    for s in sources:
        code = s.get("code", "")
        if budget <= 0:
            break
        if len(code) > budget:
            code = code[:budget]
        trimmed.append({**s, "code": code})
        budget -= len(code)

    prompt = _build_flow_prompt(entry, trimmed)
    opts = ClaudeAgentOptions(
        allowed_tools=[],
        disallowed_tools=["Bash", "Edit", "Write", "Read", "Task"],
        system_prompt=_FLOW_SYSTEM,
        max_turns=1,
        permission_mode="default",
        cwd=os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
        model=model if model and "claude" in model else None,
    )

    collected: list[str] = []
    is_error = False
    try:
        import asyncio

        async def _drain() -> bool:
            async for msg in query(prompt=prompt, options=opts):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            collected.append(block.text)
                elif isinstance(msg, ResultMessage):
                    return msg.is_error
            return False

        is_error = await asyncio.wait_for(_drain(), timeout=timeout_s)
    except asyncio.TimeoutError:
        log.warning("agent_flow: %r timed out after %ds", entry, timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_flow: %r: %s: %s", entry, exc.__class__.__name__, exc)
        return None

    if is_error or not collected:
        return None
    parsed = _parse_json_response("\n".join(collected))
    if parsed is None:
        return None
    return _normalise(parsed)
