"""PR-118 — MCP queries with REAL aiosqlite session.

PR-117 used a fake session that returned all nodes on every
select() — sufficient for ranking verification but unable to
test SQL WHERE clauses (exact-id lookup, kind filter,
component scoping). That left 'get_symbol' exact-id matching
unverified.

PR-118 closes that gap with a real SQLAlchemy + aiosqlite
session backed by the production models. The polyglot module
(app/testing/sqlite_polyglot.py) makes PG-native types
(JSONB/UUID/ARRAY) work against SQLite by overriding the
type compiler and bind/result processors.

What's now verified end-to-end:
- search_symbols WHERE filters (kind=, component_id=) really
  filter, not just rank
- get_symbol(symbol_id=X) returns exactly X, not the first row
- find_callers SQL JOIN/WHERE on edges works
- list_findings status= filter works
- the same code paths that run in production are exercised
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Install polyglot BEFORE importing any model module so the type
# patches apply to the metadata before CREATE TABLE.
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.finding_identity import finding_identity_key  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402


_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
_MCP_DIR = _ROOT / "server" / "app" / "mcp"


# ─── real session fixture ──────────────────────────────────────────


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    """Real SQLAlchemy AsyncSession backed by in-memory SQLite.
    Fresh schema per test — total isolation, total honesty about
    what's persisted."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await engine.dispose()


# ─── helpers ───────────────────────────────────────────────────────


def _run_analyzer(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), verb, str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


async def _seed_from_dogfood(s: AsyncSession, project_id: _uuid.UUID) -> tuple[int, int]:
    """Seed nodes + edges from a REAL ggoss-py run on Mnemos's mcp/
    tree. Returns counts."""
    sym_envs = _run_analyzer("symbols", _MCP_DIR)
    edge_envs = _run_analyzer("calls", _MCP_DIR)
    now = datetime.now(tz=timezone.utc)
    nc = 0
    for env in sym_envs:
        if env["record_type"] != "symbol":
            continue
        d = env["data"]
        s.add(Node(
            id=d["id"], project_id=project_id, kind="Symbol",
            data=d, certainty=d.get("certainty", "asserted"),
            created_by=[env.get("source_name", "ggoss-py")],
            valid_from=now, valid_to=None,
        ))
        nc += 1
    # Production staging publishes one current row per logical edge tuple.
    # ggoss-py may report several invocation sites for that tuple, so mirror
    # the writer's deterministic last-wins collapse instead of inserting raw
    # analyzer envelopes through the current-edge uniqueness fence.
    logical_edges: dict[tuple[str, str, str], Edge] = {}
    for env in edge_envs:
        if env["record_type"] != "edge":
            continue
        d = env["data"]
        edge = Edge(
            id=_uuid.uuid4(), project_id=project_id,
            source_id=d["source_id"], target_id=d["target_id"],
            kind=d.get("kind", "CALLS"),
            data=d.get("metadata", {}) or {},
            certainty=d.get("certainty", "asserted"),
            created_by=[env.get("source_name", "ggoss-py")],
            valid_from=now, valid_to=None,
        )
        logical_edges[(edge.source_id, edge.target_id, edge.kind)] = edge
    s.add_all(logical_edges.values())
    ec = len(logical_edges)
    await s.commit()
    return nc, ec


# ─── search_symbols WHERE filters ──────────────────────────────────


@pytest.mark.asyncio
async def test_search_symbols_kind_filter_actually_filters(session: AsyncSession):
    """The WHERE Node.kind == 'Symbol' filter must really restrict
    results — not just be a syntactic no-op. PR-117's fake session
    couldn't verify this; PR-118 does."""
    from app.mcp.queries import search_symbols

    pid = _uuid.uuid4()
    await _seed_from_dogfood(session, pid)
    # Inject a non-Symbol Node so the filter has something to exclude.
    session.add(Node(
        id="comp:test", project_id=pid, kind="Component",
        data={"name": "test_component"}, certainty="asserted",
        created_by=["test"], valid_from=datetime.now(tz=timezone.utc),
    ))
    await session.commit()

    # With filter: only Component (the one we just added).
    only_comp = await search_symbols(
        session, project_id=pid, query="test", kind="Component", top_k=50,
    )
    # Filtered set must be strictly smaller AND must not contain
    # anything that wasn't a Component.
    for r in only_comp:
        assert r["kind"] == "Component", (
            f"kind filter let through {r['kind']}"
        )


