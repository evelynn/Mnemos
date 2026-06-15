"""PR-149 — gap-driven Q&A with on-demand deepening.

The platform analyses large systems with a bounded per-language file
budget, so an early question can hit a symbol that was never extracted —
``search_symbols`` returns nothing and the operator gets "I don't know".
The user's requirement: when the analysis is insufficient to answer,
*deepen it automatically* — find the files most likely to hold the
answer, extract them via the Claude Code subscription, then re-answer.

``POST /projects/{id}/ask`` does exactly that:
1. answer from the graph if a confident symbol match exists;
2. otherwise (and if ``source_root`` is given) rank candidate files by
   the question terms, agent-extract the top few into the graph, and
   retry the answer — reporting whether it had to deepen.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.db import SessionLocal, get_session
from app.extractor.agent_extract import (
    extract_file_via_agent_sdk,
    find_candidate_files,
    is_agent_sdk_available,
    to_envelopes,
)
from app.mcp.queries import get_data_access, get_symbol, search_symbols
from app.orchestrator.jobs import _record_payload

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["ask"],
    dependencies=[Depends(require_project_org())],
)


def _terms(question: str) -> list[str]:
    return [t for t in re.split(r"[^a-zA-Z0-9_]+", question.lower()) if len(t) >= 3]


def _is_confident(hits: list[dict], terms: list[str]) -> bool:
    """A graph answer is confident only when a returned *code Symbol*
    (not a DataEntity / Contract) has a name matching a question term.

    A bare table match — e.g. "order creation handler" lexically hitting
    the ``data:orders`` table — must NOT suppress deepening, or the
    platform would answer "here's the orders table" to "where is the
    handler". Restricting confidence to Symbol nodes is what makes the
    insufficient→deepen path fire for code questions whose implementing
    symbol wasn't extracted yet."""
    for h in hits[:5]:
        sid = str(h.get("symbol_id", ""))
        if sid.startswith("data:") or sid.startswith("contract:"):
            continue
        name = str(h.get("name") or "").lower()
        if any(t in name or t in sid.lower() for t in terms):
            return True
    return False


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    source_root: str | None = Field(
        default=None, description="Repo root; enables deepening when the graph can't answer"
    )
    deepen: bool = Field(default=True)
    max_deepen_files: int = Field(default=2, ge=1, le=6)
    max_file_bytes: int = Field(default=60_000, ge=1, le=400_000)


def _short_path(file: str | None) -> str | None:
    """A human-readable, repo-relative path. Absolute worker paths are
    noise — start from ``src/`` or ``app/`` when present, else basename."""
    if not file:
        return None
    f = file.replace("\\", "/")
    for marker in ("/src/", "/app/"):
        i = f.find(marker)
        if i != -1:
            return f[i + 1 :]
    return f.rsplit("/", 1)[-1]


def _entity_name(entity_id: str | None) -> str:
    """``data.users`` / ``data:orders`` → ``users`` / ``orders``."""
    s = str(entity_id or "")
    for p in ("data.", "data:"):
        if s.startswith(p):
            return s[len(p) :]
    return s


def _answer_text(
    name: str | None, kind: str | None, file: str | None, line: int | None,
    callers: int, callees: int, reads: list[str], writes: list[str],
    summary: str | None,
) -> str:
    """A plain-language answer assembled from graph facts — works with no
    LLM (local mode), so the operator always gets a readable sentence
    instead of a bare symbol id."""
    nm = name or "This symbol"
    kd = kind or "symbol"
    a_an = "an" if kd[:1].lower() in "aeiou" else "a"
    where = ""
    if file:
        where = f" in `{file}`" + (f" (line {line})" if line else "")
    parts = [f"**{nm}** is {a_an} {kd}{where}."]
    if summary:
        parts.append(summary.rstrip(".") + ".")
    if callers:
        parts.append(f"It is called from {callers} place{'s' if callers != 1 else ''}.")
    if callees:
        parts.append(f"It calls {callees} other symbol{'s' if callees != 1 else ''}.")
    if writes:
        parts.append(f"It **writes** to {', '.join(writes)}.")
    if reads:
        parts.append(f"It **reads** from {', '.join(reads)}.")
    return " ".join(parts)


