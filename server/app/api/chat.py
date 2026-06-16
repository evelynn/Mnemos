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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import _entity_name, _short_path
from app.models.graph import Node
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
    "You are Mnemos, a senior software analyst embedded in a SOURCE-CODE "
    "ANALYSIS platform. Answer the operator's question about the ANALYSED "
    "project strictly from the provided analysis context: a project overview "
    "(the API endpoints and data tables), the most relevant symbols (with data "
    "access and call counts), and source-code excerpts. For HIGH-LEVEL "
    "questions — the project's purpose, architecture, or process/request "
    "flow — reason from the API endpoints and data tables in the overview "
    "(group the endpoints by area to describe what the system does and how a "
    "request flows through it). For SPECIFIC questions explain functions, "
    "propose SQL/query optimisations, discuss code changes with trade-offs, or "
    "draft reports, citing file:line. Never invent endpoints, tables, symbols, "
    "or paths that are not in the context; if something is genuinely missing, "
    "say so. Reply in the operator's language (Korean if they wrote Korean). "
    "Use concise, well-structured markdown."
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
    timeout_s: int = Field(default=180, ge=10, le=300)


def _resolve_source_root(given: str | None) -> str | None:
    """Request value wins; otherwise the deploy-wide default so the chat
    has code context without the operator pasting a path every time."""
    root = given or os.environ.get("MNEMOS_CHAT_SOURCE_ROOT") or None
    if root and Path(root).is_dir():
        return root
    return None


async def _project_overview(
    db: AsyncSession, project_id: uuid.UUID, *,
    max_contracts: int = 60, max_entities: int = 40,
) -> dict:
    """The project's shape from the analysis — counts, the API surface
    (HTTP endpoints / contracts) and the data tables. This is what lets
    the chat answer high-level questions ("what is this project's
    purpose / process flow?") that no single symbol match can."""
    counts = dict(
        (await db.execute(
            select(Node.kind, func.count()).where(
                Node.project_id == project_id, Node.valid_to.is_(None)
            ).group_by(Node.kind)
        )).all()
    )

    async def _names(kind: str, limit: int) -> list[str]:
        rows = (await db.execute(
            select(Node).where(
                Node.project_id == project_id, Node.valid_to.is_(None),
                Node.kind == kind,
            ).order_by(Node.id).limit(limit)
        )).scalars().all()
        return [str((r.data or {}).get("name") or r.id) for r in rows]

    return {
        "counts": counts,
        "contracts": await _names("Contract", max_contracts),
        "entities": await _names("DataEntity", max_entities),
    }


def _render_overview(ov: dict) -> str:
    c = ov.get("counts") or {}
    lines = [
        f"- Graph: {c.get('Symbol', 0)} code symbols, "
        f"{c.get('Contract', 0)} API endpoints, "
        f"{c.get('DataEntity', 0)} data tables, "
        f"{c.get('Component', 0)} components."
    ]
    if ov.get("contracts"):
        lines.append("- API endpoints (the system's external surface — read "
                     "these to infer the project's purpose and request flow):")
        lines.append("  " + "; ".join(ov["contracts"]))
    if ov.get("entities"):
        lines.append("- Data tables (the persisted domain model): "
                     + ", ".join(ov["entities"]))
    return "\n".join(lines)


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
        f"{convo}# Analysis context for the selected project\n{context}\n\n"
        f"## Operator's question\n{question}\n\n"
        "Answer using the analysis context above (project overview + relevant "
        "symbols + code). For a high-level question (purpose, architecture, "
        "process flow) ground the answer in the API endpoints and data tables; "
        "for a specific question use the relevant symbols and code, citing "
        "file:line. If something the question genuinely needs is absent from "
        "the context, say so — never invent endpoints, tables, or paths."
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