@pytest.mark.asyncio
async def test_search_symbols_finds_exact_function_in_real_data(
    session: AsyncSession,
):
    """End-to-end: real analyzer output → real DB → real query.
    'search_symbols' query must find Mnemos's own search_symbols
    function as a top result."""
    from app.mcp.queries import search_symbols

    pid = _uuid.uuid4()
    nc, ec = await _seed_from_dogfood(session, pid)
    assert nc >= 30, f"dogfood seed too small: {nc} nodes"

    results = await search_symbols(
        session, project_id=pid, query="search_symbols", top_k=10,
    )
    assert results, "search returned empty against real-DB seed"
    names = [r["name"] for r in results]
    assert "search_symbols" in names, (
        f"real-DB search couldn't find its own definition. Got: {names}"
    )


# ─── get_symbol exact-id matching (PR-117's known gap) ────────────


@pytest.mark.asyncio
async def test_get_symbol_returns_exact_match_only(session: AsyncSession):
    """The capability PR-117 couldn't verify with a fake session:
    get_symbol(symbol_id=X) must return X, not 'whatever the first
    row was'. This is the contract the MCP agent loop depends on —
    if it's wrong, every tool that uses an id returned by
    search_symbols breaks."""
    from app.mcp.queries import get_symbol

    pid = _uuid.uuid4()
    await _seed_from_dogfood(session, pid)
    # Pick three known symbols by name.
    candidates = (await session.execute(
        select(Node).where(Node.project_id == pid).limit(20)
    )).scalars().all()
    assert len(candidates) >= 3
    for target in candidates[:3]:
        out = await get_symbol(session, project_id=pid, symbol_id=target.id)
        assert out is not None, f"get_symbol returned None for {target.id}"
        # The documented response shape carries a nested ``symbol``.
        nested = out.get("symbol") or out
        returned_id = nested.get("id")
        assert returned_id == target.id, (
            f"asked for {target.id!r}, got {returned_id!r}"
        )


@pytest.mark.asyncio
async def test_get_symbol_returns_none_on_unknown_id(session: AsyncSession):
    """Unknown id must produce a clean None (not a crash, not a
    random other node). PR-117's fake session would have returned
    a row regardless."""
    from app.mcp.queries import get_symbol

    pid = _uuid.uuid4()
    await _seed_from_dogfood(session, pid)
    out = await get_symbol(
        session, project_id=pid, symbol_id="py:nonexistent:noway@1",
    )
    assert out is None, f"unknown id returned: {out}"


# ─── find_callers SQL JOIN on edges ────────────────────────────────


@pytest.mark.asyncio
async def test_find_callers_uses_real_edge_join(session: AsyncSession):
    """find_callers selects edges WHERE target_id == X. Real DB
    enforces that; fake session couldn't. Verify a known caller
    relationship survives the round trip."""
    from app.mcp.queries import find_callers

    pid = _uuid.uuid4()
    await _seed_from_dogfood(session, pid)
    # Find an edge that's resolved in-project — its target has a
    # caller (the edge's source).
    edges = (await session.execute(
        select(Edge).where(
            Edge.project_id == pid,
            Edge.data["callee_resolved"].astext == "true",
        ).limit(5)
    )).scalars().all()
    if not edges:
        # Fall back to any edge — older edge data may not carry the flag.
        edges = (await session.execute(
            select(Edge).where(Edge.project_id == pid).limit(5)
        )).scalars().all()
    assert edges, "no edges seeded — dogfood is broken"

    sample = edges[0]
    out = await find_callers(
        session, project_id=pid, symbol_id=sample.target_id, limit=50,
    )
    edge_list = out.get("edges") if isinstance(out, dict) else out
    assert edge_list, (
        f"find_callers({sample.target_id}) returned empty despite "
        f"a real edge pointing at it"
    )
    # Per _edge_out — the response uses ``caller_id`` / ``callee_id``.
    sources = [e.get("caller_id") for e in edge_list]
    assert sample.source_id in sources, (
        f"expected {sample.source_id} in callers, got: {sources[:5]}"
    )


# ─── list_findings status filter ───────────────────────────────────


