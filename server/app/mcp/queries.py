"""Shared async query helpers backing both MCP tools and HTTP endpoints.

Keeping the logic in one place means the MCP surface and the GUI surface
cannot drift — both call the same helpers.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.findings import Finding, Summary
from app.models.graph import AnalysisRun, Edge, Node

_TOKEN = re.compile(r"[A-Za-z0-9_]+")

# Grammatical function words + interrogatives that carry no search
# intent. Dropping them is what lets a natural-language question
# ("how does authentication work") rank on its content word
# ("authentication") instead of on "work" incidentally substring-
# matching "...Workbench". Verbs that double as method names
# (get/show/find/list/read/write/use) are deliberately KEPT.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "at", "for", "with", "and",
    "or", "is", "are", "was", "were", "be", "been", "am", "being",
    "do", "does", "did", "doing", "how", "what", "where", "which", "who",
    "whom", "why", "when", "this", "that", "these", "those", "it", "its",
    "as", "by", "from", "into", "about", "me", "my", "mine", "i", "you",
    "your", "we", "our", "us", "they", "them", "their", "can", "could",
    "will", "would", "shall", "should", "may", "might", "must", "here",
    "there", "so", "but", "if", "then", "than", "such", "via", "per",
    "work", "works", "working",
})

# Split identifiers/paths into word tokens: camelCase, PascalCase,
# snake_case, kebab, and path separators. ``api-auth.ts`` → [api, auth,
# ts]; ``getUserById`` → [get, user, by, id].
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_MAX_OUTPUT_STRING_CHARS = 512
_MAX_SIGNATURE_CHARS = 240
_RAW_DATA_KEYS = {
    "body",
    "blob",
    "bytes",
    "code",
    "content",
    "content_base64",
    "file_text",
    "full_text",
    "payload",
    "raw",
    "snippet",
    "source_text",
}
_LOW_SIGNAL_PATH_SEGMENTS = {
    "tests",
    "test",
    "__tests__",
    "vendored",
    "vendor",
    "third_party",
    "thirdparty",
    "node_modules",
    "build",
    "dist",
    "coverage",
}
_SUPPORT_PATH_SEGMENTS = {"tools", "scripts", "fixtures", "examples", "docs"}
_VENDORED_PATH_SEGMENTS = {
    "vendored", "vendor", "third_party", "thirdparty", "node_modules",
}
_TEST_PATH_SEGMENTS = {"tests", "test", "__tests__"}
_GENERATED_PATH_SEGMENTS = {"build", "dist", "coverage"}

_ABS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[/\\]|[/\\])")


def _norm_path(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def source_role(path: str | None) -> str:
    """Classify a source path so agents can tell product code from tests,
    vendored trees, generated output, and tooling (eval doc Task 4 —
    ``source_role`` in search results)."""
    if not path:
        return "product"
    segments = {part for part in _norm_path(path).lower().split("/") if part}
    if segments & _VENDORED_PATH_SEGMENTS:
        return "vendored"
    if segments & _TEST_PATH_SEGMENTS:
        return "test"
    if segments & _GENERATED_PATH_SEGMENTS:
        return "generated"
    if segments & _SUPPORT_PATH_SEGMENTS:
        return "support"
    return "product"


# project_id → common absolute-path prefix of its symbol locations (or None).
# The analyzers disagree on path style (ggoss-ts absolute POSIX, ggoss-py
# absolute Windows, ggoss-cpp project-relative); stripping the per-project
# common prefix gives agents one stable project-relative path to feed into
# narrow read_file calls (eval doc Task 3). Roots don't move for a live
# project, so a simple capped cache is enough.
_ROOT_PREFIX_CACHE: dict[str, str | None] = {}
_ROOT_PREFIX_CACHE_MAX = 256


async def project_root_prefix(
    session: AsyncSession, project_id: uuid.UUID
) -> str | None:
    """Best-effort repository root: the component-wise common prefix of the
    project's *absolute* symbol file paths. None when the project has no
    absolute paths (everything already relative) or only one directory of
    evidence (a prefix inferred from one dir would over-strip)."""
    key = str(project_id)
    if key in _ROOT_PREFIX_CACHE:
        return _ROOT_PREFIX_CACHE[key]
    rows = (
        await session.execute(
            select(Node.data["location"]["file"].astext)
            .where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
            )
            .limit(2000)
        )
    ).all()
    abs_paths = sorted({
        _norm_path(row[0]) for row in rows
        if row[0] and _ABS_PATH_RE.match(row[0])
    })
    prefix: str | None = None
    if abs_paths:
        first = abs_paths[0].split("/")
        last = abs_paths[-1].split("/")
        common: list[str] = []
        for a, b in zip(first, last):
            if a.lower() != b.lower():
                break
            common.append(a)
        # Never let the prefix swallow a whole path (single-file corner
        # case) — the file name itself must survive stripping.
        while common and len("/".join(common)) >= len(abs_paths[0]):
            common.pop()
        prefix = "/".join(common) if common else None
    if len(_ROOT_PREFIX_CACHE) >= _ROOT_PREFIX_CACHE_MAX:
        _ROOT_PREFIX_CACHE.clear()
    _ROOT_PREFIX_CACHE[key] = prefix
    return prefix


def relative_source_path(path: str | None, root_prefix: str | None) -> str | None:
    """Project-relative POSIX path, or None when it cannot be derived."""
    if not path or not isinstance(path, str):
        return None
    p = _norm_path(path)
    if not _ABS_PATH_RE.match(p):
        return p
    if root_prefix and p.lower().startswith(root_prefix.lower() + "/"):
        return p[len(root_prefix) + 1:]
    return None


def _name_tokens(s: str | None) -> set[str]:
    out: set[str] = set()
    for chunk in re.split(r"[^A-Za-z0-9]+", s or ""):
        for tok in _CAMEL.findall(chunk):
            out.add(tok.lower())
    return out


def _signature_excerpt(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    first_line = value.strip().splitlines()[0].strip()
    if len(first_line) <= _MAX_SIGNATURE_CHARS:
        return first_line
    return f"{first_line[:_MAX_SIGNATURE_CHARS]}... [truncated chars={len(value)}]"


def _safe_node_data(data: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (data or {}).items():
        key_s = str(key)
        low = key_s.lower()
        if key_s == "signature":
            out[key_s] = _signature_excerpt(value)
        elif (
            low in _RAW_DATA_KEYS
            or low.endswith("_raw")
            or low.endswith("_content")
            or low.endswith("_blob")
        ):
            out[key_s] = {"omitted": "raw_payload"}
        elif isinstance(value, str) and len(value) > _MAX_OUTPUT_STRING_CHARS:
            out[key_s] = {"omitted": "large_string", "chars": len(value)}
        else:
            out[key_s] = value
    return out


def _path_score_multiplier(path: str | None) -> float:
    if not path:
        return 1.0
    segments = {
        part for part in path.replace("\\", "/").lower().split("/") if part
    }
    if segments & _LOW_SIGNAL_PATH_SEGMENTS:
        return 0.55
    if segments & _SUPPORT_PATH_SEGMENTS:
        return 0.75
    return 1.0


def _prefix_match(term: str, tokens: set[str]) -> bool:
    """A term shares a stem with a word token — the token starts with
    the term (≥ 3 chars: "log"→"logs", "get"→"getUser"), or the term
    starts with the token (≥ 4 chars: "authentication"→"auth"). This is
    the concept half: it finds ``auth`` in ``AuthError`` for an
    "authentication" query WITHOUT the false mid-word hits plain
    substring produces ("flow" inside "overflow")."""
    for tok in tokens:
        if len(term) >= 3 and tok.startswith(term):
            return True
        if len(tok) >= 4 and term.startswith(tok):
            return True
    return False


def _tokenize(query: str | None) -> list[str]:
    """Split a free-text query into lowercased alphanumeric content
    terms (stopwords removed).

    A symbol search for "payment retry" must find ``retryPayment`` —
    the old substring match needed the literal phrase. Tokenising lets
    each term match independently; dropping stopwords keeps a
    natural-language question from ranking on filler words.

    snake_case identifiers (``should_compress``) keep their *whole* form
    so an exact symbol query matches that symbol — AND split into parts
    so cross-convention concept matching (``order_create`` ↔
    ``orderCreate``) still works (PR-186: the whole-form was needed so
    ``should_compress`` beats the bare ``compress`` methods).
    """
    out: list[str] = []
    for m in _TOKEN.findall(query or ""):
        low = m.lower()
        if "_" in low and low.strip("_"):
            if low not in _STOPWORDS:
                out.append(low)
            out.extend(p for p in low.split("_") if p and p not in _STOPWORDS)
        elif low not in _STOPWORDS:
            out.append(low)
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _score_symbol(
    terms: list[str],
    name: str | None,
    sym_id: str,
    signature: str | None,
    path: str | None = None,
) -> float:
    """Lexical relevance of a symbol to the query terms.

    Term-coverage scoring weighted by field — an exact name match ranks
    highest, then a name substring, then a stem/token match (the
    concept half: auth↔authentication), then the file path, then id and
    signature. Returns 0 when no term matched. BM25-ish lexical half of
    spec §11.3's search; the vector half needs an embedding model.
    """
    if not terms:
        return 0.0
    name_l = (name or "").lower()
    sig_l = (signature or "").lower()
    name_tokens = _name_tokens(name)
    aux_tokens = _name_tokens(path) | _name_tokens(sym_id)
    score = 0.0
    matched = 0
    for t in terms:
        if name_l == t:                                  # whole name
            # Exact symbol-name hits must dominate: a named symbol in the
            # question (``executeToolCallsParallel``, ``should_compress``)
            # has to beat several coincidental 4-char-stem collisions
            # (``exec`` ↔ ``shouldSuppressExec…``). PR-186 — the LLM-Ask
            # grounding was burying exact matches under stem floods.
            score += 12.0
        elif t in name_tokens:                           # exact word in name
            score += 8.0
        elif _prefix_match(t, name_tokens):              # stem of a name word
            score += 2.0
        elif t in aux_tokens or _prefix_match(t, aux_tokens):  # file path / id word
            score += 1.5
        elif t in name_l:    # mid-word substring — weak; alone it stays
            score += 1.0     # below the confident threshold (surfaces as a candidate)
        elif t in sig_l:
            score += 0.5
        else:
            continue
        matched += 1
    # Every term matched somewhere — a strong signal.
    if matched == len(terms):
        score += 1.0
    return score * _path_score_multiplier(path)


async def search_symbols(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query: str,
    kind: str | None = None,
    component_id: str | None = None,
    top_k: int = 20,
    scope: str = "all",
    path_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """``scope`` narrows results by source role — ``product`` (product +
    support code), ``tests`` (test helpers only) or ``all``. ``path_prefix``
    keeps only symbols whose project-relative path starts with the given
    prefix. Both default to no filtering (eval doc Task 4)."""
    top_k = max(1, min(top_k, 200))
    terms = _tokenize(query)
    stmt = (
        select(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
    )
    if kind:
        stmt = stmt.where(Node.kind == kind)
    if component_id:
        # Spec §11.3 filter — scope the search to a single component.
        stmt = stmt.where(Node.data["component_id"].astext == component_id)
    if terms:
        # Candidate set — any term matching id / name / signature, plus
        # a 4-char stem for longer concept words so "authentication"
        # reaches the ``auth`` in ``AuthError`` / ``api-auth.ts`` that a
        # full substring match misses. The ranking below (not SQL) orders.
        conds = []
        for t in terms:
            like = f"%{t}%"
            conds.append(Node.id.ilike(like))
            conds.append(Node.data["name"].astext.ilike(like))
            conds.append(Node.data["signature"].astext.ilike(like))
            if len(t) >= 6:
                stem = f"%{t[:4]}%"
                conds.append(Node.id.ilike(stem))
                conds.append(Node.data["name"].astext.ilike(stem))
        stmt = stmt.where(or_(*conds))
    # Cap the candidate scan so a huge graph stays bounded.
    rows = (await session.execute(stmt.limit(2000))).scalars().all()

    scored: list[tuple[float, Node]] = []
    for r in rows:
        d = r.data or {}
        s = _score_symbol(
            terms, d.get("name"), r.id, d.get("signature"),
            (d.get("location") or {}).get("file"),
        )
        if terms and s <= 0:
            continue
        scored.append((s, r))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    # Vector half (spec §11.3) — when MNEMOS_EMBEDDING_PROVIDER is
    # configured AND nodes carry stored embeddings (alembic 0022), the
    # query is embedded and cosine-similar nodes are merged into the
    # ranking via RRF. Disabled deployments keep the pure-lexical path
    # above — no behaviour change without the env flag.
    from app.mcp.embeddings import (
        EMBEDDING_DIM,
        embed_query,
        is_enabled,
        rrf_fuse,
    )

    by_id = {r.id: (s, r) for s, r in scored}
    if terms and is_enabled():
        # PR-138 — embedding-fallback visibility. Pre-fix every silent
        # failure (API timeout, pgvector missing, alembic 0022 not run)
        # degraded to BM25-only with zero operator signal. Each path
        # now ticks ``mnemos_embedding_silent_fallback_total{reason}``
        # so the ops dashboard finally sees it.
        from app.obs.metrics import embedding_silent_fallback_total
        try:
            qvec = await embed_query(query)
        except Exception:  # noqa: BLE001
            qvec = None
        if qvec is None:
            embedding_silent_fallback_total.labels(reason="api_call_failed").inc()
        elif len(qvec) != EMBEDDING_DIM:
            embedding_silent_fallback_total.labels(reason="dim_mismatch").inc()
        else:
            # Defer the import: pgvector is optional and only imported
            # when the operator has installed the [search] extra.
            try:
                from pgvector.sqlalchemy import Vector  # noqa: F401
            except ImportError:
                embedding_silent_fallback_total.labels(reason="pgvector_missing").inc()
            else:
                try:
                    vec_rows = (
                        await session.execute(
                            sa.text(
                                "SELECT id FROM nodes "
                                "WHERE project_id = :pid AND valid_to IS NULL "
                                "  AND embedding IS NOT NULL "
                                "ORDER BY embedding <=> (:q)::vector "
                                "LIMIT :k"
                            ),
                            {
                                "pid": str(project_id),
                                "q": "[" + ",".join(str(x) for x in qvec) + "]",
                                "k": top_k * 4,
                            },
                        )
                    ).all()
                    vector_ranked = [row[0] for row in vec_rows]
                    lexical_ranked = [r.id for _, r in scored]
                    fused = rrf_fuse(lexical_ranked, vector_ranked)
                    # Re-order using the fused score, keeping only entries
                    # we have a Node for (lexical candidate set).
                    rebuilt: list[tuple[float, Node]] = []
                    for sid, fscore in fused:
                        if sid in by_id:
                            _, node = by_id[sid]
                            rebuilt.append((fscore, node))
                    if rebuilt:
                        scored = rebuilt
                except Exception:  # noqa: BLE001
                    embedding_silent_fallback_total.labels(
                        reason="vector_query_failed"
                    ).inc()

    root_prefix = await project_root_prefix(session, project_id)
    norm_path_prefix = _norm_path(path_prefix).lower().rstrip("/") if path_prefix else None
    out: list[dict[str, Any]] = []
    for s, r in scored:
        if len(out) >= top_k:
            break
        d = r.data or {}
        loc = d.get("location") or {}
        file_path = loc.get("file")
        role = source_role(file_path)
        if scope == "product" and role not in ("product", "support"):
            continue
        if scope == "tests" and role != "test":
            continue
        rel = relative_source_path(file_path, root_prefix)
        if norm_path_prefix is not None:
            probe = (rel or _norm_path(file_path or "")).lower()
            if not (probe == norm_path_prefix
                    or probe.startswith(norm_path_prefix + "/")):
                continue
        out.append({
            "symbol_id": r.id,
            "name": d.get("name"),
            "component_id": d.get("component_id"),
            "kind": r.kind,
            "certainty": r.certainty,
            "score": round(s, 2),
            "excerpt": _signature_excerpt(d.get("signature")),
            "location": {
                "file": file_path,
                "relative_file": rel,
                "line": loc.get("line"),
            },
            "source_role": role,
        })
    return out


async def get_symbol(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
) -> dict[str, Any] | None:
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == symbol_id,
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return None
    callers = (
        await session.execute(
            select(Edge.id)
            .where(
                Edge.project_id == project_id,
                Edge.target_id == symbol_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
            )
            .limit(1001)
        )
    ).all()
    callees = (
        await session.execute(
            select(Edge.id)
            .where(
                Edge.project_id == project_id,
                Edge.source_id == symbol_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
            )
            .limit(1001)
        )
    ).all()
    # L1 summary, when the analysis loop has generated one — spec
    # §11.3 lists it on get_symbol so an agent can read what the
    # symbol *does* without re-reading the source.
    l1 = (
        await session.execute(
            select(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.target_id == symbol_id,
                Summary.level == 1,
                Summary.superseded_by.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "symbol": {
            "id": node.id,
            "kind": node.kind,
            "data": _safe_node_data(node.data),
            "certainty": node.certainty,
        },
        "l1_summary": l1.summary if l1 is not None else None,
        "neighbors": {
            "callers_count": len(callers),
            "callees_count": len(callees),
        },
    }


def _edge_out(e: Edge) -> dict[str, Any]:
    return {
        "caller_id": e.source_id,
        "callee_id": e.target_id,
        "certainty": e.certainty,
        # Spec §11.3 — every edge surfaces whether OTLP confirmed it.
        "exercised": str((e.data or {}).get("exercised", "")).lower() == "true",
        "site": (e.data or {}).get("invocation_site"),
    }


async def _walk_calls(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    start: str,
    direction: str,  # "callers" → walk source from target=current
    transitive: bool,
    max_depth: int,
    limit: int,
) -> dict[str, Any]:
    """Shared BFS used by both find_callers and find_callees.

    Returns ``{edges, truncated, depth_reached}``. When ``transitive``
    is false a single hop is returned, matching the simple-find shape.
    ``truncated`` flips to true when the ``limit`` cap is hit.
    """
    max_depth = max(1, min(max_depth, 5))
    limit = max(1, min(limit, 1000))
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = {start}
    frontier: list[str] = [start]
    truncated = False
    depth_reached = 0
    for depth in range(1, max_depth + 1):
        if not frontier:
            break
        if direction == "callers":
            cond = (Edge.target_id.in_(frontier),)
        else:
            cond = (Edge.source_id.in_(frontier),)
        rows = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                    *cond,
                ).limit(limit + 1)
            )
        ).scalars().all()
        depth_reached = depth
        if not rows:
            break
        next_frontier: list[str] = []
        for e in rows:
            if len(edges) >= limit:
                truncated = True
                break
            edges.append(_edge_out(e))
            nxt = e.source_id if direction == "callers" else e.target_id
            if nxt not in seen_nodes:
                seen_nodes.add(nxt)
                next_frontier.append(nxt)
        if truncated or not transitive:
            break
        frontier = next_frontier
    return {"edges": edges, "truncated": truncated, "depth_reached": depth_reached}


async def find_callers(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
    limit: int = 100,
    transitive: bool = False,
    max_depth: int = 3,
) -> dict[str, Any]:
    return await _walk_calls(
        session,
        project_id=project_id,
        start=symbol_id,
        direction="callers",
        transitive=transitive,
        max_depth=max_depth,
        limit=limit,
    )


async def find_callees(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
    limit: int = 100,
    transitive: bool = False,
    max_depth: int = 3,
) -> dict[str, Any]:
    return await _walk_calls(
        session,
        project_id=project_id,
        start=symbol_id,
        direction="callees",
        transitive=transitive,
        max_depth=max_depth,
        limit=limit,
    )


async def impact_analysis(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Transitive caller walk + data / test / runtime impacts."""
    max_depth = max(1, min(max_depth, 5))
    direct = [
        e["caller_id"]
        for e in (
            await find_callers(
                session, project_id=project_id, symbol_id=symbol_id, limit=500
            )
        )["edges"]
    ]
    seen = set(direct)
    frontier = list(direct)
    transitive: list[str] = []
    for _depth in range(max_depth - 1):
        next_frontier: list[str] = []
        for node in frontier:
            walk = await find_callers(
                session, project_id=project_id, symbol_id=node, limit=500
            )
            for caller in walk["edges"]:
                cid = caller["caller_id"]
                if cid in seen:
                    continue
                seen.add(cid)
                next_frontier.append(cid)
                transitive.append(cid)
        if not next_frontier:
            break
        frontier = next_frontier
    # Fan out from every affected symbol to the DataEntities they
    # touch (READS/WRITES) — was a `[]` stub. Tests are flagged by a
    # ``data.is_test`` marker on the Node, falling back to a name
    # heuristic. ``runtime_exercised`` is true iff any edge along the
    # caller chain carries the OTLP-stamped exercised flag.
    affected_set = {symbol_id, *direct, *transitive}
    data_rows = (
        await session.execute(
            select(Edge.source_id, Edge.target_id, Edge.kind, Edge.data).where(
                Edge.project_id == project_id,
                Edge.source_id.in_(affected_set),
                Edge.kind.in_(("READS", "WRITES")),
                Edge.valid_to.is_(None),
            )
        )
    ).all()
    affected_data_entities = sorted({row[1] for row in data_rows})

    runtime_exercised = any(
        str((row[3] or {}).get("exercised", "")).lower() == "true"
        for row in data_rows
    )
    if not runtime_exercised:
        # Also check the call edges in the chain.
        chk = (
            await session.execute(
                select(Edge.data).where(
                    Edge.project_id == project_id,
                    Edge.source_id.in_(affected_set),
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                ).limit(2000)
            )
        ).all()
        runtime_exercised = any(
            str((row[0] or {}).get("exercised", "")).lower() == "true"
            for row in chk
        )

    test_rows = (
        await session.execute(
            select(Node.id, Node.data).where(
                Node.project_id == project_id,
                Node.id.in_(affected_set),
                Node.valid_to.is_(None),
            )
        )
    ).all()
    affected_tests: list[str] = []
    opaque_components: list[str] = []
    for nid, ndata in test_rows:
        d = ndata or {}
        if d.get("is_test") or "test" in nid.lower() or "spec" in nid.lower():
            affected_tests.append(nid)
        if d.get("is_opaque") or d.get("kind") == "OpaqueComponent":
            opaque_components.append(nid)

    return {
        "directly_affected": direct,
        "transitively_affected": transitive,
        "affected_tests": sorted(set(affected_tests)),
        "affected_data_entities": affected_data_entities,
        "opaque_components_touched": sorted(set(opaque_components)),
        "runtime_exercised": runtime_exercised,
    }


