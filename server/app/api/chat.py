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
to the selected bounded provider route. Claude Chat uses only the direct
Anthropic Messages API. The reply is free-form markdown. With no LLM
available it returns 503 — the lexical
``/ask`` stays the offline fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.ask import _entity_name, _short_path
from app.api.graph_guard import require_readable_current_graph
from app.api.llm_providers import (
    ChatProviderAttemptState,
    ChatSemanticCodec,
    any_provider_available,
    available_providers,
    chat_answer_semantic_codec,
    default_provider,
    is_provider_available,
    output_token_limit_capability,
    provider_chat,
    resolve_config,
)
import app.db as app_db
from app.mcp.file_read import read_project_file
from app.models.graph import Node
from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.extractor.cost import (
    BudgetExceeded,
    LLMRunBudget,
    RunBudgetExceeded,
)
from app.llm.contracts import LLMPurpose, ResultStatus
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptOutcome,
    AttemptReplayResult,
    AttemptStartMetadata,
    AttemptTicket,
    BudgetScopeKind,
    LLMLifecycleError,
    SemanticCandidate,
    SemanticCandidateReplay,
    SemanticCandidateReplayRequest,
    begin_attempt,
    classify_attempt_result,
    finalize_attempt,
    fingerprint_input,
    load_attempt_replay,
    load_terminal_semantic_candidate,
    use_attempt_callbacks,
)
from app.mcp.queries import get_data_access, get_symbol, search_symbols

log = logging.getLogger("mnemos.chat")

# Chat is an on-demand consumer of the graph, not permission to put a whole
# conversation or repository into one provider request.  These are character
# ceilings because every provider adapter receives text.  The final ceiling is
# checked against the exact prompt string; the two section budgets select only
# complete history messages and complete graph-context records.
CHAT_HISTORY_MAX_CHARS = 10_000
CHAT_CONTEXT_MAX_CHARS = 8_000
CHAT_OVERVIEW_MAX_CHARS = 2_000
CHAT_PROMPT_MAX_CHARS = 24_000
CHAT_CODE_TOTAL_MAX_CHARS = 6_000
CHAT_CODE_ITEM_MAX_CHARS = 1_600
CHAT_REWRITE_MAX_OUTPUT_TOKENS = 128
CHAT_ANSWER_MAX_OUTPUT_TOKENS = 1_200
CHAT_MAX_PROVIDER_ATTEMPTS = 4
CHAT_MAX_RESERVED_INPUT_TOKENS = 120_000
CHAT_MAX_RESERVED_OUTPUT_TOKENS = 4_800

REWRITE_MIN_TERMS = 3
REWRITE_MAX_TERMS = 8
REWRITE_MAX_TERM_CHARS = 64
REWRITE_OVERVIEW_MAX_CHARS = 1_000
REWRITE_PROMPT_MAX_CHARS = 6_000

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["chat"],
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)

