"""PR-200 — fresh-graph write fast path (throughput ①).

Measured (docs/04-eval/throughput-analysis-2026-07-08.md): a large first-pass
analysis is ~93% DB-ingest, dominated by a supersede-UPDATE per record that
matches ZERO rows on an empty graph. ``upsert_*`` gains ``skip_close``; the
ingest path sets it only when the graph started empty AND the id hasn't been
written this run (seen-sets). These tests pin the correctness that makes the
skip safe — especially the within-run duplicate (a contract node both a Java
handler and an HTML form emit) still ending as exactly one current version.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr200")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import Edge, Node  # noqa: E402
from app.orchestrator.jobs import _graph_is_empty, _record_payload  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


@pytest.fixture()
async def session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


def _totals() -> dict[str, int]:
    return {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0, "errors": 0}


def _sym(node_id: str, source: str) -> dict:
    return {
        "record_type": "symbol", "source_name": source,
        "data": {"id": node_id, "name": node_id, "certainty": "asserted"},
    }


def _contract(source: str) -> dict:
    return {
        "record_type": "contract", "source_name": source,
        "data": {"id": "x", "kind": "http_endpoint",
                 "spec": {"method": "GET", "path": "/owners"},
                 "certainty": "inferred"},
    }


async def _current(session, model, **where):
    stmt = select(model).where(model.valid_to.is_(None))
    for k, v in where.items():
        stmt = stmt.where(getattr(model, k) == v)
    return (await session.execute(stmt)).scalars().all()


@pytest.mark.asyncio
async def test_graph_is_empty_detects_fresh(session):
    pid = uuid.uuid4()
    assert await _graph_is_empty(session, pid) is True
    session.add(Node(id="n1", project_id=pid, kind="Symbol", data={},
                     certainty="asserted", created_by=["t"]))
    await session.commit()
    assert await _graph_is_empty(session, pid) is False


@pytest.mark.asyncio
async def test_fresh_distinct_ids_all_current_none_superseded(session):
    pid = uuid.uuid4()
    seen_nodes: set[str] = set()
    for i in range(3):
        await _record_payload(
            session, project_id=pid, payload=_sym(f"sym:{i}", "ggoss-py"),
            accept_kinds={"symbol", "data_entity"}, totals=_totals(),
            fresh=True, seen_nodes=seen_nodes, seen_edges=set(),
        )
    await session.commit()
    current = await _current(session, Node)
    allrows = (await session.execute(select(Node))).scalars().all()
    assert len(current) == 3
    # Fast path: nothing was superseded (no wasted close).
    assert len(allrows) == 3


@pytest.mark.asyncio
async def test_within_run_duplicate_id_ends_one_current(session):
    """The load-bearing case: the SAME normalized contract node id emitted by
    two sources in one fresh run must supersede — not leave two current rows."""
    import asyncio

    pid = uuid.uuid4()
    seen_nodes: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    # Java handler exposes GET /owners …
    await _record_payload(
        session, project_id=pid, payload=_contract("ggoss-java"),
        accept_kinds={"contract", "edge"}, totals=_totals(),
        fresh=True, seen_nodes=seen_nodes, seen_edges=seen_edges,
    )
    # In production the second emission is a separate analyzer STAGE (its own
    # subprocess + commit), so ``valid_from`` differs — the bitemporal PK is
    # (id, project_id, valid_from). Advance the clock a touch to model that;
    # within one stage analyzers dedup, so the same id at the same instant
    # never occurs.
    await asyncio.sleep(0.002)
    # … and an HTML form points at the SAME endpoint node.
    await _record_payload(
        session, project_id=pid, payload=_contract("ggoss-web"),
        accept_kinds={"contract", "edge"}, totals=_totals(),
        fresh=True, seen_nodes=seen_nodes, seen_edges=seen_edges,
    )
    await session.commit()
    cid = "http.GET./owners"
    current = await _current(session, Node, id=cid)
    allrows = (await session.execute(
        select(Node).where(Node.id == cid))).scalars().all()
    assert len(current) == 1, "exactly one current version"
    assert len(allrows) == 2, "first was superseded, not duplicated"


@pytest.mark.asyncio
async def test_reanalysis_non_fresh_identical_payload_is_noop(session):
    """Unchanged re-analysis keeps the original bitemporal version stable."""
    pid = uuid.uuid4()
    # First (fresh) write.
    await _record_payload(
        session, project_id=pid, payload=_sym("sym:a", "ggoss-py"),
        accept_kinds={"symbol", "data_entity"}, totals=_totals(),
        fresh=True, seen_nodes=set(), seen_edges=set(),
    )
    await session.commit()
    assert await _graph_is_empty(session, pid) is False
    # Re-analysis: fresh=False → normal upsert supersedes the old row.
    await _record_payload(
        session, project_id=pid, payload=_sym("sym:a", "ggoss-py"),
        accept_kinds={"symbol", "data_entity"}, totals=_totals(),
        fresh=False,
    )
    await session.commit()
    current = await _current(session, Node, id="sym:a")
    allrows = (await session.execute(
        select(Node).where(Node.id == "sym:a"))).scalars().all()
    assert len(current) == 1
    assert len(allrows) == 1


@pytest.mark.asyncio
async def test_fresh_edge_duplicate_ends_one_current(session):
    pid = uuid.uuid4()
    seen_edges: set[tuple[str, str, str]] = set()
    edge = {
        "record_type": "edge", "source_name": "ggoss-cpp",
        "data": {"source_id": "a", "target_id": "b", "kind": "CALLS",
                 "certainty": "asserted"},
    }
    import asyncio
    for i in range(2):
        if i:
            await asyncio.sleep(0.002)  # models the separate-stage time gap
        await _record_payload(
            session, project_id=pid, payload=edge, accept_kinds={"edge"},
            totals=_totals(), fresh=True, seen_nodes=set(), seen_edges=seen_edges,
        )
    await session.commit()
    current = await _current(session, Edge)
    allrows = (await session.execute(select(Edge))).scalars().all()
    assert len(current) == 1
    assert len(allrows) == 1, "same logical edge must not create history churn"