async def _build_answer(
    db: AsyncSession, project_id: uuid.UUID, hits: list[dict]
) -> dict | None:
    if not hits:
        return None
    # Prefer a code Symbol over a DataEntity/Contract lexical hit.
    best = next(
        (
            h
            for h in hits
            if not str(h.get("symbol_id", "")).startswith(("data:", "contract:"))
        ),
        hits[0],
    )
    sym = await get_symbol(db, project_id=project_id, symbol_id=best["symbol_id"])
    da = await get_data_access(db, project_id=project_id, symbol_id=best["symbol_id"])

    sym_data = ((sym or {}).get("symbol") or {}).get("data") or {}
    loc = sym_data.get("location") or {}
    file = _short_path(loc.get("file"))
    line = loc.get("line")
    summary = (sym or {}).get("l1_summary")
    # Local mode (no LLM) writes a placeholder L1 like "[stub L1] <id>
    # summarised from N evidence rows" — that is not a real summary, so
    # keep it out of the human answer.
    if summary and summary.lstrip().startswith("[stub"):
        summary = None
    neighbors = (sym or {}).get("neighbors") or {}
    callers = int(neighbors.get("callers_count") or 0)
    callees = int(neighbors.get("callees_count") or 0)
    writes = [_entity_name(w.get("entity_id")) for w in da.get("writes", [])]
    reads = [_entity_name(w.get("entity_id")) for w in da.get("reads", [])]
    # The node ``kind`` is always "Symbol"; the analyser's specific kind
    # (function / interface / method / class) lives in ``data`` and reads
    # far more naturally in the answer.
    real_kind = sym_data.get("kind") or best.get("kind")
    return {
        "symbol_id": best["symbol_id"],
        "name": best.get("name"),
        "kind": real_kind,
        "file": file,
        "line": line,
        "signature": sym_data.get("signature"),
        "summary": summary,
        "callers_count": callers,
        "callees_count": callees,
        "reads": reads,
        "writes": writes,
        "text": _answer_text(
            best.get("name"), real_kind, file, line,
            callers, callees, reads, writes, summary,
        ),
        "detail": sym,
        "data_access": {"reads": da.get("reads", []), "writes": da.get("writes", [])},
    }


@router.post("/ask", dependencies=[Depends(require_operator)])
async def ask(
    project_id: uuid.UUID,
    body: AskRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    terms = _terms(body.question)
    hits = await search_symbols(db, project_id=project_id, query=body.question, top_k=5)
    answered = _is_confident(hits, terms)

    deepened = False
    extracted_files: list[str] = []
    candidates: list[str] = []

    if not answered and body.deepen and body.source_root:
        if not is_agent_sdk_available():
            raise HTTPException(status_code=503, detail="agent_sdk_unavailable")
        root = Path(body.source_root)
        cands = find_candidate_files(str(root), terms, limit=body.max_deepen_files)
        candidates = [str(p.relative_to(root)) for p, _ in cands]
        for path, lang in cands:
            try:
                code = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(code) > body.max_file_bytes:
                code = code[: body.max_file_bytes]
            rel = str(path.relative_to(root))
            extracted = await extract_file_via_agent_sdk(
                language=lang, file_rel=rel, code=code
            )
            if not extracted:
                continue
            try:
                totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0}
                async with SessionLocal() as s:
                    for env in to_envelopes(lang, rel, extracted):
                        await _record_payload(
                            s,
                            project_id=project_id,
                            payload=env,
                            accept_kinds={"symbol", "data_entity", "edge"},
                            totals=totals,
                        )
                    await s.commit()
                extracted_files.append(rel)
            except Exception:  # noqa: BLE001
                continue
        deepened = bool(extracted_files)
        if deepened:
            hits = await search_symbols(
                db, project_id=project_id, query=body.question, top_k=5
            )
            answered = _is_confident(hits, terms)

    answer = await _build_answer(db, project_id, hits)

    await audit_record(
        actor=f"user:{user.id}",
        action="qa.ask",
        target=body.question[:120],
        project_id=project_id,
        details={"answered": answered, "deepened": deepened, "extracted": extracted_files},
    )

    return {
        "question": body.question,
        "answered": answered,
        "deepened": deepened,
        "deepen_candidates": candidates,
        "extracted_files": extracted_files,
        "matches": [
            {"symbol_id": h["symbol_id"], "name": h.get("name"), "kind": h.get("kind")}
            for h in hits
        ],
        "answer": answer,
    }
