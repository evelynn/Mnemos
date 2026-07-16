"""Focused contracts for bounded run-scoped graph staging batches."""

from __future__ import annotations

import os
import uuid
from dataclasses import FrozenInstanceError

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "graph-publication-batching-test-key")

from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

from app import graph_publication  # noqa: E402
from app.graph_publication import (  # noqa: E402
    GraphCoverageAlreadySealed,
    StagedEdgeCandidate,
    StagedIdentityConflict,
    StagedNodeCandidate,
    edge_semantic_hash,
    node_semantic_hash,
    seal_graph_coverage,
    stage_edges_batch,
    stage_nodes_batch,
)
from app.models import (  # noqa: E402,F401
    audit,
    auth,
    comments,
    findings,
    graph,
    onboarding,
    organization,
    plans,
    projects,
    runtime,
    samples,
    stages,
)
from app.models.base import Base  # noqa: E402
from app.models.graph import (  # noqa: E402
    AnalysisRun,
    GraphEdgeStage,
    GraphNodeStage,
)
from app.models.projects import Project  # noqa: E402


@pytest_asyncio.fixture
async def stage_database():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            Project(
                id=project_id,
                name="staging-batch",
                gitlab_project_id=36001,
                gitlab_url="https://example.invalid/staging-batch",
                default_branch="main",
                languages=["python", "typescript"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="running",
                triggered_by="test:batch",
                git_sha="a" * 40,
                scope="incremental",
                graph_base_generation=1,
            )
        )
        await session.commit()
    try:
        yield engine, sessions, project_id, run_id
    finally:
        await engine.dispose()


def test_candidates_are_frozen_payload_snapshots() -> None:
    node_payload = {"name": "before", "nested": {"items": [1, 2]}}
    edge_payload = {"call_site": {"line": 7}}
    node = StagedNodeCandidate(
        node_id="py:m.py::f",
        kind="Symbol",
        data=node_payload,
        certainty="asserted",
        source_name="ggoss-py",
    )
    edge = StagedEdgeCandidate(
        source_id="py:m.py::f",
        target_id="py:m.py::g",
        kind="CALLS",
        data=edge_payload,
        certainty="asserted",
        source_name="ggoss-py",
    )

    node_payload["name"] = "after"
    node_payload["nested"]["items"].append(3)
    edge_payload["call_site"]["line"] = 99
    returned = node.data
    returned["nested"]["items"].append(4)

    assert node.data == {"name": "before", "nested": {"items": [1, 2]}}
    assert edge.data == {"call_site": {"line": 7}}
    with pytest.raises(FrozenInstanceError):
        node.node_id = "py:changed"  # type: ignore[misc]


@pytest.mark.asyncio
async def test_node_batch_reads_identities_once_and_reduces_in_input_order(
    stage_database,
):
    engine, sessions, project_id, run_id = stage_database
    stage_selects = 0

    def count_stage_selects(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal stage_selects
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and "from graph_node_stage" in normalized:
            stage_selects += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_stage_selects)
    try:
        async with sessions() as session:
            changed = await stage_nodes_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedNodeCandidate(
                        node_id="py:shared",
                        kind="Symbol",
                        data={
                            "id": "py:shared",
                            "name": "shared",
                            "human_confirmation": {"action": "confirm"},
                        },
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:shared",
                        kind="Symbol",
                        data={"id": "py:shared", "name": "shared"},
                        certainty="asserted",
                        source_name="ggoss-ts",
                    ),
                    StagedNodeCandidate(
                        node_id="py:shared",
                        kind="Symbol",
                        data={"id": "py:shared", "name": "shared"},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:trusted",
                        kind="Symbol",
                        data={"id": "py:trusted", "version": 1},
                        certainty="verified",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:trusted",
                        kind="Symbol",
                        data={"id": "py:trusted", "version": 2},
                        certainty="inferred",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:updated",
                        kind="Symbol",
                        data={"id": "py:updated", "version": 1},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:updated",
                        kind="Symbol",
                        data={"id": "py:updated", "version": 2},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                ),
            )
            assert changed == (True, True, False, True, False, True, True)
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_stage_selects)

    assert stage_selects == 1
    async with sessions() as session:
        shared = await session.get(GraphNodeStage, (run_id, "py:shared"))
        trusted = await session.get(GraphNodeStage, (run_id, "py:trusted"))
        updated = await session.get(GraphNodeStage, (run_id, "py:updated"))
        assert shared is not None and trusted is not None and updated is not None
        assert shared.data == {"id": "py:shared", "name": "shared"}
        assert shared.source_name == "ggoss-py"
        assert shared.source_names == ["ggoss-py", "ggoss-ts"]
        assert shared.semantic_hash == node_semantic_hash(
            kind="Symbol",
            data=shared.data,
            certainty="asserted",
            created_by=["ggoss-py", "ggoss-ts"],
        )
        assert trusted.certainty == "verified"
        assert trusted.data["version"] == 1
        assert updated.data["version"] == 2


