"""PR-184 — cut DB bloat: no redundant NodeSource write + history retention.

Self-contained in-memory SQLite (same polyglot layer serve_local uses), so it
runs in CI with no Postgres.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.merge.writer import (
    GraphHistoryPruneDisabled,
    prune_graph_history,
    upsert_edge,
    upsert_node,
)

PID = uuid.uuid4()


@pytest_asyncio.fixture
async def s():
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    from app.models import (  # noqa: F401
        audit, auth, comments, findings, graph, onboarding,
        organization, plans, projects, runtime, samples, stages,
    )
    from app.models.base import Base

    e = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with e.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(e, expire_on_commit=False)() as sess:
        yield sess
    await e.dispose()


@pytest.mark.asyncio
async def test_upsert_node_writes_no_node_source(s):
    from app.models.graph import Node, NodeSource

    await upsert_node(s, project_id=PID, node_id="n1", kind="Symbol",
                      data={"name": "f"}, certainty="asserted", source_name="ggoss")
    await s.commit()
    assert (await s.execute(select(func.count()).select_from(Node))).scalar() == 1
    # The 84MB-duplicate raw_data write is gone.
    assert (await s.execute(select(func.count()).select_from(NodeSource))).scalar() == 0
    # Provenance is still recorded — on the node itself.
    node = (await s.execute(select(Node))).scalar_one()
    assert node.created_by == ["ggoss"]


@pytest.mark.asyncio
async def test_semantic_node_noop_preserves_version_and_real_change_supersedes(s):
    from app.models.graph import Node

    await upsert_node(
        s, project_id=PID, node_id="sym:f", kind="Symbol",
        data={"name": "f", "location": {"line": 7, "file": "a.py"}},
        certainty="asserted", source_name="ggoss-py",
    )
    await s.commit()
    original = (await s.execute(select(Node).where(Node.id == "sym:f"))).scalar_one()
    original_valid_from = original.valid_from

    # Same JSON object with different mapping insertion order is still the
    # same source fact and must not produce a new bitemporal version.
    await upsert_node(
        s, project_id=PID, node_id="sym:f", kind="Symbol",
        data={"location": {"file": "a.py", "line": 7}, "name": "f"},
        certainty="asserted", source_name="ggoss-py",
    )
    await s.commit()
    rows = (await s.execute(
        select(Node).where(Node.project_id == PID, Node.id == "sym:f")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].valid_from == original_valid_from
    assert rows[0].valid_to is None

    # A meaningful payload change does create history.
    await upsert_node(
        s, project_id=PID, node_id="sym:f", kind="Symbol",
        data={"location": {"file": "a.py", "line": 8}, "name": "f"},
        certainty="asserted", source_name="ggoss-py",
    )
    await s.commit()
    rows = (await s.execute(
        select(Node).where(Node.project_id == PID, Node.id == "sym:f")
        .order_by(Node.valid_from)
    )).scalars().all()
    assert len(rows) == 2
    assert rows[0].valid_to is not None
    assert rows[1].valid_to is None
    assert rows[1].data["location"]["line"] == 8


@pytest.mark.asyncio
async def test_semantic_edge_noop_keeps_physical_uuid(s):
    from app.models.graph import Edge

    kwargs = {
        "project_id": PID,
        "source_id": "sym:a",
        "target_id": "sym:b",
        "kind": "CALLS",
        "data": {"site": {"line": 11, "file": "a.py"}},
        "certainty": "asserted",
        "source_name": "ggoss-py",
    }
    await upsert_edge(s, **kwargs)
    await s.commit()
    original = (await s.execute(select(Edge))).scalar_one()
    original_id = original.id
    original_valid_from = original.valid_from

    await upsert_edge(s, **kwargs)
    await s.commit()
    rows = (await s.execute(select(Edge))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == original_id
    assert rows[0].valid_from == original_valid_from

    # Optional/LLM-derived evidence must not replace a stronger deterministic
    # fact even when it happens to collide on the same logical edge identity.
    await upsert_edge(
        s,
        **{
            **kwargs,
            "data": {"site": {"line": 999, "file": "hostile.py"}},
            "certainty": "inferred",
            "source_name": "agent:python",
        },
    )
    await s.commit()
    rows = (await s.execute(select(Edge).order_by(Edge.valid_from))).scalars().all()
    assert len(rows) == 1
    assert rows[0].valid_to is None
    assert rows[0].id == original_id
    assert rows[0].data["site"]["file"] == "a.py"


@pytest.mark.asyncio
async def test_inferred_node_cannot_replace_asserted_node(s):
    from app.models.graph import Node

    await upsert_node(
        s,
        project_id=PID,
        node_id="deterministic:victim",
        kind="Symbol",
        data={"name": "victim", "file": "trusted.py"},
        certainty="asserted",
        source_name="ggoss-py",
    )
    await s.commit()

    await upsert_node(
        s,
        project_id=PID,
        node_id="deterministic:victim",
        kind="Symbol",
        data={"name": "replaced", "file": "hostile.py"},
        certainty="inferred",
        source_name="agent:python",
    )
    await s.commit()

    rows = (
        await s.execute(
            select(Node).where(
                Node.project_id == PID,
                Node.id == "deterministic:victim",
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].valid_to is None
    assert rows[0].data == {"name": "victim", "file": "trusted.py"}
    assert rows[0].created_by == ["ggoss-py"]


@pytest.mark.asyncio
async def test_history_prune_fails_closed_until_retention_is_observable(s):
    from app.models.graph import Edge

    now = datetime.now(tz=timezone.utc)
    # current edge (valid_to IS NULL)
    await upsert_edge(s, project_id=PID, source_id="a", target_id="b",
                      kind="CALLS", data={}, certainty="asserted", source_name="g")
    # Old superseded history must remain available to compare_runs until a
    # retained-from watermark/history revision can make deletion observable.
    s.add(Edge(project_id=PID, source_id="x", target_id="y", kind="CALLS",
               data={}, certainty="asserted", created_by=["g"],
               valid_from=now - timedelta(days=30), valid_to=now - timedelta(days=20)))
    # recent superseded (1 day ago) — should be kept
    s.add(Edge(project_id=PID, source_id="p", target_id="q", kind="CALLS",
               data={}, certainty="asserted", created_by=["g"],
               valid_from=now - timedelta(days=2), valid_to=now - timedelta(days=1)))
    await s.commit()

    with pytest.raises(GraphHistoryPruneDisabled):
        await prune_graph_history(s, project_id=PID, keep_days=7)

    assert (await s.execute(select(func.count()).select_from(Edge))).scalar() == 3
    # the current edge is untouched
    cur = (await s.execute(
        select(func.count()).select_from(Edge).where(Edge.valid_to.is_(None)))).scalar()
    assert cur == 1
