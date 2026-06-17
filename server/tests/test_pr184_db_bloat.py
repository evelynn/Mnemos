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

from app.merge.writer import prune_graph_history, upsert_edge, upsert_node

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
async def test_prune_drops_old_history_keeps_current_and_recent(s):
    from app.models.graph import Edge

    now = datetime.now(tz=timezone.utc)
    # current edge (valid_to IS NULL)
    await upsert_edge(s, project_id=PID, source_id="a", target_id="b",
                      kind="CALLS", data={}, certainty="asserted", source_name="g")
    # old superseded (20 days ago) — should be pruned
    s.add(Edge(project_id=PID, source_id="x", target_id="y", kind="CALLS",
               data={}, certainty="asserted", created_by=["g"],
               valid_from=now - timedelta(days=30), valid_to=now - timedelta(days=20)))
    # recent superseded (1 day ago) — should be kept
    s.add(Edge(project_id=PID, source_id="p", target_id="q", kind="CALLS",
               data={}, certainty="asserted", created_by=["g"],
               valid_from=now - timedelta(days=2), valid_to=now - timedelta(days=1)))
    await s.commit()

    res = await prune_graph_history(s, project_id=PID, keep_days=7)
    await s.commit()

    assert res["edges"] == 1  # only the 20-day-old superseded row
    assert (await s.execute(select(func.count()).select_from(Edge))).scalar() == 2
    # the current edge is untouched
    cur = (await s.execute(
        select(func.count()).select_from(Edge).where(Edge.valid_to.is_(None)))).scalar()
    assert cur == 1