@pytest.mark.asyncio
async def test_node_batch_conflict_rolls_back_all_pending_candidates(stage_database):
    _, sessions, project_id, run_id = stage_database
    async with sessions() as session:
        with pytest.raises(StagedIdentityConflict):
            await stage_nodes_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedNodeCandidate(
                        node_id="py:conflict",
                        kind="Symbol",
                        data={"version": 1},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedNodeCandidate(
                        node_id="py:conflict",
                        kind="Symbol",
                        data={"version": 2},
                        certainty="asserted",
                        source_name="ggoss-ts",
                    ),
                ),
            )
        await session.rollback()

    async with sessions() as session:
        count = (
            await session.execute(
                select(func.count()).select_from(GraphNodeStage).where(
                    GraphNodeStage.run_id == run_id
                )
            )
        ).scalar_one()
        assert count == 0


@pytest.mark.asyncio
async def test_edge_batch_preserves_overlay_hash_owner_and_downgrade_rules(
    stage_database,
):
    engine, sessions, project_id, run_id = stage_database
    stage_selects = 0

    def count_stage_selects(
        _connection, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        nonlocal stage_selects
        normalized = " ".join(statement.lower().split())
        if normalized.startswith("select") and "from graph_edge_stage" in normalized:
            stage_selects += 1

    event.listen(engine.sync_engine, "before_cursor_execute", count_stage_selects)
    try:
        async with sessions() as session:
            changed = await stage_edges_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedEdgeCandidate(
                        source_id="py:a",
                        target_id="py:b",
                        kind="CALLS",
                        data={
                            "call_site": {"line": 4},
                            "human_confirmation": {"action": "confirm"},
                            "exercised": True,
                            "hit_count": 9,
                        },
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedEdgeCandidate(
                        source_id="py:a",
                        target_id="py:b",
                        kind="CALLS",
                        data={"call_site": {"line": 4}},
                        certainty="asserted",
                        source_name="ggoss-ts",
                    ),
                    StagedEdgeCandidate(
                        source_id="py:a",
                        target_id="py:b",
                        kind="CALLS",
                        data={"call_site": {"line": 4}},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedEdgeCandidate(
                        source_id="py:trusted",
                        target_id="py:sink",
                        kind="CALLS",
                        data={"version": 1},
                        certainty="verified",
                        source_name="ggoss-py",
                    ),
                    StagedEdgeCandidate(
                        source_id="py:trusted",
                        target_id="py:sink",
                        kind="CALLS",
                        data={"version": 2},
                        certainty="inferred",
                        source_name="ggoss-py",
                    ),
                ),
            )
            assert changed == (True, True, False, True, False)
            await session.commit()
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_stage_selects)

    assert stage_selects == 1
    async with sessions() as session:
        shared = await session.get(
            GraphEdgeStage, (run_id, "py:a", "py:b", "CALLS")
        )
        trusted = await session.get(
            GraphEdgeStage, (run_id, "py:trusted", "py:sink", "CALLS")
        )
        assert shared is not None and trusted is not None
        assert shared.data == {"call_site": {"line": 4}}
        assert shared.source_names == ["ggoss-py", "ggoss-ts"]
        assert shared.semantic_hash == edge_semantic_hash(
            source_id="py:a",
            target_id="py:b",
            kind="CALLS",
            data=shared.data,
            certainty="asserted",
            created_by=["ggoss-py", "ggoss-ts"],
        )
        assert trusted.certainty == "verified"
        assert trusted.data == {"version": 1}

    async with sessions() as session:
        with pytest.raises(StagedIdentityConflict):
            await stage_edges_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedEdgeCandidate(
                        source_id="py:x",
                        target_id="py:y",
                        kind="CALLS",
                        data={"version": 1},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                    StagedEdgeCandidate(
                        source_id="py:x",
                        target_id="py:y",
                        kind="CALLS",
                        data={"version": 2},
                        certainty="asserted",
                        source_name="ggoss-ts",
                    ),
                ),
            )
        await session.rollback()
    async with sessions() as session:
        assert await session.get(
            GraphEdgeStage, (run_id, "py:x", "py:y", "CALLS")
        ) is None


