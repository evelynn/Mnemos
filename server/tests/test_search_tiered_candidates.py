"""Tiered candidate selection for lexical symbol search.

The bounded candidate scan is filled by priority tier (exact name →
name prefix → unanchored substring). Regression guard: under cap
pressure an exact-name symbol must be guaranteed into the pool instead
of being crowded out by arbitrary substring rows, and the pool must
stay duplicate-free.
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
    monkeypatch.setattr(queries, "_SEARCH_EXACT_TIER_CAP", 5)
    monkeypatch.setattr(queries, "_SEARCH_PREFIX_TIER_CAP", 5)

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
