"""PR-147 — trace_flow auto-trigger: gather flow files from the graph.

Instead of the operator naming the FE/BE/DB files, /trace_flow/auto
resolves an entry point to the involved files via the knowledge graph
(symbols matching the entry across any tier, plus their one-hop
CALLS/READS/WRITES neighbours — which reach the SQL-schema DataEntity).

The gather logic is graph-backed, so this test seeds a tiny self-contained
SQLite graph and asserts the resolver pulls FE + BE (name match) and the
DB schema file (via a WRITES edge).
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr147")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

import uuid

import pytest


def test_auto_route_and_request_model():
    from app.api.flow import TraceFlowAutoRequest, router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/v1/projects/{project_id}/trace_flow/auto" in paths
    m = TraceFlowAutoRequest(entry="x", source_root="/tmp")
    assert m.max_files == 8 and m.persist is True
    assert m.source_root == "/tmp"  # accepted only for compatibility; endpoint ignores it
    # Auto resolution uses the latest completed graph/snapshot pair; selecting
    # an older snapshot while querying the current graph would mix revisions.
    assert "analysis_run_id" not in TraceFlowAutoRequest.model_fields


@pytest.mark.asyncio
async def test_gather_files_from_graph_spans_tiers():
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api.flow import _gather_files_from_graph
    from app.models.base import Base
    from app.models.graph import Edge, Node
    from app.models.organization import Organization  # noqa: F401
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)

    pid = uuid.uuid4()
    async with AsyncSession(eng, expire_on_commit=False) as s:
        s.add_all([
            Node(id="js:frontend/checkout.js::placeOrder", project_id=pid, kind="Symbol",
                 data={"name": "placeOrder", "location": {"file": "frontend/checkout.js"}}, certainty="inferred", created_by="test"),
            Node(id="py:backend/orders.py::handle_create_order", project_id=pid, kind="Symbol",
                 data={"name": "handle_create_order", "location": {"file": "backend/orders.py"}}, certainty="inferred", created_by="test"),
            Node(id="data:orders", project_id=pid, kind="DataEntity",
                 data={"name": "orders", "location": {"file": "db/schema.sql"}}, certainty="inferred", created_by="test"),
            Node(id="py:other::unrelated", project_id=pid, kind="Symbol",
                 data={"name": "unrelated", "location": {"file": "backend/other.py"}}, certainty="inferred", created_by="test"),
        ])
        # backend handler WRITES the orders table (one hop -> pulls the DB file)
        s.add(Edge(project_id=pid, source_id="py:backend/orders.py::handle_create_order",
                   target_id="data:orders", kind="WRITES", data={}, certainty="inferred",
                   created_by="test"))
        await s.commit()

        files = await _gather_files_from_graph(s, pid, "place order", max_files=10)

    # FE + BE matched by name; DB file reached via the WRITES edge.
    assert "frontend/checkout.js" in files
    assert "backend/orders.py" in files
    assert "db/schema.sql" in files
    # the unrelated symbol (no name/edge match) is not pulled in
    assert "backend/other.py" not in files