@pytest.mark.asyncio
async def test_batch_bounds_are_checked_before_locking(stage_database):
    _, sessions, project_id, run_id = stage_database
    async with sessions() as session:
        assert await stage_nodes_batch(
            session,
            project_id=project_id,
            run_id=run_id,
            candidates=(),
        ) == ()
        assert not session.in_transaction()
        with pytest.raises(ValueError, match="node staging batch exceeds 250"):
            await stage_nodes_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedNodeCandidate(
                        node_id=f"py:n{i}",
                        kind="Symbol",
                        data={"i": i},
                        certainty="asserted",
                        source_name="ggoss-py",
                    )
                    for i in range(251)
                ),
            )
        assert not session.in_transaction()
        with pytest.raises(ValueError, match="edge staging batch exceeds 150"):
            await stage_edges_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedEdgeCandidate(
                        source_id=f"py:s{i}",
                        target_id=f"py:t{i}",
                        kind="CALLS",
                        data={},
                        certainty="asserted",
                        source_name="ggoss-py",
                    )
                    for i in range(151)
                ),
            )
        assert not session.in_transaction()


@pytest.mark.asyncio
async def test_batches_fail_closed_after_coverage_seal(stage_database):
    _, sessions, project_id, run_id = stage_database
    async with sessions() as session:
        await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
        await session.commit()

    async with sessions() as session:
        with pytest.raises(GraphCoverageAlreadySealed):
            await stage_nodes_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedNodeCandidate(
                        node_id="py:late",
                        kind="Symbol",
                        data={},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                ),
            )
        await session.rollback()
        with pytest.raises(GraphCoverageAlreadySealed):
            await stage_edges_batch(
                session,
                project_id=project_id,
                run_id=run_id,
                candidates=(
                    StagedEdgeCandidate(
                        source_id="py:late",
                        target_id="py:sink",
                        kind="CALLS",
                        data={},
                        certainty="asserted",
                        source_name="ggoss-py",
                    ),
                ),
            )


@pytest.mark.asyncio
async def test_singleton_stage_wrappers_delegate_to_batch(monkeypatch):
    node_calls = []
    edge_calls = []

    async def fake_nodes(session, **kwargs):
        node_calls.append((session, kwargs))
        return (False,)

    async def fake_edges(session, **kwargs):
        edge_calls.append((session, kwargs))
        return (True,)

    monkeypatch.setattr(graph_publication, "stage_nodes_batch", fake_nodes)
    monkeypatch.setattr(graph_publication, "stage_edges_batch", fake_edges)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    session = object()

    assert not await graph_publication.stage_node(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        run_id=run_id,
        node_id="py:a",
        kind="Symbol",
        data={"name": "a"},
        certainty="asserted",
        source_name="ggoss-py",
    )
    assert await graph_publication.stage_edge(
        session,  # type: ignore[arg-type]
        project_id=project_id,
        run_id=run_id,
        source_id="py:a",
        target_id="py:b",
        kind="CALLS",
        data={"line": 1},
        certainty="asserted",
        source_name="ggoss-py",
    )

    node_candidate = tuple(node_calls[0][1]["candidates"])[0]
    edge_candidate = tuple(edge_calls[0][1]["candidates"])[0]
    assert isinstance(node_candidate, StagedNodeCandidate)
    assert node_candidate.data == {"name": "a"}
    assert isinstance(edge_candidate, StagedEdgeCandidate)
    assert edge_candidate.data == {"line": 1}
