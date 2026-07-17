"""Summary candidate selection has a database-independent total order."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.extractor.runner import _priority_symbols
from app.models.graph import Edge, Node
from app.testing.sqlite_polyglot import install_polyglot


@pytest.mark.asyncio
async def test_priority_symbol_entry_degree_and_filler_ties_are_total_order() -> None:
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)

    entry_project = uuid.uuid4()
    degree_project = uuid.uuid4()
    filler_project = uuid.uuid4()
    async with AsyncSession(engine, expire_on_commit=False) as session:
        # Insert in reverse order so insertion/row-return order cannot satisfy
        # the assertions accidentally.
        session.add_all(
            [
                Node(
                    id="sym:z-entry",
                    project_id=entry_project,
                    kind="Symbol",
                    data={"is_entry_point": True},
                    certainty="asserted",
                    created_by=["test"],
                ),
                Node(
                    id="sym:a-entry",
                    project_id=entry_project,
                    kind="Symbol",
                    data={"is_entry_point": True},
                    certainty="asserted",
                    created_by=["test"],
                ),
            ]
        )
        for node_id in ("sym:z-target", "sym:a-target", "sym:caller"):
            session.add(
                Node(
                    id=node_id,
                    project_id=degree_project,
                    kind="Symbol",
                    data={},
                    certainty="asserted",
                    created_by=["test"],
                )
            )
        session.add_all(
            [
                Edge(
                    project_id=degree_project,
                    source_id="sym:caller",
                    target_id="sym:z-target",
                    kind="CALLS",
                    data={},
                    certainty="asserted",
                    created_by=["test"],
                ),
                Edge(
                    project_id=degree_project,
                    source_id="sym:caller",
                    target_id="sym:a-target",
                    kind="CALLS",
                    data={},
                    certainty="asserted",
                    created_by=["test"],
                ),
            ]
        )
        for node_id in ("sym:z-filler", "sym:a-filler"):
            session.add(
                Node(
                    id=node_id,
                    project_id=filler_project,
                    kind="Symbol",
                    data={},
                    certainty="asserted",
                    created_by=["test"],
                )
            )
        await session.commit()

        entries = await _priority_symbols(session, entry_project, 2)
        degree = await _priority_symbols(session, degree_project, 2)
        filler = await _priority_symbols(session, filler_project, 2)

    await engine.dispose()
    assert [node.id for node in entries] == ["sym:a-entry", "sym:z-entry"]
    assert [node.id for node in degree] == ["sym:a-target", "sym:z-target"]
    assert [node.id for node in filler] == ["sym:a-filler", "sym:z-filler"]
