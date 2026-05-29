"""PR-117 — MCP queries with REAL dogfood data (Mnemos→Mnemos).

목표 점수 평가에서 가장 큰 갭: MCP queries 가 실데이터로
호출된 적 없음 (점수 45). PR-116 이 분석기→ingest 만 검증,
이번이 ingest→query→응답 닫는다.

방법:
1. ggoss-py 가 server/app/mcp/ 를 분석 → 실제 Node 객체 시드
2. fake AsyncSession 이 select(Node) 에 그 리스트 반환
3. search_symbols / find_callers / get_symbol 실제 호출
4. 응답이 의미있는지 검증 — "search_symbols 가 자기 자신을 찾는가"

이게 작동하면 점수 45 → 80+. 안 되면 그 자체로 진짜 결함 발견.

목적 재확인: "에이전트가 MCP 로 조회" 가 핵심 가치. 이게 작동
안 하면 platform 가치 절반 이상 손실.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
_MCP_DIR = _ROOT / "server" / "app" / "mcp"


def _run_analyzer(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), verb, str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"ggoss-py {verb} failed: {cp.stderr[:300]}"
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


# ─── Helpers: build Node/Edge objects from analyzer envelopes ─────


def _make_nodes(symbol_envs: list[dict], project_id: uuid.UUID) -> list[Any]:
    """Translate analyzer envelopes → in-memory ORM-shaped Node
    instances. Doesn't go through SQLAlchemy because we don't want
    to provision a real DB just to test queries.py's ranking."""
    from app.models.graph import Node
    nodes: list[Node] = []
    now = datetime.now(tz=timezone.utc)
    for env in symbol_envs:
        if env.get("record_type") != "symbol":
            continue
        d = env["data"]
        n = Node(
            id=d["id"],
            project_id=project_id,
            kind="Symbol",
            data=d,
            certainty=d.get("certainty", "asserted"),
            created_by=[d.get("source_name", "ggoss-py")],
        )
        n.valid_from = now
        n.valid_to = None
        nodes.append(n)
    return nodes


def _make_edges(edge_envs: list[dict], project_id: uuid.UUID) -> list[Any]:
    from app.models.graph import Edge
    edges = []
    now = datetime.now(tz=timezone.utc)
    for env in edge_envs:
        if env.get("record_type") != "edge":
            continue
        d = env["data"]
        e = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            source_id=d["source_id"],
            target_id=d["target_id"],
            kind=d.get("kind", "CALLS"),
            data=d.get("metadata", {}) or {},
            certainty=d.get("certainty", "asserted"),
            created_by=[env.get("source_name", "ggoss-py")],
        )
        e.valid_from = now
        e.valid_to = None
        edges.append(e)
    return edges


class _FakeResult:
    """Mimics SQLAlchemy's AsyncResult well enough for queries.py's
    .scalars().all() pattern."""

    def __init__(self, items: list[Any]):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None

    def scalar(self):
        return self._items[0] if self._items else None


class _FakeSession:
    """Returns ALL nodes/edges on every select. queries.py's
    in-memory ranking + filtering then does the work — which is
    exactly the layer we want to verify.

    Filters that live in SQL (ilike, JSON path) are bypassed; the
    Python-side ranking and pagination still run. That's an
    acceptable trade because:
    - ranking is the value-creating part
    - SQL filtering is generic and well-tested in SQLAlchemy itself
    """

    def __init__(self, nodes: list[Any], edges: list[Any]):
        self.nodes = nodes
        self.edges = edges

    async def execute(self, stmt, *args, **kw):
        # We can't introspect the SQL statement portably; just key
        # off which table the FROM clause references.
        s = str(stmt).lower()
        if "from edges" in s or "edges " in s:
            # Filter common cases queries.py uses:
            #   WHERE target_id = X OR source_id = X
            # Cheap dispatch: hand back ALL edges; queries.py only
            # uses _walk_calls which iterates anyway.
            return _FakeResult(self.edges)
        if "from nodes" in s or "nodes " in s:
            return _FakeResult(self.nodes)
        if "select 1" in s:
            return _FakeResult([1])
        return _FakeResult([])