# Korean (and other non-ASCII) code concepts → the English keywords that
# actually appear in source. The lexical ranker only matches ASCII tokens,
# so ``"인증은 어떻게 동작해?"`` tokenises to nothing and never reaches the
# ``auth`` code. Mapping the concept words in is instant (no extra LLM call,
# which on the local subscription cold-starts for 60-150s) and matched by
# substring since Korean is agglutinative ("인증은"/"인증을" both contain
# "인증"). Extend freely — uncovered concepts just fall back to the lexical
# hits and the model answers what it can.
_KO_CONCEPTS: dict[str, list[str]] = {
    "인증": ["auth", "authentication", "login", "session", "token"],
    "로그인": ["login", "signin", "auth", "session"],
    "로그아웃": ["logout", "signout", "session"],
    "권한": ["permission", "role", "rbac", "authorize", "access", "guard"],
    "비밀번호": ["password", "credential", "hash", "auth"],
    "토큰": ["token", "jwt", "session", "auth"],
    "세션": ["session", "cookie", "auth"],
    "보안": ["security", "secure", "sanitize", "auth"],
    "암호화": ["encrypt", "crypto", "hash", "cipher"],
    "결제": ["payment", "pay", "billing", "checkout", "transaction"],
    "주문": ["order", "checkout", "cart"],
    "데이터베이스": ["database", "db", "query", "sql", "connection"],
    "디비": ["database", "db", "query", "sql"],
    "쿼리": ["query", "sql", "select", "search"],
    "캐시": ["cache", "redis"],
    "업로드": ["upload", "file", "multipart", "attachment"],
    "다운로드": ["download", "export", "file"],
    "검색": ["search", "query", "index", "find", "filter"],
    "알림": ["notification", "notify", "alert", "push"],
    "이메일": ["email", "mail", "smtp", "send"],
    "메일": ["email", "mail", "smtp"],
    "사용자": ["user", "account", "profile", "member"],
    "유저": ["user", "account", "profile"],
    "회원": ["user", "member", "account", "signup", "register"],
    "프로필": ["profile", "user", "account"],
    "에러": ["error", "exception", "handler"],
    "오류": ["error", "exception", "handler"],
    "예외": ["exception", "error", "catch"],
    "로그": ["log", "logging", "audit"],
    "감사": ["audit", "log"],
    "설정": ["config", "setting", "setup", "option"],
    "큐": ["queue", "job", "worker", "task"],
    "작업": ["job", "task", "worker", "queue"],
    "스케줄": ["schedule", "cron", "job", "timer"],
    "예약": ["schedule", "reservation", "booking"],
    "라우트": ["route", "router", "endpoint", "handler"],
    "엔드포인트": ["endpoint", "route", "api", "handler"],
    "미들웨어": ["middleware", "interceptor", "guard"],
    "모델": ["model", "schema", "entity"],
    "스키마": ["schema", "model", "validate"],
    "검증": ["validate", "validation", "verify", "check"],
    "유효성": ["validate", "validation", "schema"],
    "리포트": ["report", "summary", "export"],
    "보고서": ["report", "summary"],
    "그래프": ["graph", "node", "edge", "chart"],
    "차트": ["chart", "graph", "plot"],
    "파일": ["file", "upload", "storage"],
    "이미지": ["image", "photo", "upload", "thumbnail"],
    "프로세스": ["process", "flow", "pipeline", "workflow"],
    "흐름": ["flow", "process", "pipeline"],
    "최적화": ["optimize", "performance", "cache", "index"],
    "성능": ["performance", "optimize", "latency"],
}


def _expand_terms(question: str) -> list[str]:
    """English search keywords for the concept words in the question."""
    q = question.lower()
    kws: list[str] = []
    for ko, en in _KO_CONCEPTS.items():
        if ko in q:
            for k in en:
                if k not in kws:
                    kws.append(k)
    return kws[:10]


def _merge_hits(a: list[dict], b: list[dict], top_k: int) -> list[dict]:
    """Union two hit lists by symbol id, keeping the higher score."""
    by_id: dict[str, dict] = {}
    for h in (a or []) + (b or []):
        sid = str(h.get("symbol_id", ""))
        if not sid:
            continue
        cur = by_id.get(sid)
        if cur is None or float(h.get("score") or 0) > float(cur.get("score") or 0):
            by_id[sid] = h
    return sorted(
        by_id.values(), key=lambda h: float(h.get("score") or 0), reverse=True
    )[:top_k]


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
    # A Korean/concept question barely tokenises to English, so the lexical
    # ranker misses the relevant code. Map concept words to English keywords
    # and search again, merging by best score.
    expanded = _expand_terms(body.message)
    if expanded:
        extra = await search_symbols(
            db, project_id=project_id, query=" ".join(expanded), top_k=body.top_k
        )
        hits = _merge_hits(hits, extra, body.top_k)
    source_root = _resolve_source_root(body.source_root)
    overview = await _project_overview(db, project_id)
    ctx = await _build_context(db, project_id, hits, source_root)
    context_md = (
        "## Project overview\n" + _render_overview(overview)
        + "\n\n## Most relevant symbols for this question\n" + _render_context(ctx)
    )
    prompt = _build_prompt(body.history, context_md, body.message)

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
        details={"symbols": [c.get("name") for c in ctx], "code": bool(source_root),
                 "expanded": expanded},
    )

    return {
        "reply": reply,
        "context": [
            {"name": c.get("name"), "kind": c.get("kind"),
             "file": c.get("file"), "line": c.get("line")}
            for c in ctx
        ],
        "used_code": bool(source_root),
        "expanded": expanded,
    }
