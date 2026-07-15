"""PR-149 — gap-driven Q&A with on-demand deepening.

The platform analyses large systems with a bounded per-language file
budget, so an early question can hit a symbol that was never extracted —
``search_symbols`` returns nothing and the operator gets "I don't know".
The user's requirement: when the analysis is insufficient to answer,
*deepen it automatically* — find the files most likely to hold the
answer, extract them via the Claude Code subscription, then re-answer.

``POST /projects/{id}/ask`` does exactly that:
1. answer from the graph if a confident symbol match exists;
2. otherwise report the evidence gap. Snapshot-bound on-demand extraction is
   intentionally disabled until it can stage and atomically publish facts;
   request-supplied server paths are never trusted as project source.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.api.graph_guard import require_readable_current_graph
from app.mcp.queries import (
    _name_tokens,
    _prefix_match,
    _tokenize,
    get_data_access,
    get_symbol,
    search_symbols,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["ask"],
    dependencies=[
        Depends(require_project_org()),
        Depends(require_readable_current_graph, scope="function"),
    ],
)


# A graph answer counts as confident only at this search score — a
# name (3.0/5.0), or a stem/concept hit (2.5) once the all-terms-matched
# bonus is added. Below it (file-path 1.5, id 1.2, signature 0.5 only)
# the match is too loose to assert "X is the answer", so the UI presents
# candidates instead of a definitive answer — and the deepen path may fire.
_CONFIDENT_SCORE = 3.0


def _terms(question: str) -> list[str]:
    # Stopword-filtered content terms (shared with the search ranker) so
    # "how does authentication work" reduces to ["authentication"], not
    # filler that incidentally matches a symbol.
    return _tokenize(question)


def _name_coverage(name: str | None, terms: list[str]) -> int:
    """How many of the question's content terms actually hit this
    symbol's *name* (exact word, shared stem, or substring)."""
    name_l = (name or "").lower()
    toks = _name_tokens(name)
    return sum(
        1 for t in terms
        if t in toks or _prefix_match(t, toks) or t in name_l
    )


def _is_confident(hits: list[dict], terms: list[str]) -> bool:
    """Confident only when a returned *code Symbol* (not a
    DataEntity / Contract) clears ``_CONFIDENT_SCORE`` AND genuinely
    covers the question.

    Two guards:
    - A bare table match — e.g. "order creation handler" lexically hitting
      the ``data:orders`` table — must NOT suppress deepening, so
      DataEntity/Contract nodes never count (keeps the deepen path firing).
    - A multi-term question must have **>1** of its content terms hit the
      symbol name. Without this, a lone word-match on an unrelated
      multi-word name poses as a confident answer — "search index" →
      ``statusIndex`` (only "index" matched), "errors logged" →
      ``useBindingErrors`` (only "errors"). Such partial matches still
      surface as the tentative "closest match", just not as a definitive
      answer."""
    if not terms:
        return False
    for h in hits[:5]:
        sid = str(h.get("symbol_id", ""))
        if sid.startswith("data:") or sid.startswith("contract:"):
            continue
        score = float(h.get("score") or 0.0)
        if score < _CONFIDENT_SCORE:
            continue
        if len(terms) < 2 or _name_coverage(h.get("name"), terms) >= 2:
            return True
        # Only one term hit a multi-term question. Stay confident *only*
        # if that hit is the operator typing the symbol's whole compound
        # name (score 5.0 + ≥2 word tokens) — a deliberate identifier like
        # "saveCache" — not one word of an unrelated or common-word name
        # ("index" in statusIndex, "connection" in Connection).
        if score >= 5.0 and len(_name_tokens(h.get("name"))) >= 2:
            return True
    return False


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=500)
    source_root: str | None = Field(
        default=None,
        description=(
            "Deprecated and ignored; arbitrary server paths cannot be used "
            "as analysis evidence."
        ),
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
        "deepening_status": (
            "not_needed"
            if answered
            else "snapshot_bound_deepening_not_implemented"
        ),
        "deepen_candidates": candidates,
        "extracted_files": extracted_files,
        "matches": [
            {"symbol_id": h["symbol_id"], "name": h.get("name"), "kind": h.get("kind")}
            for h in hits
        ],
        "answer": answer,
    }
