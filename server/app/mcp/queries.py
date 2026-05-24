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
from app.models.graph import Edge, Node

_TOKEN = re.compile(r"[A-Za-z0-9]+")


def _tokenize(query: str | None) -> list[str]:
    """Split a free-text query into lowercased alphanumeric terms.

    A symbol search for "payment retry" must find ``retryPayment`` —
    the old substring match needed the literal phrase. Tokenising lets
    each term match independently.
    """
    return [t.lower() for t in _TOKEN.findall(query or "")]


def _score_symbol(
    terms: list[str], name: str | None, sym_id: str, signature: str | None
) -> float:
    """Lexical relevance of a symbol to the query terms.

    Term-coverage scoring weighted by field — a hit in the name ranks
    well above one in the id or signature, an exact name match highest.
    Returns 0 when no term matched. This is the BM25-ish lexical half
    of spec §11.3's search; the vector half needs an embedding model.
    """
    if not terms:
        return 0.0
    name_l = (name or "").lower()
    id_l = (sym_id or "").lower()
    sig_l = (signature or "").lower()
    score = 0.0
    matched = 0
    for t in terms:
        if t in name_l:
            score += 3.0
            if name_l == t:
                score += 2.0
            matched += 1
        elif t in id_l:
            score += 1.5
            matched += 1
        elif t in sig_l:
            score += 0.5
            matched += 1
    # Every term matched somewhere — a strong signal.
    if matched == len(terms):
        score += 1.0
    return score


async def search_symbols(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    query: str,
    kind: str | None = None,
    top_k: int = 20,
) -> list[dict[str, Any]]:
    top_k = max(1, min(top_k, 200))
    terms = _tokenize(query)
    stmt = (
        select(Node)
        .where(Node.project_id == project_id, Node.valid_to.is_(None))
    )
    if kind:
        stmt = stmt.where(Node.kind == kind)
    if terms:
        # Candidate set — any term matching id / name / signature. The
        # ranking below (not SQL) decides the order.
        conds = []
        for t in terms:
            like = f"%{t}%"
            conds.append(Node.id.ilike(like))
            conds.append(Node.data["name"].astext.ilike(like))
            conds.append(Node.data["signature"].astext.ilike(like))
        stmt = stmt.where(or_(*conds))
    # Cap the candidate scan so a huge graph stays bounded.
    rows = (await session.execute(stmt.limit(2000))).scalars().all()

    scored: list[tuple[float, Node]] = []
    for r in rows:
        d = r.data or {}
        s = _score_symbol(terms, d.get("name"), r.id, d.get("signature"))
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
        try:
            qvec = await embed_query(query)
        except Exception:  # noqa: BLE001
            qvec = None
        if qvec is not None and len(qvec) == EMBEDDING_DIM:
            # Defer the import: pgvector is optional and only imported
            # when the operator has installed the [search] extra.
            try:
                from pgvector.sqlalchemy import Vector  # noqa: F401

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
                # pgvector missing or column absent — fall back to
                # pure-lexical silently; an operator who set the env
                # without running the migration just gets PR-80.
                pass

    return [
        {
            "symbol_id": r.id,
            "name": (r.data or {}).get("name"),
            "component_id": (r.data or {}).get("component_id"),
            "kind": r.kind,
            "certainty": r.certainty,
            "score": round(s, 2),
            "excerpt": (r.data or {}).get("signature"),
        }
        for s, r in scored[:top_k]
    ]


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
            "data": node.data,
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


async def find_runtime_path(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entry_contract_id: str,
    max_depth: int = 6,
) -> dict[str, Any]:
    """Return reachable symbols from a contract via exercised edges.

    ``frequency`` is the bottleneck OTLP hit count along the chain —
    ``min(edge.data.hit_count)`` over the steps taken. The earlier
    hardcoded ``1`` was a stub; PR-25's reconcile already increments
    ``hit_count`` per observation, so the real value is queryable.
    """
    max_depth = max(1, min(max_depth, 10))
    frontier = [entry_contract_id]
    seen: set[str] = {entry_contract_id}
    chain: list[str] = []
    hit_counts: list[int] = []
    for _ in range(max_depth):
        rows = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.source_id.in_(frontier),
                    Edge.valid_to.is_(None),
                    Edge.data["exercised"].astext == "true",
                )
            )
        ).scalars().all()
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
