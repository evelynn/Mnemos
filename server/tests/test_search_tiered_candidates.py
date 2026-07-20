"""Tiered candidate selection for lexical symbol search.

The bounded candidate scan spends its cap on the best candidates first by
ordering rows by priority tier (exact name → name prefix → anything else
the filter matched) inside a single query. Regression guards: under cap
pressure an exact-name symbol must survive instead of being crowded out
by arbitrary substring rows; the pool must stay duplicate-free; and the
tiering must not cost extra database round trips.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import app.mcp.queries as queries
from app.mcp.queries import search_symbols
from app.models.graph import Node
from app.models.organization import Organization
from app.models.overlays import GraphNodeHumanOverlay
from app.models.projects import Project
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


async def _make_session_factory(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _symbol(project_id: uuid.UUID, node_id: str, name: str) -> Node:
    return Node(
        id=node_id,
        project_id=project_id,
        kind="Symbol",
        data={
            "name": name,
            "signature": f"def {name}():",
            "location": {"file": "src/module.py", "line": 1},
        },
        certainty="asserted",
        created_by=["ggoss-py"],
    )


@pytest.mark.asyncio
async def test_exact_match_survives_cap_pressure(monkeypatch):
    monkeypatch.setattr(queries, "_SEARCH_CANDIDATE_CAP", 10)

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = await _make_session_factory(engine)
    project_id = uuid.uuid4()
    async with Session() as session:
        # 30 substring-matching decoys whose ids sort before the exact
        # symbol — an unordered/id-ordered flat scan capped at 10 would
        # return only decoys.
        for index in range(30):
            session.add(
                _symbol(
                    project_id,
                    f"aaa:sub{index:02d}",
                    f"sub_target_helper_{index:02d}",
                )
            )
        session.add(_symbol(project_id, "zzz:target", "target"))
        await session.commit()

        results = await search_symbols(
            session, project_id=project_id, query="target", top_k=5
        )

    await engine.dispose()
    assert results, "search returned nothing"
    assert results[0]["symbol_id"] == "zzz:target", (
        "exact-name symbol was crowded out of the capped candidate pool"
    )


@pytest.mark.asyncio
async def test_tiering_costs_no_extra_queries():
    """Priority tiering must be an ORDER BY, not extra round trips.

    An earlier revision issued one query per tier; on PostgreSQL that
    meant three scans (two on unindexable expressions over ``data->>'name'``)
    where the untiered scan needed one.
    """
    import sqlalchemy as sa

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = await _make_session_factory(engine)
    project_id = uuid.uuid4()
    async with Session() as session:
        for index in range(10):
            session.add(
                _symbol(project_id, f"py:sym{index:02d}", f"target_{index:02d}")
            )
        session.add(_symbol(project_id, "py:exact", "target"))
        await session.commit()

        node_selects = 0

        def _count(conn, cursor, statement, parameters, context, executemany):
            nonlocal node_selects
            normalized = " ".join(statement.split()).lower()
            if normalized.startswith("select") and " from nodes" in normalized:
                node_selects += 1

        bind = session.get_bind()
        target = getattr(bind, "sync_engine", bind)
        sa.event.listen(target, "before_cursor_execute", _count)
        try:
            results = await search_symbols(
                session, project_id=project_id, query="target", top_k=5
            )
        finally:
            sa.event.remove(target, "before_cursor_execute", _count)

    await engine.dispose()
    assert results[0]["symbol_id"] == "py:exact"
    # One candidate scan; project_root_prefix adds its own separate query.
    assert node_selects == 2, (
        f"expected a single candidate scan (plus root-prefix), got {node_selects}"
    )


@pytest.mark.asyncio
async def test_tiered_pool_has_no_duplicates():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = await _make_session_factory(engine)
    project_id = uuid.uuid4()
    async with Session() as session:
        # Matches every tier at once: exact name, prefix, substring.
        session.add(_symbol(project_id, "py:retry", "retry"))
        session.add(_symbol(project_id, "py:retry_payment", "retry_payment"))
        await session.commit()

        results = await search_symbols(
            session, project_id=project_id, query="retry", top_k=20
        )

    await engine.dispose()
    ids = [row["symbol_id"] for row in results]
    assert len(ids) == len(set(ids)), f"duplicate symbols in results: {ids}"
    assert ids[0] == "py:retry"
    assert "py:retry_payment" in ids
