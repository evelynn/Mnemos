"""PR-196 — fix: node-subject findings never got the OTLP exercised boost.

Root cause: ``merge/runtime.py`` reconcile marks *edges* exercised from OTLP
traces (``Edge.data.exercised = "true"``), but ``findings._subject_is_exercised``
read ``Node.data.exercised`` — a flag the runtime path never sets. So the 1.3×
"live production path" risk multiplier silently never fired for node-subject
findings derived from real traces (memory-tracked real bug).

Fix: a symbol counts as exercised when the node flag is set OR any CALLS edge
incident to it is exercised — aligning the read with where runtime writes it
and with ``queries.impact_analysis``.
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr196")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.merge.findings import _subject_is_exercised, _upsert_finding  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import Edge, Node  # noqa: E402
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


async def _node(s, pid, node_id, data):
    s.add(Node(id=node_id, project_id=pid, kind="Symbol", data=data,
               certainty="asserted", created_by=["test"]))


async def _calls_edge(s, pid, src, tgt, exercised):
    s.add(Edge(
        id=uuid.uuid4(), project_id=pid, source_id=src, target_id=tgt,
        kind="CALLS", data={"exercised": "true"} if exercised else {},
        certainty="asserted", created_by=["test"],
    ))


@pytest.mark.asyncio
async def test_node_exercised_via_incident_edge_the_bug(session):
    """The regression: runtime marks the EDGE exercised, node flag is unset —
    the subject must still count as exercised (was False = the bug)."""
    pid = uuid.uuid4()
    await _node(session, pid, "sym:orders:createOrder", {"name": "createOrder"})
    await _node(session, pid, "sym:orders:validate", {"name": "validate"})
    # Production trace exercised the call createOrder -> validate.
    await _calls_edge(session, pid, "sym:orders:createOrder", "sym:orders:validate", True)
    await session.commit()

    # Both endpoints of the exercised edge are on a live path.
    assert await _subject_is_exercised(session, pid, "sym:orders:createOrder") is True
    assert await _subject_is_exercised(session, pid, "sym:orders:validate") is True


@pytest.mark.asyncio
async def test_node_flag_still_honored_backcompat(session):
    """seed_demo / direct marking sets Node.data.exercised — must still work."""
    pid = uuid.uuid4()
    await _node(session, pid, "sym:a", {"name": "a", "exercised": "true"})
    await session.commit()
    assert await _subject_is_exercised(session, pid, "sym:a") is True


@pytest.mark.asyncio
async def test_not_exercised_without_flag_or_exercised_edge(session):
    """No node flag and only a non-exercised edge → not exercised."""
    pid = uuid.uuid4()
    await _node(session, pid, "sym:x", {"name": "x"})
    await _node(session, pid, "sym:y", {"name": "y"})
    await _calls_edge(session, pid, "sym:x", "sym:y", False)
    await session.commit()
    assert await _subject_is_exercised(session, pid, "sym:x") is False
    assert await _subject_is_exercised(session, pid, "sym:none") is False


@pytest.mark.asyncio
async def test_finding_on_edge_exercised_node_scores_higher(session):
    """End-to-end: the same finding on a node with an exercised incident edge
    outranks one on a dead-path node — the 1.3× boost now actually applies."""
    pid = uuid.uuid4()
    await _node(session, pid, "sym:live", {"name": "live"})
    await _node(session, pid, "sym:live_callee", {"name": "callee"})
    await _node(session, pid, "sym:dead", {"name": "dead"})
    await _node(session, pid, "sym:dead_callee", {"name": "dcallee"})
    await _calls_edge(session, pid, "sym:live", "sym:live_callee", True)
    await _calls_edge(session, pid, "sym:dead", "sym:dead_callee", False)
    await session.commit()

    await _upsert_finding(
        session, project_id=pid, kind="unverified_claim", severity="warning",
        detail={}, subject_node_id="sym:live",
    )
    await _upsert_finding(
        session, project_id=pid, kind="unverified_claim", severity="warning",
        detail={}, subject_node_id="sym:dead",
    )
    await session.commit()

    from sqlalchemy import select
    findings = {
        f.subject_node_id: f
        for f in (await session.execute(select(Finding))).scalars().all()
    }
    # Same blast radius (1 edge each), same severity — only the exercised
    # flag differs, so the live one must score strictly higher.
    assert findings["sym:live"].risk_score > findings["sym:dead"].risk_score


@pytest.mark.asyncio
async def test_graph_view_node_badge_reflects_incident_exercised_edge(session):
    """The same root cause hit the graph visualizer: a node's exercised
    badge read the node flag (never set by runtime) so every symbol showed
    as dead. It must now reflect an incident exercised edge."""
    from app.api.analysis import graph_component_map

    pid = uuid.uuid4()
    await _node(session, pid, "sym:g_a", {"name": "a"})
    await _node(session, pid, "sym:g_b", {"name": "b"})
    await _calls_edge(session, pid, "sym:g_a", "sym:g_b", True)
    await session.commit()

    out = await graph_component_map(pid, None, db=session)
    nodes = {n["id"]: n for n in out["nodes"]}
    assert nodes["sym:g_a"]["exercised"] is True
    assert nodes["sym:g_b"]["exercised"] is True
    assert out["edges"][0]["exercised"] is True