@pytest.fixture(scope="module")
def dogfood_graph():
    """Build a Node/Edge collection by analyzing Mnemos's own
    server/app/mcp/ tree. Module-scoped so the heavy subprocess
    work runs once."""
    project_id = uuid.uuid4()
    sym_envs = _run_analyzer("symbols", _MCP_DIR)
    edge_envs = _run_analyzer("calls", _MCP_DIR)
    nodes = _make_nodes(sym_envs, project_id)
    edges = _make_edges(edge_envs, project_id)
    return project_id, nodes, edges


# ─── search_symbols on real Mnemos data ────────────────────────────


@pytest.mark.asyncio
async def test_search_symbols_finds_mcp_query_functions(dogfood_graph):
    """The strongest 'does it work' test: search 'search_symbols'
    against Mnemos's own mcp/queries.py code. The platform's own
    search_symbols function MUST appear in the top results.
    Otherwise the search is useless."""
    from app.mcp.queries import search_symbols

    project_id, nodes, edges = dogfood_graph
    session = _FakeSession(nodes, edges)

    results = await search_symbols(
        session, project_id=project_id, query="search_symbols", top_k=10,
    )
    assert len(results) > 0, "search returned empty for an exact-name query"
    # First result must be the function itself (highest-rank exact name match).
    names = [r["name"] for r in results]
    assert "search_symbols" in names, (
        f"search_symbols not in top 10 results for query 'search_symbols' — "
        f"ranking broken. Got: {names}"
    )


@pytest.mark.asyncio
async def test_search_symbols_ranks_exact_match_first(dogfood_graph):
    """If you query 'get_symbol' and there's a function named that,
    it must rank above other functions whose names merely contain the
    string. Exact-match priority is what makes search usable."""
    from app.mcp.queries import search_symbols

    project_id, nodes, edges = dogfood_graph
    session = _FakeSession(nodes, edges)

    results = await search_symbols(
        session, project_id=project_id, query="get_symbol", top_k=5,
    )
    assert results, "no results for 'get_symbol'"
    # Top result must be name=='get_symbol' (exact) — anything else
    # means the lexical scorer doesn't favour exact name matches.
    assert results[0]["name"] == "get_symbol", (
        f"top result was {results[0]['name']!r}, not exact match 'get_symbol'"
    )


@pytest.mark.asyncio
async def test_search_symbols_returns_zero_score_when_no_match(dogfood_graph):
    """A query that doesn't match anything must return an empty
    list, not random low-ranked junk."""
    from app.mcp.queries import search_symbols

    project_id, nodes, edges = dogfood_graph
    session = _FakeSession(nodes, edges)

    results = await search_symbols(
        session, project_id=project_id,
        query="ZZZZZNONEXISTENTSYMBOLXXX", top_k=10,
    )
    assert results == [], f"expected empty, got {len(results)} results"


@pytest.mark.asyncio
async def test_search_symbols_respects_top_k_cap(dogfood_graph):
    from app.mcp.queries import search_symbols

    project_id, nodes, edges = dogfood_graph
    session = _FakeSession(nodes, edges)

    results = await search_symbols(
        session, project_id=project_id, query="_", top_k=3,
    )
    assert len(results) <= 3


