"""PR-174 — conversational AI over the analysis.

The operator asked for a chat that can answer anything about an analysed
project from the analysis itself: what a function does, how to optimise a
SQL/query, how to approach a change, how a process flows, and to draft
short reports. ``/ask`` (PR-149) only locates a symbol; this composes a
real answer with an LLM.

``POST /projects/{id}/chat`` retrieves the most relevant symbols for the
message (the same lexical ranker the search uses), enriches them with
graph facts (data access, call counts, signature) and a slice of the
actual source code, then hands that context plus the conversation history
to Claude via the local subscription (``claude_agent_sdk``). The reply is
free-form markdown. With no LLM available it returns 503 — the lexical
``/ask`` stays the offline fallback.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import _entity_name, _short_path
from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.extractor.agent_sdk import is_agent_sdk_available
from app.mcp.queries import get_data_access, get_symbol, search_symbols

log = logging.getLogger("mnemos.chat")

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["chat"],
    dependencies=[Depends(require_project_org())],
)

_SYSTEM = (
    "You are Mnemos, a senior software analyst embedded in a code-knowledge "
    "platform. Answer the operator's question about the ANALYSED project using "
    "the provided analysis context (symbols, data access, call graph) and the "
    "source-code excerpts. You can: explain what a function does, propose SQL / "
    "query optimisations, discuss how to modify code with trade-offs, trace a "
    "process across layers, and draft short reports. Cite file:line for "
    "concrete claims. If the context is insufficient to answer, say what is "
    "missing rather than inventing — never fabricate symbols, tables, or file "
    "paths that are not in the context. Reply in the operator's language "
    "(Korean if they wrote Korean). Use concise, well-structured markdown."
)


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=8000)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)
    source_root: str | None = Field(
        default=None, description="Repo root; enables source-code context"
    )
    top_k: int = Field(default=6, ge=1, le=12)
    timeout_s: int = Field(default=120, ge=10, le=240)


def _resolve_source_root(given: str | None) -> str | None:
    """Request value wins; otherwise the deploy-wide default so the chat
    has code context without the operator pasting a path every time."""
    root = given or os.environ.get("MNEMOS_CHAT_SOURCE_ROOT") or None
    if root and Path(root).is_dir():
        return root
    return None


def _read_code(source_root: str, file: str | None, line: int | None,
               max_chars: int = 1800) -> str | None:
    """A slice of the symbol's source — from a little before its
    definition line through the body — joined under ``source_root``."""
    if not source_root or not file:
        return None
    rel = _short_path(file) or file
    path = Path(source_root) / rel
    if not path.is_file():
        # ``_short_path`` may have trimmed a leading segment; try the raw
        # relative file as given.
        path = Path(source_root) / file.replace("\\", "/")
        if not path.is_file():
            return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    start = max(0, (line or 1) - 3)
    snippet = "\n".join(lines[start:start + 70])
    return snippet[:max_chars]


async def _build_context(
    db: AsyncSession, project_id: uuid.UUID, hits: list[dict],
    source_root: str | None, total_cap: int = 9000,
) -> list[dict]:
    """Per-hit graph facts + a code slice, capped so the prompt stays
    bounded on a huge project."""
    out: list[dict] = []
    used = 0
    seen_files: set[str] = set()
    for h in hits:
        sid = str(h.get("symbol_id", ""))
        if sid.startswith(("data:", "contract:")):
            continue
        sym = await get_symbol(db, project_id=project_id, symbol_id=sid)
        if not sym:
            continue
        data = ((sym or {}).get("symbol") or {}).get("data") or {}
        loc = data.get("location") or {}
        file = _short_path(loc.get("file"))
        line = loc.get("line")
        da = await get_data_access(db, project_id=project_id, symbol_id=sid)
        nb = (sym or {}).get("neighbors") or {}
        entry = {
            "name": h.get("name") or data.get("name"),
            "kind": data.get("kind") or h.get("kind"),
            "file": file,
            "line": line,
            "signature": data.get("signature"),
            "reads": [_entity_name(w.get("entity_id")) for w in da.get("reads", [])],
            "writes": [_entity_name(w.get("entity_id")) for w in da.get("writes", [])],
            "callers": int(nb.get("callers_count") or 0),
            "callees": int(nb.get("callees_count") or 0),
        }
        # Read each file's code once; cheaper and avoids repeating bodies.
        code = None
        if source_root and loc.get("file") and loc["file"] not in seen_files:
            code = _read_code(source_root, loc.get("file"), line)
            if code:
                seen_files.add(loc["file"])
                entry["code"] = code
                used += len(code)
        out.append(entry)
        if used >= total_cap:
            break
    return out


def _render_context(ctx: list[dict]) -> str:
    parts = []
    for c in ctx:
        loc = f"{c['file']}:{c['line']}" if c.get("file") else "(location unknown)"
        head = f"### {c.get('name')} ({c.get('kind') or 'symbol'}) — {loc}"
        lines = [head]
        if c.get("signature"):
            lines.append(f"signature: `{c['signature']}`")
        if c.get("writes"):
            lines.append("writes: " + ", ".join(c["writes"]))
        if c.get("reads"):
            lines.append("reads: " + ", ".join(c["reads"]))
        lines.append(f"called from {c['callers']} place(s); calls {c['callees']} symbol(s).")
        if c.get("code"):
            lines.append("```\n" + c["code"] + "\n```")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) if parts else "(no matching symbols found)"


def _build_prompt(history: list[ChatMessage], context: str, question: str) -> str:
    convo = ""
    if history:
        convo = "## Conversation so far\n" + "\n".join(
            f"{m.role}: {m.content}" for m in history
        ) + "\n\n"
    return (
        f"{convo}## Analysis context (most relevant symbols)\n{context}\n\n"
        f"## Operator's question\n{question}\n\n"
        "Answer using the context above. Cite file:line. If something the "
        "question needs is not in the context, say so explicitly."
    )


async def chat_via_agent_sdk(
    *, system: str, prompt: str, timeout_s: int = 120
) -> str | None:
    """One-shot conversational Claude call via the local subscription.
    Returns the markdown reply, or None on any failure (never raises)."""
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
        log.warning("chat: agent SDK timed out after %ds", timeout_s)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("chat: %s: %s", exc.__class__.__name__, exc)
        return None
    text = "\n".join(out).strip()
    return text or None


@router.post("/chat", dependencies=[Depends(require_operator)])
async def chat(
    project_id: uuid.UUID,
    body: ChatRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    if not is_agent_sdk_available():
        raise HTTPException(status_code=503, detail="llm_unavailable")

    hits = await search_symbols(
        db, project_id=project_id, query=body.message, top_k=body.top_k
    )
    source_root = _resolve_source_root(body.source_root)
    ctx = await _build_context(db, project_id, hits, source_root)
    prompt = _build_prompt(body.history, _render_context(ctx), body.message)

    reply = await chat_via_agent_sdk(
        system=_SYSTEM, prompt=prompt, timeout_s=body.timeout_s
    )
    if reply is None:
        raise HTTPException(status_code=503, detail="llm_call_failed")

    await audit_record(
        actor=f"user:{user.id}",
        action="qa.chat",
        target=body.message[:120],
        project_id=project_id,
        details={"symbols": [c.get("name") for c in ctx], "code": bool(source_root)},
    )

    return {
        "reply": reply,
        "context": [
            {"name": c.get("name"), "kind": c.get("kind"),
             "file": c.get("file"), "line": c.get("line")}
            for c in ctx
        ],
        "used_code": bool(source_root),
    }
