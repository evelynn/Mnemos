from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.models.graph import Edge, Node
from app.orchestrator.jobs import _close_missing_deterministic_facts
from app.orchestrator.seen_index import SeenFactIndex
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


def test_seen_index_spills_and_removes_private_temp_file():
    index = SeenFactIndex(memory_limit=1_000, flush_size=100)
    path = None
    try:
        for number in range(1_005):
            assert index.add_node(f"symbol:{number}") is False
        for number in range(25):
            index.add_edge((f"symbol:{number}", f"symbol:{number + 1}", "CALLS"))

        assert index.spilled is True
        assert index.fast_path_enabled is False
        assert index.contains_nodes(["symbol:0", "symbol:1004", "missing"]) == {
            "symbol:0",
            "symbol:1004",
        }
        assert index.contains_edges(
            [("symbol:0", "symbol:1", "CALLS"), ("x", "y", "CALLS")]
        ) == {("symbol:0", "symbol:1", "CALLS")}
        stats = index.stats()
        assert stats["storage"] == "disk"
        assert stats["nodes"] == 1_005
        path = index._path  # run-private implementation detail asserted for cleanup
        assert path is not None and path.is_file()
    finally:
        index.close()
    assert path is not None and not path.exists()


@pytest.mark.asyncio
async def test_deletion_sweep_is_paged_and_uses_spilled_membership():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    valid_from = datetime.now(tz=timezone.utc)
    async with Session() as session:
        session.add_all(
            Node(
                id=f"symbol:{number:04d}",
                project_id=project_id,
                kind="Symbol",
                data={"id": f"symbol:{number:04d}"},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=valid_from,
            )
            for number in range(1_005)
        )
        await session.commit()

    seen = SeenFactIndex(memory_limit=1_000, flush_size=100)
    try:
        # First and last identities are intentionally omitted.  Crossing two
        # page boundaries catches whole-table materialisation/keyset mistakes.
        for number in range(1, 1_004):
            seen.add_node(f"symbol:{number:04d}")
        async with Session() as session:
            closed = await _close_missing_deterministic_facts(
                session,
                project_id=project_id,
                source_names={"ggoss-py"},
                seen_nodes=seen,
                seen_edges=seen,
            )
            await session.commit()
            current = int(
                (
                    await session.execute(
                        select(func.count()).select_from(Node).where(
                            Node.project_id == project_id,
                            Node.valid_to.is_(None),
                        )
                    )
                ).scalar_one()
            )
        assert closed == {"nodes": 2, "edges": 0}
        assert current == 1_003
    finally:
        seen.close()
        await engine.dispose()