async def list_findings(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    severity: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    stmt = (
        select(Finding)
        .where(Finding.project_id == project_id)
        .order_by(Finding.last_seen_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if severity:
        stmt = stmt.where(Finding.severity == severity)
    if status:
        stmt = stmt.where(Finding.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": str(f.id),
            "kind": f.kind,
            "severity": f.severity,
            "status": f.status,
            "subject_node_id": f.subject_node_id,
            "subject_edge_id": str(f.subject_edge_id) if f.subject_edge_id else None,
            "detail": f.detail,
            "first_seen_at": f.first_seen_at.isoformat(),
            "last_seen_at": f.last_seen_at.isoformat(),
        }
        for f in rows
    ]


async def get_module_summary(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_id: str,
    level: int,
) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.level == level,
                Summary.superseded_by.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    # Certainty rollup — spec §11.3 mandates the
    # {verified, asserted, inferred} breakdown so an agent can tell
    # how trustworthy the narrative's evidence is.
    breakdown = {"verified": 0, "asserted": 0, "inferred": 0}
    for claim in row.claims or []:
        for ev in (claim.get("evidence") or []) if isinstance(claim, dict) else []:
            c = ev.get("certainty") if isinstance(ev, dict) else None
            if c in breakdown:
                breakdown[c] += 1
    return {
        "target_id": row.target_id,
        "level": row.level,
        "summary": row.summary,
        "detailed": row.detailed,
        "claims": row.claims,
        "open_questions": row.open_questions,
        "certainty_breakdown": breakdown,
        "generated_at": row.generated_at.isoformat(),
        "model_used": row.model_used,
    }


async def list_flows(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List the cross-tier process flows (level-4 summaries from
    ``trace_flow``) for the project, so an agent can discover which
    processes have been traced before fetching one with
    ``get_module_summary(target_id, level=4)``. Returns id + one-line
    summary + the step/flag/data sections so a single call is enough to
    *show* the process without a second round-trip."""
    rows = (
        await session.execute(
            select(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.level == 4,
                Summary.superseded_by.is_(None),
            )
            .order_by(Summary.generated_at.desc())
            .limit(max(1, min(limit, 200)))
        )
    ).scalars().all()
    return [
        {
            "target_id": r.target_id,
            "summary": r.summary,
            "detailed": r.detailed,
            "sections": r.claims or [],
            "open_questions": r.open_questions or [],
            "generated_at": r.generated_at.isoformat() if r.generated_at else None,
        }
        for r in rows
    ]



_TIME_WINDOW_SUFFIXES = {"h": 3600, "d": 86400, "w": 7 * 86400}


def _parse_time_window(s: str | None) -> int | None:
    """Parse spec §11.3 ``time_window`` like ``"7d"`` / ``"24h"`` /
    ``"4w"`` to seconds. Returns None when no/invalid window — the
    caller treats that as "no time filter"."""
    if not s:
        return None
    s = s.strip().lower()
    if len(s) < 2 or s[-1] not in _TIME_WINDOW_SUFFIXES:
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    return n * _TIME_WINDOW_SUFFIXES[s[-1]] if n > 0 else None


async def find_runtime_path(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entry_contract_id: str,
    max_depth: int = 6,
    time_window: str | None = None,
) -> dict[str, Any]:
    """Return reachable symbols from a contract via exercised edges.

    ``time_window`` (spec §11.3, e.g. ``"7d"``) filters out edges whose
    ``last_seen_at`` predates the window — otherwise an edge exercised
    once six months ago leaks into "common paths today".

    ``frequency`` is the bottleneck OTLP hit count along the chain —
    ``min(edge.data.hit_count)`` over the steps taken. The earlier
    hardcoded ``1`` was a stub; PR-25's reconcile already increments
    ``hit_count`` per observation, so the real value is queryable.
    """
    max_depth = max(1, min(max_depth, 10))
    window_sec = _parse_time_window(time_window)
    frontier = [entry_contract_id]
    seen: set[str] = {entry_contract_id}
    chain: list[str] = []
    hit_counts: list[int] = []
    cutoff_iso: str | None = None
    if window_sec is not None:
        from datetime import datetime, timedelta, timezone

        cutoff_iso = (
            datetime.now(tz=timezone.utc) - timedelta(seconds=window_sec)
        ).isoformat()
    for _ in range(max_depth):
        stmt = select(Edge).where(
            Edge.project_id == project_id,
            Edge.source_id.in_(frontier),
            Edge.valid_to.is_(None),
            Edge.data["exercised"].astext == "true",
        )
        if cutoff_iso is not None:
            # last_seen_at lives in Edge.data; compare ISO strings
            # (RFC-3339 ISO compares correctly when both are tz-aware).
            stmt = stmt.where(Edge.data["last_seen_at"].astext >= cutoff_iso)
        rows = (await session.execute(stmt)).scalars().all()
        frontier = []
        for e in rows:
            if e.target_id in seen:
                continue
            seen.add(e.target_id)
            frontier.append(e.target_id)
            chain.append(e.target_id)
            try:
                hit_counts.append(int((e.data or {}).get("hit_count", 0)))
            except (TypeError, ValueError):
                hit_counts.append(0)
        if not frontier:
            break
    frequency = min(hit_counts) if hit_counts else 0
    return {
        "common_paths": [
            {"frequency": frequency, "chain": chain, "depth": len(chain)}
        ],
    }


async def get_data_access(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    symbol_id: str,
    limit: int = 200,
) -> dict[str, Any]:
    """List the DataEntities a symbol READS / WRITES (spec §11.3).

    The data has been in the graph since PR-61 (READS/WRITES edges
    from analyzer ``data_access``) but no Q&A tool surfaced it — an
    agent asking "where does this function touch the DB?" got only
    static caller hops back from ``find_callees`` with no entity
    info. This is the missing tool.
    """
    limit = max(1, min(limit, 1000))
    rows = (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.source_id == symbol_id,
                Edge.kind.in_(("READS", "WRITES")),
                Edge.valid_to.is_(None),
            ).limit(limit)
        )
    ).scalars().all()

    reads: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    for e in rows:
        item = {
            "entity_id": e.target_id,
            "certainty": e.certainty,
            "exercised": str((e.data or {}).get("exercised", "")).lower() == "true",
            "access_site": (e.data or {}).get("access_site"),
        }
        (writes if e.kind == "WRITES" else reads).append(item)
    return {
        "symbol_id": symbol_id,
        "reads": reads,
        "writes": writes,
        "truncated": len(rows) >= limit,
    }


async def get_contract(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    contract_id: str,
) -> dict[str, Any] | None:
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == contract_id,
                Node.kind == "Contract",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return None

    exposers = (
        await session.execute(
            select(Edge.source_id)
            .where(
                Edge.project_id == project_id,
                Edge.target_id == contract_id,
                Edge.kind == "EXPOSES",
                Edge.valid_to.is_(None),
            )
            .limit(100)
        )
    ).all()
    callers = (
        await session.execute(
            select(Edge.source_id)
            .where(
                Edge.project_id == project_id,
                Edge.target_id == contract_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
            )
            .limit(500)
        )
    ).all()
    return {
        "contract": node.data,
        "exposers": [row[0] for row in exposers],
        "callers": [row[0] for row in callers],
        "runtime_stats": None,
    }


# ─── temporal comparison (bitemporal graph diff) ───────────────────
#
# PR-204 — "what changed between two analysis runs" and its blast radius.
# codebase-memory-mcp re-indexes without keeping history, so it CANNOT
# compare graph states across commits/runs. Mnemos's bitemporal graph
# (valid_from / valid_to per row) makes an as-of snapshot a query, so a diff
# is two snapshots subtracted — an analysis + comparison that a re-indexing
# tool structurally can't do. Grounded: the diff preserves certainty so an
# agent knows how trustworthy each change is.


def _live_at(model, when):
    """Bitemporal predicate — the row is the live version at time ``when``."""
    return (model.valid_from <= when) & (
        (model.valid_to.is_(None)) | (model.valid_to > when)
    )


async def _run_asof(session, project_id, run_id):
    run = (
        await session.execute(
            select(AnalysisRun).where(
                AnalysisRun.project_id == project_id, AnalysisRun.id == run_id
            )
        )
    ).scalar_one_or_none()
    if run is None:
        return None, None
    return run, (run.completed_at or run.created_at)


async def _node_snapshot(session, project_id, when):
    rows = (
        await session.execute(
            select(Node.id, Node.kind, Node.valid_from, Node.certainty, Node.data)
            .where(Node.project_id == project_id, _live_at(Node, when))
        )
    ).all()
    return {
        nid: {"kind": kind, "vfrom": vf, "certainty": cert,
              "name": (data or {}).get("name")}
        for nid, kind, vf, cert, data in rows
    }


async def _edge_snapshot(session, project_id, when):
    rows = (
        await session.execute(
            select(Edge.source_id, Edge.target_id, Edge.kind)
            .where(Edge.project_id == project_id, _live_at(Edge, when))
        )
    ).all()
    return {(s, t, k) for s, t, k in rows}


async def compare_runs(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    run_a_id: uuid.UUID,
    run_b_id: uuid.UUID,
    limit: int = 40,
) -> dict[str, Any]:
    """Diff the graph between two analysis runs (bitemporal). Returns bounded
    added / removed / modified symbols + contracts, edge-kind deltas, and new
    findings — with certainty preserved. The older run is ``before``."""
    limit = max(1, min(limit, 200))
    run_a, t_a = await _run_asof(session, project_id, run_a_id)
    run_b, t_b = await _run_asof(session, project_id, run_b_id)
    if run_a is None or run_b is None:
        return {"error": "run_not_found"}
    if t_a is None or t_b is None:
        return {"error": "run_incomplete"}
    # Order chronologically — ``before`` is the earlier snapshot.
    if t_a > t_b:
        run_a, run_b, t_a, t_b = run_b, run_a, t_b, t_a

    na = await _node_snapshot(session, project_id, t_a)
    nb = await _node_snapshot(session, project_id, t_b)
    a_ids, b_ids = set(na), set(nb)

    def _kind_bucket(ids, snap):
        out: dict[str, list[dict]] = {}
        for nid in ids:
            info = snap[nid]
            out.setdefault(info["kind"], []).append(
                {"id": nid, "name": info["name"], "certainty": info["certainty"]}
            )
        return out

    added_ids = b_ids - a_ids
    removed_ids = a_ids - b_ids
    # Modified = same id, but the live version was superseded between A and B.
    modified_ids = {
        nid for nid in (a_ids & b_ids) if na[nid]["vfrom"] != nb[nid]["vfrom"]
    }
    added = _kind_bucket(added_ids, nb)
    removed = _kind_bucket(removed_ids, na)
    modified = _kind_bucket(modified_ids, nb)

    def _section(kind):
        return {
            "added": added.get(kind, [])[:limit],
            "removed": removed.get(kind, [])[:limit],
            "modified": modified.get(kind, [])[:limit],
            "added_count": len(added.get(kind, [])),
            "removed_count": len(removed.get(kind, [])),
            "modified_count": len(modified.get(kind, [])),
        }

    ea = await _edge_snapshot(session, project_id, t_a)
    eb = await _edge_snapshot(session, project_id, t_b)
    edge_added, edge_removed = eb - ea, ea - eb
    edge_delta: dict[str, dict[str, int]] = {}
    for s, t, k in edge_added:
        edge_delta.setdefault(k, {"added": 0, "removed": 0})["added"] += 1
    for s, t, k in edge_removed:
        edge_delta.setdefault(k, {"added": 0, "removed": 0})["removed"] += 1

    # New findings — first observed within (t_a, t_b].
    new_findings = (
        await session.execute(
            select(Finding)
            .where(
                Finding.project_id == project_id,
                Finding.first_seen_at > t_a,
                Finding.first_seen_at <= t_b,
            )
            .order_by(Finding.risk_score.desc())
            .limit(limit)
        )
    ).scalars().all()

    # Change blast radius — who called the changed / removed symbols in the
    # BEFORE graph. Those callers are what a reviewer must re-check. This is
    # the analysis+comparison fusion (what changed AND what it affects) that a
    # history-less tool cannot produce.
    changed_targets = list(modified_ids | removed_ids)[:200]
    change_impact: list[dict[str, Any]] = []
    if changed_targets:
        caller_rows = (
            await session.execute(
                select(Edge.source_id, Edge.target_id)
                .where(
                    Edge.project_id == project_id,
                    Edge.kind == "CALLS",
                    _live_at(Edge, t_a),
                    Edge.target_id.in_(changed_targets),
                )
                .limit(2000)
            )
        ).all()
        by_target: dict[str, list[str]] = {}
        for src, tgt in caller_rows:
            by_target.setdefault(tgt, []).append(src)
        change_impact = [
            {
                "changed_symbol": tgt,
                "change_kind": "removed" if tgt in removed_ids else "modified",
                "affected_callers": callers[:10],
                "affected_count": len(callers),
            }
            for tgt, callers in sorted(
                by_target.items(), key=lambda kv: -len(kv[1])
            )
        ][:limit]

    return {
        "before": {"run_id": str(run_a.id), "git_sha": run_a.git_sha,
                   "at": t_a.isoformat()},
        "after": {"run_id": str(run_b.id), "git_sha": run_b.git_sha,
                  "at": t_b.isoformat()},
        "symbols": _section("Symbol"),
        "contracts": _section("Contract"),
        "data_entities": _section("DataEntity"),
        "edges": edge_delta,
        "new_findings": [
            {"id": str(f.id), "kind": f.kind, "severity": f.severity,
             "risk_score": f.risk_score, "subject_node_id": f.subject_node_id}
            for f in new_findings
        ],
        "new_findings_count": len(new_findings),
        "change_impact": change_impact,
        "summary": {
            "symbols_added": _section("Symbol")["added_count"],
            "symbols_removed": _section("Symbol")["removed_count"],
            "symbols_modified": _section("Symbol")["modified_count"],
            "edges_added": len(edge_added),
            "edges_removed": len(edge_removed),
            "contracts_changed": (_section("Contract")["added_count"]
                                  + _section("Contract")["removed_count"]),
            "new_findings": len(new_findings),
            "impacted_callers": sum(c["affected_count"] for c in change_impact),
        },
        "note": "certainty preserved per change; older run is 'before'. "
                "Bitemporal diff — a re-indexing tool without history cannot "
                "produce this.",
    }