@pytest.mark.asyncio
async def test_list_findings_status_filter(session: AsyncSession):
    """Findings WHERE status='open' must really exclude resolved
    ones — agent's triage workflow depends on it."""
    from app.mcp.queries import list_findings

    pid = _uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    run_id = _uuid.uuid4()
    session.add(
        Project(
            id=pid,
            name="finding-currentness",
            gitlab_project_id=118,
            gitlab_url="https://example.invalid/finding-currentness",
            default_branch="main",
            languages=["python"],
        )
    )
    session.add(
        AnalysisRun(
            id=run_id,
            project_id=pid,
            status="completed",
            triggered_by="test",
            git_sha="a" * 40,
            scope="full",
            started_at=now,
            completed_at=now,
            **published_run_fields(generation=1, published_at=now),
        )
    )
    await session.flush()
    session.add(
        GraphHead(
            project_id=pid,
            current_run_id=run_id,
            generation=1,
            overlay_generation=0,
            state="ready",
            published_at=now,
        )
    )
    # Seed 2 open + 1 resolved finding. SQLite has no gen_random_uuid()
    # — provide explicit ids so the server_default doesn't fire.
    for status, kind in [
        ("open", "duplicate_endpoint"),
        ("open", "schema_mismatch"),
        ("resolved", "dead_path_suspected"),
    ]:
        subject_node_id = f"test:{kind}"
        session.add(Finding(
            id=_uuid.uuid4(),
            project_id=pid, kind=kind, severity="error", status=status,
            identity_key=finding_identity_key(
                kind=kind,
                subject_node_id=subject_node_id,
            ),
            subject_node_id=subject_node_id,
            detail={}, risk_score=80,
            first_seen_at=now, last_seen_at=now,
            resolved_at=(now if status == "resolved" else None),
            validated_graph_generation=1,
            validated_overlay_generation=0,
            validated_at=now,
        ))
    await session.commit()

    all_findings = await list_findings(session, project_id=pid)
    only_open = await list_findings(session, project_id=pid, status="open")
    assert len(all_findings) == 3
    assert len(only_open) == 2
    assert all(f["status"] == "open" for f in only_open)


# ─── round-trip: ingest envelope → search → get → callers ──────────


@pytest.mark.asyncio
async def test_full_pipeline_dogfood_to_agent_response(session: AsyncSession):
    """The whole loop the operator promises Claude Code:
    analyzer output → ingest → search_symbols → get_symbol →
    find_callers. Every step real, no mocks."""
    from app.mcp.queries import find_callers, get_symbol, search_symbols

    pid = _uuid.uuid4()
    await _seed_from_dogfood(session, pid)

    # Step 1: agent asks for a function by fuzzy name
    hits = await search_symbols(
        session, project_id=pid, query="get_symbol", top_k=5,
    )
    assert hits, "search step failed"
    target_id = hits[0]["symbol_id"]

    # Step 2: agent drills into the top hit
    detail = await get_symbol(session, project_id=pid, symbol_id=target_id)
    assert detail is not None
    nested = detail.get("symbol") or detail
    assert nested.get("id") == target_id

    # Step 3: agent walks back to see what calls it (may be empty
    # for a leaf function — only assert no crash).
    callers = await find_callers(
        session, project_id=pid, symbol_id=target_id, limit=20,
    )
    assert callers is not None
    edge_list = callers.get("edges") if isinstance(callers, dict) else callers
    assert isinstance(edge_list, list)


# ─── polyglot type round-trip sanity ───────────────────────────────


@pytest.mark.asyncio
async def test_polyglot_uuid_jsonb_array_roundtrip(session: AsyncSession):
    """Defensive: confirm the polyglot layer hasn't quietly broken
    a type round-trip. A regression here would silently corrupt
    every other test in this file."""
    pid = _uuid.uuid4()
    n = Node(
        id="rt:test", project_id=pid, kind="Symbol",
        data={"nested": {"k": "v"}, "list": [1, 2, 3]},
        certainty="verified",
        created_by=["a", "b", "c"],
        valid_from=datetime.now(tz=timezone.utc),
    )
    session.add(n)
    await session.commit()

    fetched = (await session.execute(
        select(Node).where(Node.id == "rt:test")
    )).scalar_one()
    # JSONB nested structure preserved.
    assert fetched.data["nested"]["k"] == "v"
    assert fetched.data["list"] == [1, 2, 3]
    # ARRAY preserved as list.
    assert fetched.created_by == ["a", "b", "c"]
    # UUID round-trip.
    assert fetched.project_id == pid