# Provider listing is project-independent (it reports which AIs are
# configured), so it lives on its own un-prefixed router.
meta_router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@meta_router.get("/providers")
async def list_chat_providers(
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """The selectable AI providers and whether each is configured — the
    Chat tab populates its selector from this."""
    cfg = await resolve_config(db)
    return {"providers": available_providers(cfg), "default": default_provider(cfg)}


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
    provider: str | None = Field(
        default=None,
        max_length=32,
        description="AI provider id (openai/gemini/claudecode/atlas); None = first configured",
    )
    source_root: str | None = Field(
        default=None,
        description=(
            "Deprecated and ignored. Source is read only from the immutable "
            "snapshot bound to the completed analysis run."
        ),
    )
    top_k: int = Field(default=6, ge=1, le=12)
    timeout_s: int = Field(default=180, ge=10, le=300)


def _chat_operation_id(
    project_id: uuid.UUID,
    purpose: LLMPurpose,
) -> uuid.UUID:
    """Stable project/purpose namespace bound to exact input by the adapter."""

    return uuid.uuid5(project_id, purpose.value)


def _chat_attempt_callbacks(
    *,
    project_id: uuid.UUID,
    purpose: LLMPurpose,
    run_budget: LLMRunBudget,
) -> AttemptCallbacks:
    """Create short-lived accounting callbacks for one Chat provider call.

    The request's graph-reading session is never held across a provider
    dispatch. Each accounting transition gets a fresh runtime-resolved
    ``SessionLocal`` so STARTED is committed and visible before network I/O.
    """

    async def _start(metadata: AttemptStartMetadata) -> AttemptTicket:
        async with app_db.SessionLocal() as accounting_db:
            return await begin_attempt(
                accounting_db,
                project_id=project_id,
                analysis_run_id=None,
                purpose=purpose,
                target_id=f"chat:{purpose.value.removeprefix('chat_')}",
                level=0,
                run_budget=run_budget,
                scope_kind=BudgetScopeKind.REQUEST,
                metadata=metadata,
                pre_reserved=None,
                require_atomic_dollar_reservation=True,
            )

    async def _finish(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
    ) -> None:
        async with app_db.SessionLocal() as accounting_db:
            await finalize_attempt(
                accounting_db,
                ticket=ticket,
                outcome=outcome,
            )

    async def _finish_candidate(
        ticket: AttemptTicket,
        outcome: AttemptOutcome,
        candidate: SemanticCandidate,
    ) -> None:
        async with app_db.SessionLocal() as accounting_db:
            await finalize_attempt(
                accounting_db,
                ticket=ticket,
                outcome=outcome,
                candidate=candidate,
            )

    async def _replay_candidate(
        request: SemanticCandidateReplayRequest,
    ) -> SemanticCandidateReplay:
        async with app_db.SessionLocal() as accounting_db:
            return await load_terminal_semantic_candidate(
                accounting_db,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=purpose,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=request.normalizer,
            )

    async def _replay_attempt(
        request: SemanticCandidateReplayRequest,
    ) -> AttemptReplayResult:
        async with app_db.SessionLocal() as accounting_db:
            return await load_attempt_replay(
                accounting_db,
                operation_id=request.operation_id,
                attempt_no=request.attempt_no,
                project_id=project_id,
                purpose=purpose,
                input_fingerprint=request.input_fingerprint,
                contract_name=request.contract_name,
                binding_fingerprint=request.binding_fingerprint,
                normalizer=request.normalizer,
            )

    return AttemptCallbacks(
        start=_start,
        finish=_finish,
        finish_candidate=_finish_candidate,
        replay_candidate=_replay_candidate,
        replay_attempt=_replay_attempt,
        mark_provider_dispatch=lambda: None,
    )


async def _classify_chat_candidate(
    attempt_id: uuid.UUID,
    *,
    result_status: ResultStatus,
    failure_code: str | None = None,
) -> None:
    async with app_db.SessionLocal() as accounting_db:
        await classify_attempt_result(
            accounting_db,
            attempt_id=attempt_id,
            result_status=result_status,
            failure_code=failure_code,
        )


async def _project_overview(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    max_contracts: int = 60,
    max_entities: int = 40,
) -> dict:
    """The project's shape from the analysis — counts, the API surface
    (HTTP endpoints / contracts) and the data tables. This is what lets
    the chat answer high-level questions ("what is this project's
    purpose / process flow?") that no single symbol match can."""
    counts = dict(
        (
            await db.execute(
                select(Node.kind, func.count())
                .where(Node.project_id == project_id, Node.valid_to.is_(None))
                .group_by(Node.kind)
            )
        ).all()
    )

    async def _names(kind: str, limit: int) -> list[str]:
        rows = (
            (
                await db.execute(
                    select(Node)
                    .where(
                        Node.project_id == project_id,
                        Node.valid_to.is_(None),
                        Node.kind == kind,
                    )
                    .order_by(Node.id)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [str((r.data or {}).get("name") or r.id) for r in rows]

    return {
        "counts": counts,
        "contracts": await _names("Contract", max_contracts),
        "entities": await _names("DataEntity", max_entities),
    }


def _render_overview(ov: dict) -> str:
    return "\n".join(_overview_items(ov))


def _overview_items(ov: dict) -> list[str]:
    """Render independently droppable overview facts.

    Endpoint and entity names are graph data.  Keeping each as its own item
    lets the prompt packer omit an oversized/low-priority tail without cutting
    an identifier in half.
    """

    c = ov.get("counts") or {}
    items = [
        f"- Graph: {c.get('Symbol', 0)} code symbols, "
        f"{c.get('Contract', 0)} API endpoints, "
        f"{c.get('DataEntity', 0)} data tables, "
        f"{c.get('Component', 0)} components."
    ]
    items.extend(f"- API endpoint: {name}" for name in ov.get("contracts") or [])
    items.extend(f"- Data table: {name}" for name in ov.get("entities") or [])
    return items


def _pack_complete_text_items(
    items: list[str], *, max_chars: int, separator: str = "\n"
) -> tuple[list[str], dict]:
    """Keep complete strings within an exact character budget."""

    selected: list[str] = []
    used = 0
    for item in items:
        addition = len(item) + (len(separator) if selected else 0)
        if used + addition > max(0, max_chars):
            continue
        selected.append(item)
        used += addition
    return selected, {
        "provided_items": len(items),
        "included_items": len(selected),
        "omitted_items": len(items) - len(selected),
        "included_chars": used,
        "max_chars": max(0, max_chars),
        "truncated": len(selected) != len(items),
    }


def _complete_line_excerpt(text: str, max_chars: int) -> tuple[str | None, bool]:
    """Select only whole source lines; never slice through a giant line."""

    if max_chars <= 0:
        return None, bool(text)
    selected: list[str] = []
    used = 0
    lines = text.splitlines(keepends=True)
    for line in lines:
        if used + len(line) > max_chars:
            break
        selected.append(line)
        used += len(line)
    excerpt = "".join(selected).rstrip("\r\n")
    return (excerpt or None), len(selected) != len(lines)


async def _build_context(
    db: AsyncSession,
    project_id: uuid.UUID,
    hits: list[dict],
    total_cap: int = CHAT_CODE_TOTAL_MAX_CHARS,
) -> list[dict]:
    """Build graph records with whole-line source excerpts.

    This function bounds source reads.  ``_pack_chat_context`` separately
    enforces the exact final context budget across complete records.
    """
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
        if used < total_cap and loc.get("file") and loc["file"] not in seen_files:
            window = await read_project_file(
                db,
                project_id=project_id,
                file_path=loc["file"],
                start_line=max(1, int(line or 1) - 2),
                end_line=max(1, int(line or 1) - 2) + 69,
            )
            seen_files.add(loc["file"])
            source = window.get("content")
            if isinstance(source, str) and source:
                per_item_cap = min(
                    CHAT_CODE_ITEM_MAX_CHARS,
                    max(0, total_cap - used),
                )
                code, truncated = _complete_line_excerpt(source, per_item_cap)
                if code:
                    entry["code"] = code
                    used += len(code)
                entry["_code_source_chars"] = len(source)
                entry["_code_truncated"] = truncated or code is None
        out.append(entry)
    return out


def _render_context_item(c: dict) -> str:
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
    return "\n".join(lines)


def _render_context(ctx: list[dict]) -> str:
    parts = [_render_context_item(item) for item in ctx]
    return "\n\n".join(parts) if parts else "(no matching symbols found)"


def _pack_chat_context(
    overview: dict,
    context: list[dict],
    *,
    max_chars: int = CHAT_CONTEXT_MAX_CHARS,
) -> tuple[str, list[dict], dict]:
    """Pack complete overview facts and symbol records into one context."""

    overview_heading = "## Project overview\n"
    symbol_heading = "\n\n## Most relevant symbols for this question\n"
    fixed_chars = len(overview_heading) + len(symbol_heading)
    overview_budget = min(
        CHAT_OVERVIEW_MAX_CHARS,
        max(0, max_chars - fixed_chars),
    )
    overview_items, overview_meta = _pack_complete_text_items(
        _overview_items(overview), max_chars=overview_budget
    )
    overview_text = "\n".join(overview_items)
    prefix = overview_heading + overview_text + symbol_heading

    included: list[dict] = []
    rendered: list[str] = []
    code_omitted = 0
    symbol_items_skipped_for_budget = 0
    provided_with_code = sum(bool(item.get("code")) for item in context)
    for item in context:
        candidate = dict(item)
        block = _render_context_item(candidate)
        addition = len(block) + (2 if rendered else 0)
        if len(prefix) + sum(len(value) for value in rendered) + 2 * max(
            0, len(rendered) - 1
        ) + addition > max_chars and candidate.get("code"):
            candidate.pop("code", None)
            block = _render_context_item(candidate)
            addition = len(block) + (2 if rendered else 0)
            code_omitted += 1
        rendered_chars = sum(len(value) for value in rendered) + 2 * max(0, len(rendered) - 1)
        if len(prefix) + rendered_chars + addition > max_chars:
            # A very large signature/path/metadata record must not prevent a
            # later, smaller symbol from using the remaining context budget.
            # Skip the complete record; never slice machine-grounding facts.
            symbol_items_skipped_for_budget += 1
            continue
        included.append(candidate)
        rendered.append(block)

    if not rendered:
        empty = "(no matching symbols found)"
        if len(prefix) + len(empty) <= max_chars:
            rendered.append(empty)

    text = prefix + "\n\n".join(rendered)
    metadata = {
        "max_chars": max_chars,
        "actual_chars": len(text),
        "provided_symbol_items": len(context),
        "included_symbol_items": len(included),
        "omitted_symbol_items": len(context) - len(included),
        "symbol_items_skipped_for_budget": symbol_items_skipped_for_budget,
        "provided_code_items": provided_with_code,
        "included_code_items": sum(bool(item.get("code")) for item in included),
        "code_items_omitted_for_budget": code_omitted,
        "source_code_items_truncated": sum(bool(item.get("_code_truncated")) for item in included),
        "overview": overview_meta,
    }
    metadata["truncated"] = bool(
        metadata["omitted_symbol_items"]
        or metadata["code_items_omitted_for_budget"]
        or metadata["source_code_items_truncated"]
        or overview_meta["truncated"]
    )
    if len(text) > max_chars:
        raise AssertionError("chat context exceeded complete-item budget")
    return text, included, metadata


def _pack_history(history: list[ChatMessage], *, max_chars: int) -> tuple[list[str], dict]:
    """Keep the most recent complete messages, preserving their order."""

    rendered = [f"{message.role}: {message.content}" for message in history]
    selected_reversed: list[str] = []
    used = 0
    for item in reversed(rendered):
        addition = len(item) + (1 if selected_reversed else 0)
        if used + addition > max(0, max_chars):
            continue
        selected_reversed.append(item)
        used += addition
    selected = list(reversed(selected_reversed))
    return selected, {
        "provided_items": len(rendered),
        "included_items": len(selected),
        "omitted_items": len(rendered) - len(selected),
        "original_chars": sum(len(item) for item in rendered) + max(0, len(rendered) - 1),
        "included_chars": used,
        "max_chars": max(0, max_chars),
        "truncated": len(selected) != len(rendered),
    }


def _build_bounded_prompt(
    history: list[ChatMessage],
    context: str,
    question: str,
    *,
    max_chars: int = CHAT_PROMPT_MAX_CHARS,
    system: str = "",
) -> tuple[str, dict]:
    tail = (
        f"# Analysis context for the selected project\n{context}\n\n"
        f"## Operator's question\n{question}\n\n"
        "Answer using the analysis context above (project overview + relevant "
        "symbols + code). For a high-level question (purpose, architecture, "
        "process flow) ground the answer in the API endpoints and data tables; "
        "for a specific question use the relevant symbols and code, citing "
        "file:line. If something the question genuinely needs is absent from "
        "the context, say so — never invent endpoints, tables, or paths."
    )
    prompt_ceiling = max(0, max_chars - len(system))
    if len(tail) > prompt_ceiling:
        raise ValueError("chat_prompt_fixed_sections_exceed_budget")

    history_heading = "## Conversation so far\n"
    history_suffix = "\n\n"
    available = min(
        CHAT_HISTORY_MAX_CHARS,
        max(
            0,
            prompt_ceiling - len(tail) - len(history_heading) - len(history_suffix),
        ),
    )
    history_items, history_meta = _pack_history(history, max_chars=available)
    convo = history_heading + "\n".join(history_items) + history_suffix if history_items else ""
    prompt = convo + tail
    if len(system) + len(prompt) > max_chars:
        raise AssertionError("chat prompt exceeded complete-item budget")
    return prompt, {
        "max_chars": max_chars,
        "actual_chars": len(prompt),
        "system_chars": len(system),
        "provider_input_chars": len(system) + len(prompt),
        "history": history_meta,
        "truncated": history_meta["truncated"],
    }


def _build_prompt(history: list[ChatMessage], context: str, question: str) -> str:
    """Compatibility wrapper for callers that only need the prompt text."""

    return _build_bounded_prompt(history, context, question)[0]


# Korean (and other non-ASCII) code concepts → the English keywords that
# actually appear in source. The lexical ranker only matches ASCII tokens,
# so ``"인증은 어떻게 동작해?"`` tokenises to nothing and never reaches the
# ``auth`` code. Mapping the concept words in is instant (no extra provider
# call) and matched by
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
    return sorted(by_id.values(), key=lambda h: float(h.get("score") or 0), reverse=True)[:top_k]


_WEAK_SCORE = 10.0


def _has_korean(text: str) -> bool:
    return any("가" <= c <= "힣" for c in text or "")


def _weak_recall(
    question: str,
    hits: list[dict],
    *,
    static_expansion_hits: list[dict] | None = None,
) -> bool:
    """Should we spend an LLM call to rewrite the query into search terms?

    An unmapped Korean question still needs translation even when an unrelated
    English homonym scored highly.  A mapped Korean concept, however, first gets
    a free deterministic expansion; a strong hit from *that expansion* makes a
    second provider call pure waste.  English questions use the merged lexical
    score directly.
    """
    top = float(hits[0].get("score") or 0) if hits else 0.0
    if _has_korean(question):
        if static_expansion_hits is None:
            return True
        expanded_top = (
            float(static_expansion_hits[0].get("score") or 0) if static_expansion_hits else 0.0
        )
        return expanded_top < _WEAK_SCORE
    return top < _WEAK_SCORE


class RewriteTermsContractError(ValueError):
    """Safe diagnostic for the provider → search-term JSON boundary."""

    result_status = ResultStatus.SCHEMA_REJECTED


class RewriteTermsParseError(RewriteTermsContractError):
    """The provider text did not contain the supported JSON dialect."""

    result_status = ResultStatus.PARSE_REJECTED


def _actual_shape(value) -> str:  # noqa: ANN001
    if isinstance(value, str):
        return f"str(len={len(value)})"
    if isinstance(value, (list, dict, tuple)):
        return f"{type(value).__name__}(len={len(value)})"
    return type(value).__name__


def _normalize_rewrite_terms(payload) -> list[str]:  # noqa: ANN001
    """Return the sole bounded rewrite representation or reject it."""

    if not isinstance(payload, list):
        raise RewriteTermsContractError(f"$: expected JSON array, got {_actual_shape(payload)}")
    if not REWRITE_MIN_TERMS <= len(payload) <= REWRITE_MAX_TERMS:
        raise RewriteTermsContractError(
            f"$: expected {REWRITE_MIN_TERMS}..{REWRITE_MAX_TERMS} terms, "
            f"got list(len={len(payload)})"
        )
    canonical: list[str] = []
    seen: set[str] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, str):
            raise RewriteTermsContractError(
                f"$[{index}]: expected string up to "
                f"{REWRITE_MAX_TERM_CHARS} chars, got {_actual_shape(raw)}"
            )
        term = " ".join(raw.split())
        if not term or len(term) > REWRITE_MAX_TERM_CHARS:
            raise RewriteTermsContractError(
                f"$[{index}]: expected non-empty string up to "
                f"{REWRITE_MAX_TERM_CHARS} chars, got str(len={len(term)})"
            )
        marker = term.casefold()
        if marker in seen:
            raise RewriteTermsContractError(
                f"$[{index}]: duplicate normalized term; emit unique terms"
            )
        seen.add(marker)
        canonical.append(term)
    return canonical


def _parse_rewrite_terms(text: str) -> list[str]:
    """Parse exact JSON or the one documented ``json`` fence dialect."""

    if not isinstance(text, str):
        raise RewriteTermsContractError(f"$: expected provider text, got {_actual_shape(text)}")
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        if not lines or lines[0].strip().lower() not in {"```", "```json"}:
            raise RewriteTermsParseError("$: unsupported fenced payload")
        if len(lines) < 3 or lines[-1].strip() != "```":
            raise RewriteTermsParseError("$: unterminated JSON fence")
        candidate = "\n".join(lines[1:-1]).strip()
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RewriteTermsParseError(
            f"$@char{exc.pos}: invalid JSON; return one JSON array only"
        ) from exc
    return _normalize_rewrite_terms(payload)


_CHAT_REWRITE_CONTRACT = "mnemos.chat.rewrite.v1"


def _normalize_chat_rewrite_candidate(
    payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Validate the encrypted/replayed rewrite candidate without raw text."""

    if not isinstance(payload, Mapping) or set(payload) != {"contract", "terms"}:
        raise RewriteTermsContractError(
            "$: expected the exact chat rewrite candidate object"
        )
    if payload.get("contract") != _CHAT_REWRITE_CONTRACT:
        raise RewriteTermsContractError("$: chat rewrite contract does not match")
    return {
        "contract": _CHAT_REWRITE_CONTRACT,
        "terms": _normalize_rewrite_terms(payload.get("terms")),
    }


def _build_chat_rewrite_candidate(text: str) -> Mapping[str, Any]:
    return _normalize_chat_rewrite_candidate(
        {
            "contract": _CHAT_REWRITE_CONTRACT,
            "terms": _parse_rewrite_terms(text),
        }
    )


def _render_chat_rewrite_candidate(payload: Mapping[str, Any]) -> str:
    canonical = _normalize_chat_rewrite_candidate(payload)
    return json.dumps(
        canonical["terms"],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _chat_rewrite_semantic_codec(
    *,
    binding_identity: str,
    system: str,
    prompt: str,
) -> ChatSemanticCodec:
    return ChatSemanticCodec(
        contract_name=_CHAT_REWRITE_CONTRACT,
        binding_fingerprint=fingerprint_input(
            "mnemos.chat.rewrite.binding.v1",
            binding_identity,
            LLMPurpose.CHAT_REWRITE.value,
            system,
            prompt,
        ),
        normalize_response=_build_chat_rewrite_candidate,
        normalizer=_normalize_chat_rewrite_candidate,
        render_candidate=_render_chat_rewrite_candidate,
        parse_failure_code="chat_rewrite_parse_rejected",
        schema_failure_code="chat_rewrite_schema_rejected",
    )


async def _llm_search_terms(
    question: str,
    overview: dict,
    provider: str,
    cfg: dict,
    timeout_s: int,
    run_budget: LLMRunBudget | None = None,
    operation_id: uuid.UUID | None = None,
    attempt_state: ChatProviderAttemptState | None = None,
    semantic_binding: str | None = None,
) -> list[str]:
    """LLM → English code-search terms grounded in THIS project.

    The static ``_KO_CONCEPTS`` map can't scale to every concept; the LLM bridges
    a natural-language (often Korean) question to concrete English identifiers the
    lexical search can actually match. Returns ``[]`` on any failure so the caller
    falls back to the lexical hits + static map — never raises into the request.
    """
    overview_items, _ = _pack_complete_text_items(
        _overview_items(overview), max_chars=REWRITE_OVERVIEW_MAX_CHARS
    )
    ov = "\n".join(overview_items)
    system = (
        "You turn a user's natural-language question (often Korean) into English "
        "code-search terms for THIS project. Output ONLY a JSON array of 3-8 short "
        "terms — concrete identifier fragments (function/class names, domain nouns) "
        "likely to appear in the source. No prose."
    )
    prompt = f"## Project\n{ov}\n\n## Question\n{question}\n\nJSON array:"
    if len(system) + len(prompt) > REWRITE_PROMPT_MAX_CHARS:
        log.warning(
            "chat: rewrite prompt exceeds complete-item budget (%d > %d)",
            len(system) + len(prompt),
            REWRITE_PROMPT_MAX_CHARS,
        )
        return []
    try:
        raw = await provider_chat(
            provider,
            cfg,
            system=system,
            prompt=prompt,
            timeout_s=min(timeout_s, 90),
            max_output_tokens=CHAT_REWRITE_MAX_OUTPUT_TOKENS,
            run_budget=run_budget,
            operation_id=operation_id,
            attempt_state=attempt_state,
            semantic_codec=_chat_rewrite_semantic_codec(
                binding_identity=(
                    semantic_binding
                    or (str(operation_id) if operation_id is not None else "unit")
                ),
                system=system,
                prompt=prompt,
            ),
        )
    except (BudgetExceeded, RunBudgetExceeded, LLMLifecycleError, asyncio.CancelledError):
        raise
    except Exception as exc:  # noqa: BLE001
        # Provider/client exceptions may embed request or response content.
        # Keep the fallback observable without copying that payload to logs.
        log.warning(
            "chat: search-term rewrite failed: %s",
            exc.__class__.__name__,
        )
        return []
    if not raw:
        return []
    try:
        terms = _parse_rewrite_terms(raw)
    except RewriteTermsContractError as exc:
        if attempt_state is not None:
            attempt_state.consumer_result_status = exc.result_status
            attempt_state.consumer_failure_code = (
                "chat_rewrite_parse_rejected"
                if exc.result_status == ResultStatus.PARSE_REJECTED
                else "chat_rewrite_schema_rejected"
            )
        log.warning("chat: search-term rewrite contract rejected: %s", exc)
        return []
    if attempt_state is not None:
        attempt_state.consumer_result_status = ResultStatus.ACCEPTED
        attempt_state.consumer_failure_code = None
    return terms


@router.post("/chat", dependencies=[Depends(require_operator)])
async def chat(
    project_id: uuid.UUID,
    body: ChatRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    cfg = await resolve_config(db)
    if not any_provider_available(cfg):
        raise HTTPException(status_code=503, detail="llm_unavailable")
    provider = body.provider or default_provider(cfg)
    if not is_provider_available(provider, cfg):
        raise HTTPException(status_code=400, detail=f"provider_not_configured:{provider}")

    # Retrieve a *broad* grounded candidate pool, then let the composing LLM
    # pick — top-k=6 alone buried the right symbol behind English homonyms
    # (e.g. "block" → channel gates instead of `beforeToolCall`).
    broad = max(body.top_k, 24)
    hits = await search_symbols(db, project_id=project_id, query=body.message, top_k=broad)
    # Fast, free static concept map (common Korean concepts → English).
    expanded = _expand_terms(body.message)
    static_hits: list[dict] | None = None
    if expanded:
        static_hits = await search_symbols(
            db, project_id=project_id, query=" ".join(expanded), top_k=broad
        )
        hits = _merge_hits(hits, static_hits, broad)
    overview = await _project_overview(db, project_id)
    # The deterministic tokenizer can't bridge Korean→symbol names, so when
    # recall is weak the LLM rewrites the question into project-grounded English
    # search terms (validated: surfaces `beforeToolCall` / `safe_schedule_*`
    # that a Korean concept query otherwise misses entirely).
    rewrite_attempted = False
    rewrite_terms: list[str] = []
    llm_run_budget: LLMRunBudget | None = None
    rewrite_needed = _weak_recall(
        body.message,
        hits,
        static_expansion_hits=static_hits,
    )
    if rewrite_needed:
        rewrite_attempted = True
        llm_run_budget = LLMRunBudget(
            max_calls=CHAT_MAX_PROVIDER_ATTEMPTS,
            max_input_tokens=CHAT_MAX_RESERVED_INPUT_TOKENS,
            max_output_tokens=CHAT_MAX_RESERVED_OUTPUT_TOKENS,
            wall_time_sec=body.timeout_s,
        )
        rewrite_state = ChatProviderAttemptState()
        try:
            async with use_attempt_callbacks(
                _chat_attempt_callbacks(
                    project_id=project_id,
                    purpose=LLMPurpose.CHAT_REWRITE,
                    run_budget=llm_run_budget,
                )
            ):
                rewrite_terms = await _llm_search_terms(
                    body.message,
                    overview,
                    provider,
                    cfg,
                    body.timeout_s,
                    run_budget=llm_run_budget,
                    operation_id=_chat_operation_id(
                        project_id, LLMPurpose.CHAT_REWRITE
                    ),
                    attempt_state=rewrite_state,
                    semantic_binding=str(project_id),
                )
            if rewrite_state.attempt_id is not None:
                rewrite_status = rewrite_state.consumer_result_status
                # Transport/model failures are already terminal physical
                # outcomes and have no semantic candidate to classify.  The
                # optional rewrite must continue with deterministic lexical
                # retrieval instead of feeding NOT_APPLICABLE into the
                # semantic CAS (which intentionally rejects that status).
                if rewrite_status not in {
                    None,
                    ResultStatus.PENDING,
                    ResultStatus.NOT_APPLICABLE,
                }:
                    await _classify_chat_candidate(
                        rewrite_state.attempt_id,
                        result_status=rewrite_status,
                        failure_code=rewrite_state.consumer_failure_code,
                    )
        except (BudgetExceeded, RunBudgetExceeded) as exc:
            reason = getattr(exc, "reason", exc.__class__.__name__)
            raise HTTPException(
                status_code=429,
                detail=f"llm_request_budget_exceeded:{reason}",
            ) from exc
        except LLMLifecycleError as exc:
            raise HTTPException(
                status_code=503,
                detail="llm_accounting_failed",
            ) from exc
        if rewrite_terms:
            more = await search_symbols(
                db,
                project_id=project_id,
                query=" ".join(rewrite_terms),
                top_k=broad,
            )
            hits = _merge_hits(hits, more, broad)
    # Feed a broadened set (was top-6) so the LLM selects the relevant symbols.
    hits = hits[: max(body.top_k, 12)]
    ctx = await _build_context(db, project_id, hits)
    context_md, included_ctx, context_meta = _pack_chat_context(
        overview,
        ctx,
    )
    used_code = any(bool(item.get("code")) for item in included_ctx)
    prompt, prompt_meta = _build_bounded_prompt(
        body.history,
        context_md,
        body.message,
        system=_SYSTEM,
    )
    output_capability = output_token_limit_capability(provider, cfg)

    if llm_run_budget is None:
        # Keep the deterministic retrieval path free of LLM objects.  The
        # request budget begins only when the first provider call is needed.
        llm_run_budget = LLMRunBudget(
            max_calls=CHAT_MAX_PROVIDER_ATTEMPTS,
            max_input_tokens=CHAT_MAX_RESERVED_INPUT_TOKENS,
            max_output_tokens=CHAT_MAX_RESERVED_OUTPUT_TOKENS,
            wall_time_sec=body.timeout_s,
        )

    answer_state = ChatProviderAttemptState()
    try:
        async with use_attempt_callbacks(
            _chat_attempt_callbacks(
                project_id=project_id,
                purpose=LLMPurpose.CHAT_ANSWER,
                run_budget=llm_run_budget,
            )
        ):
            reply = await provider_chat(
                provider,
                cfg,
                system=_SYSTEM,
                prompt=prompt,
                timeout_s=body.timeout_s,
                max_output_tokens=CHAT_ANSWER_MAX_OUTPUT_TOKENS,
                run_budget=llm_run_budget,
                operation_id=_chat_operation_id(
                    project_id, LLMPurpose.CHAT_ANSWER
                ),
                attempt_state=answer_state,
                semantic_codec=chat_answer_semantic_codec(
                    str(project_id),
                    LLMPurpose.CHAT_ANSWER.value,
                    _SYSTEM,
                    prompt,
                ),
            )
        if reply is not None and answer_state.attempt_id is not None:
            await _classify_chat_candidate(
                answer_state.attempt_id,
                result_status=ResultStatus.ACCEPTED,
            )
    except (BudgetExceeded, RunBudgetExceeded) as exc:
        reason = getattr(exc, "reason", exc.__class__.__name__)
        raise HTTPException(
            status_code=429,
            detail=f"llm_request_budget_exceeded:{reason}",
        ) from exc
    except LLMLifecycleError as exc:
        raise HTTPException(
            status_code=503,
            detail="llm_accounting_failed",
        ) from exc
    if reply is None:
        if llm_run_budget.exhausted:
            raise HTTPException(
                status_code=429,
                detail=f"llm_request_budget_exceeded:{llm_run_budget.exhausted_reason}",
            )
        raise HTTPException(status_code=503, detail="llm_call_failed")

    await audit_record(
        actor=f"user:{user.id}",
        action="qa.chat",
        target=(
            "query_sha256:"
            + fingerprint_input("mnemos.audit.chat-query.v1", body.message)
        ),
        project_id=project_id,
        details={
            "query_chars": len(body.message),
            "symbols": [c.get("name") for c in included_ctx],
            "code": used_code,
            "provider": provider,
            "expanded": expanded,
            "rewrite_attempted": rewrite_attempted,
            "prompt_chars": prompt_meta["provider_input_chars"],
            "prompt_truncated": bool(prompt_meta["truncated"] or context_meta["truncated"]),
            "llm_budget": llm_run_budget.stats(),
        },
    )

    truncation = {
        "truncated": bool(prompt_meta["truncated"] or context_meta["truncated"]),
        "prompt": prompt_meta,
        "context": context_meta,
    }
    return {
        "reply": reply,
        "provider": provider,
        "context": [
            {
                "name": c.get("name"),
                "kind": c.get("kind"),
                "file": c.get("file"),
                "line": c.get("line"),
            }
            for c in included_ctx
        ],
        "used_code": used_code,
        "expanded": expanded,
        "rewrite": {
            "attempted": rewrite_attempted,
            "terms": rewrite_terms,
            "skipped_by_strong_static_expansion": bool(
                _has_korean(body.message) and static_hits is not None and not rewrite_needed
            ),
        },
        "truncation": truncation,
        "output_budget": {
            "requested_max_tokens": CHAT_ANSWER_MAX_OUTPUT_TOKENS,
            **output_capability,
        },
        "request_budget": llm_run_budget.stats(),
    }