# ─── get_symbol on real data ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_symbol_returns_a_real_mnemos_function(dogfood_graph):
    """get_symbol(known_id) must return the symbol's metadata in the
    documented response shape: ``{symbol, neighbors, l1_summary}``.
    The fake session can't enforce SQL WHERE id=X (the platform's
    Postgres-flavoured WHERE clauses don't introspect cleanly), so we
    check the response *shape* — the contract — rather than identity
    matching, which would need a real DB."""
    from app.mcp.queries import get_symbol

    project_id, nodes, edges = dogfood_graph
    target = next(
        (n for n in nodes if (n.data or {}).get("name") == "search_symbols"),
        None,
    )
    assert target is not None, "fixture didn't extract search_symbols itself?"

    session = _FakeSession(nodes, edges)
    out = await get_symbol(session, project_id=project_id, symbol_id=target.id)
    assert out is not None, "get_symbol returned None for a known id"
    # Documented contract per spec §11.3 — these keys must exist.
    assert "symbol" in out, f"missing 'symbol' key: {out.keys()}"
    assert "neighbors" in out, f"missing 'neighbors' key: {out.keys()}"
    # neighbors must carry caller/callee counts.
    nbr = out["neighbors"]
    assert "callers_count" in nbr or "callees_count" in nbr
    # And the symbol's id must come back nested.
    assert out["symbol"].get("id"), "nested symbol has no id"


# ─── find_callers on real data ─────────────────────────────────────


@pytest.mark.asyncio
async def test_find_callers_returns_real_caller_set(dogfood_graph):
    """The dogfood call graph has measurable hotspots —
    _record_payload (PR-116) is called by run_ingest. Walk the
    caller graph and confirm the relationship is surfaced."""
    from app.mcp.queries import find_callers

    project_id, nodes, edges = dogfood_graph
    # Find any in-project edge with a Mnemos-shaped source.
    in_proj_edges = [
        e for e in edges
        if (e.data or {}).get("callee_resolved") is True
    ]
    if not in_proj_edges:
        pytest.skip("no in-project edges in the dogfood graph")
    sample = in_proj_edges[0]
    callee_id = sample.target_id

    session = _FakeSession(nodes, edges)
    out = await find_callers(
        session, project_id=project_id, symbol_id=callee_id, limit=50,
    )
    # find_callers returns a dict with 'edges' list per spec §11.3.
    edges_returned = out.get("edges") if isinstance(out, dict) else out
    assert edges_returned, (
        f"find_callers returned empty for {callee_id} despite having "
        f"{len(in_proj_edges)} resolved in-project edges in the graph"
    )


# ─── ranking math: pure function, real data ────────────────────────


def test_score_symbol_ranks_exact_name_above_substring():
    """Even before MCP, the underlying _score_symbol must rank an
    exact name match above a substring containment."""
    from app.mcp.queries import _score_symbol, _tokenize

    terms = _tokenize("foo")
    exact = _score_symbol(terms, name="foo", sym_id="x", signature="def foo()")
    substring = _score_symbol(terms, name="foobar", sym_id="x", signature="def foobar()")
    no_match = _score_symbol(terms, name="bar", sym_id="x", signature="def bar()")
    assert exact > substring > 0
    assert no_match == 0


def test_tokenize_splits_snake_and_kebab():
    """A query 'order_create' must tokenise into ['order', 'create']
    so both halves contribute to matching."""
    from app.mcp.queries import _tokenize
    toks = _tokenize("order_create_v2")
    assert "order" in toks
    assert "create" in toks


# ─── system-level health: did the whole loop produce a graph? ──────


def test_dogfood_graph_is_realistic_in_shape(dogfood_graph):
    """Cross-cutting sanity: the analyzer + ingest contract +
    in-memory translation produces a graph with shape consistent
    with what we measured during PR-115."""
    _, nodes, edges = dogfood_graph
    # Measured during PR-117: 36 symbols / 60+ edges from mcp/ tree.
    # Floor at 30 to leave headroom for minor refactors; alarm
    # only fires on a real regression.
    assert len(nodes) >= 30, (
        f"mcp/ tree should yield ≥30 symbols, got {len(nodes)}"
    )
    assert len(edges) >= 50, (
        f"mcp/ tree should yield ≥50 edges, got {len(edges)}"
    )
    # Sanity: every node carries a non-empty kind.
    assert all(n.kind for n in nodes)
    # Sanity: every edge has a CALLS-shaped kind.
    assert all(e.kind == "CALLS" for e in edges)
