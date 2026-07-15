"""Atomic graph publication contracts on real SQLite transactions.

These tests exercise the same ORM models and stage/promotion primitives used
by the Phase-B ``run_ingest`` path in local mode.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "graph-publication-phase-a-test-key")

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
from app.graph_publication import (  # noqa: E402
    ActiveStagingCleanupRejected,
    GraphCoverageAlreadySealed,
    GraphCoverageNotSealed,
    GraphGenerationChanged,
    GraphHeadNeedsRebuild,
    GraphPublicationInvariantError,
    IncompleteFullRebuildCoverage,
    MultiProducerContributionRequired,
    StagedIdentityConflict,
    StaleGraphGeneration,
    bootstrap_graph_head,
    capture_graph_base_generation,
    cleanup_staged_graph,
    edge_semantic_hash,
    node_semantic_hash,
    promote_staged_graph,
    read_graph_stamp,
    revalidate_graph_stamp,
    seal_graph_coverage,
    stage_node,
)
from app.models.base import Base  # noqa: E402
from app.models.graph import (  # noqa: E402
    AnalysisRun,
    Edge,
    GraphEdgeStage,
    GraphHead,
    GraphNodeStage,
    Node,
)
from app.models.projects import Project  # noqa: E402
from app.source_snapshot import find_unpublished_refresh  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402

install_polyglot()


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


async def _project(session_factory, *, state: str = "ready", generation: int = 1):
    project_id = uuid.uuid4()
    base_run_id = uuid.uuid4()
    published_at = datetime(2026, 7, 14, 1, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="atomic-publication",
                gitlab_project_id=81001,
                gitlab_url="https://example.invalid/atomic-publication",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=base_run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:baseline",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at - timedelta(minutes=1),
                completed_at=published_at,
                **published_run_fields(
                    generation=generation,
                    published_at=published_at,
                    stats={"baseline": True},
                ),
            )
        )
        # GraphHead.current_run_id is deletion-restricted and FK-enforced in
        # SQLite. Flush the referenced run before inserting the head because
        # these mappers intentionally have no ORM relationship dependency.
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=(base_run_id if state == "ready" else None),
                generation=generation,
                state=state,
                published_at=(published_at if state == "ready" else None),
            )
        )
        await session.commit()
    return project_id, base_run_id, published_at


async def _run(
    session_factory,
    project_id: uuid.UUID,
    *,
    scope: str = "incremental",
    status: str = "running",
) -> uuid.UUID:
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status=status,
                triggered_by="test:phase-a",
                git_sha="b" * 40,
                scope=scope,
                started_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.flush()
        await capture_graph_base_generation(
            session, project_id=project_id, run_id=run_id
        )
        await session.commit()
    return run_id


async def _seal(
    session,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    authoritative_sources: set[str] | frozenset[str] = frozenset(),
) -> None:
    await seal_graph_coverage(
        session,
        project_id=project_id,
        run_id=run_id,
        authoritative_sources=authoritative_sources,
    )
    await session.commit()


@pytest.mark.asyncio
async def test_stage_writer_unions_identical_owners_and_rejects_conflict(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    data = {"id": "py:shared", "name": "shared"}

    async with session_factory() as session:
        assert await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:shared",
            kind="Symbol",
            data=data,
            certainty="asserted",
            source_name="ggoss-py",
        )
        assert await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:shared",
            kind="Symbol",
            data=data,
            certainty="asserted",
            source_name="ggoss-ts",
        )
        assert not await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:shared",
            kind="Symbol",
            data=data,
            certainty="asserted",
            source_name="ggoss-py",
        )
        await session.commit()

    async with session_factory() as session:
        staged = await session.get(GraphNodeStage, (run_id, "py:shared"))
        assert staged is not None
        assert staged.source_name == "ggoss-py"
        assert staged.source_names == ["ggoss-py", "ggoss-ts"]
        with pytest.raises(StagedIdentityConflict):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="py:shared",
                kind="Symbol",
                data={**data, "name": "conflict"},
                certainty="asserted",
                source_name="ggoss-java",
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_coverage_seal_is_a_barrier_against_late_stage_writes(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        await _seal(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
    async with session_factory() as session:
        with pytest.raises(GraphCoverageAlreadySealed):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="py:late",
                kind="Symbol",
                data={"id": "py:late", "name": "late"},
                certainty="asserted",
                source_name="ggoss-py",
            )


@pytest.mark.asyncio
async def test_same_transaction_seal_invalidates_stage_writer_cache(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:before-seal",
            kind="Symbol",
            data={"id": "py:before-seal", "name": "before"},
            certainty="asserted",
            source_name="ggoss-py",
        )
        await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
        with pytest.raises(GraphCoverageAlreadySealed):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="py:after-seal",
                kind="Symbol",
                data={"id": "py:after-seal", "name": "after"},
                certainty="asserted",
                source_name="ggoss-py",
            )


@pytest.mark.asyncio
async def test_stage_lock_cache_tracks_transaction_object_across_commits(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:first-transaction",
            kind="Symbol",
            data={"id": "py:first-transaction", "name": "first"},
            certainty="asserted",
            source_name="ggoss-py",
        )
        first_token = session.info["mnemos_graph_stage_lock"]
        assert first_token[0] is session.get_transaction()
        await session.commit()

        await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:second-transaction",
            kind="Symbol",
            data={"id": "py:second-transaction", "name": "second"},
            certainty="asserted",
            source_name="ggoss-py",
        )
        second_token = session.info["mnemos_graph_stage_lock"]
        assert second_token[0] is session.get_transaction()
        assert second_token[0] is not first_token[0]
        await session.commit()


@pytest.mark.asyncio
async def test_database_rejects_duplicate_current_node_identity(database):
    _, session_factory = database
    project_id, _, published_at = await _project(session_factory)
    async with session_factory() as session:
        session.add_all(
            [
                Node(
                    id="py:duplicate",
                    project_id=project_id,
                    kind="Symbol",
                    data={"id": "py:duplicate", "version": 1},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=published_at + timedelta(seconds=1),
                ),
                Node(
                    id="py:duplicate",
                    project_id=project_id,
                    kind="Symbol",
                    data={"id": "py:duplicate", "version": 2},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=published_at + timedelta(seconds=2),
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.commit()


def _staged_node(
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    node_id: str,
    name: str,
    source_name: str = "ggoss-py",
) -> GraphNodeStage:
    data = {"id": node_id, "name": name, "language": "python"}
    return GraphNodeStage(
        run_id=run_id,
        project_id=project_id,
        node_id=node_id,
        kind="Symbol",
        data=data,
        certainty="asserted",
        source_name=source_name,
        semantic_hash=node_semantic_hash(
            kind="Symbol",
            data=data,
            certainty="asserted",
            created_by=[source_name],
        ),
    )


def _staged_edge(
    *,
    run_id: uuid.UUID,
    project_id: uuid.UUID,
    source_id: str,
    target_id: str,
    source_name: str = "ggoss-py",
) -> GraphEdgeStage:
    data = {"call_site": {"file": "m.py", "line": 2}}
    return GraphEdgeStage(
        run_id=run_id,
        project_id=project_id,
        source_id=source_id,
        target_id=target_id,
        kind="CALLS",
        data=data,
        certainty="asserted",
        source_name=source_name,
        semantic_hash=edge_semantic_hash(
            source_id=source_id,
            target_id=target_id,
            kind="CALLS",
            data=data,
            certainty="asserted",
            created_by=[source_name],
        ),
    )


@pytest.mark.asyncio
async def test_bootstrap_is_fail_closed_and_only_staged_full_can_make_ready(database):
    _, session_factory = database
    project_id = uuid.uuid4()
    legacy_at = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="new-project",
                gitlab_project_id=81002,
                gitlab_url="https://example.invalid/new-project",
                default_branch="main",
                languages=["python"],
            )
        )
        await session.flush()
        head = await bootstrap_graph_head(session, project_id=project_id)
        assert head.state == "needs_rebuild"
        assert head.current_run_id is None
        session.add(
            Node(
                id="py:legacy.py::stale",
                project_id=project_id,
                kind="Symbol",
                data={"id": "py:legacy.py::stale", "name": "stale"},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=legacy_at,
            )
        )
        await session.commit()

    incremental_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            _staged_node(
                run_id=incremental_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="f",
            )
        )
        await session.commit()
        await _seal(
            session, project_id=project_id, run_id=incremental_id
        )
        with pytest.raises(GraphHeadNeedsRebuild):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=incremental_id,
                allow_full_rebuild=True,
            )

    uncovered_full_id = await _run(session_factory, project_id, scope="full")
    async with session_factory() as session:
        session.add(
            _staged_node(
                run_id=uncovered_full_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="f",
                source_name="ggoss-ts",
            )
        )
        await session.commit()
        await _seal(
            session,
            project_id=project_id,
            run_id=uncovered_full_id,
            authoritative_sources={"ggoss-ts"},
        )
        with pytest.raises(IncompleteFullRebuildCoverage, match="ggoss-py"):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=uncovered_full_id,
                allow_full_rebuild=True,
            )

    full_id = await _run(session_factory, project_id, scope="full")
    async with session_factory() as session:
        session.add(
            _staged_node(
                run_id=full_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="f",
            )
        )
        await session.commit()
        await _seal(
            session,
            project_id=project_id,
            run_id=full_id,
            authoritative_sources={"ggoss-py"},
        )
        result = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=full_id,
            allow_full_rebuild=True,
        )
        assert result.generation == 1

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        assert head is not None
        assert head.state == "ready"
        assert head.current_run_id == full_id
        stale = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:legacy.py::stale",
                )
            )
        ).scalar_one()
        assert stale.valid_to is not None


@pytest.mark.asyncio
async def test_graph_head_shape_is_enforced_by_database(database):
    _, session_factory = database
    project_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="invalid-head-shape",
                gitlab_project_id=81003,
                gitlab_url="https://example.invalid/invalid-head-shape",
                default_branch="main",
                languages=["python"],
            )
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=None,
                generation=1,
                state="ready",
                published_at=datetime.now(tz=timezone.utc),
            )
        )
        with pytest.raises(IntegrityError, match="ck_graph_heads_shape"):
            await session.commit()

    async with session_factory() as session:
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=None,
                generation=1,
                state="needs_rebuild",
                published_at=None,
            )
        )
        with pytest.raises(IntegrityError, match="ck_graph_heads_shape"):
            await session.commit()


@pytest.mark.asyncio
async def test_sqlite_enforces_publication_foreign_keys(database):
    _, session_factory = database
    project_id, current_run_id, _ = await _project(session_factory)
    staged_run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        assert int((await session.execute(text("PRAGMA foreign_keys"))).scalar_one()) == 1
        session.add(
            _staged_node(
                run_id=staged_run_id,
                project_id=project_id,
                node_id="py:m.py::cascade",
                name="cascade",
            )
        )
        await session.commit()

        with pytest.raises(IntegrityError):
            await session.execute(
                delete(AnalysisRun).where(AnalysisRun.id == current_run_id)
            )
            await session.commit()
        await session.rollback()
        assert await session.get(GraphHead, project_id) is not None

        await session.execute(
            delete(AnalysisRun).where(AnalysisRun.id == staged_run_id)
        )
        await session.commit()
        assert (
            await session.execute(
                select(GraphNodeStage).where(GraphNodeStage.run_id == staged_run_id)
            )
        ).scalar_one_or_none() is None

        await session.execute(delete(Project).where(Project.id == project_id))
        await session.commit()
        assert await session.get(GraphHead, project_id) is None


@pytest.mark.asyncio
async def test_unpublished_guard_detects_overlapping_and_active_writers(database):
    _, session_factory = database
    project_id, _, baseline_at = await _project(session_factory)
    failed_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=failed_id,
                project_id=project_id,
                status="failed",
                triggered_by="test:overlap",
                git_sha="e" * 40,
                scope="full",
                started_at=baseline_at - timedelta(minutes=10),
                completed_at=baseline_at + timedelta(minutes=1),
                error_log="failed after overlapping the baseline",
            )
        )
        await session.commit()
        unsafe = await find_unpublished_refresh(session, project_id=project_id)
        assert unsafe is not None
        assert unsafe.run_id == failed_id

    repair_at = baseline_at + timedelta(minutes=2)
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=uuid.uuid4(),
                project_id=project_id,
                status="completed",
                triggered_by="test:repair",
                git_sha="f" * 40,
                scope="full",
                started_at=baseline_at + timedelta(minutes=1),
                completed_at=repair_at,
            )
        )
        await session.commit()
        assert await find_unpublished_refresh(session, project_id=project_id) is None

        running_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=running_id,
                project_id=project_id,
                status="running",
                triggered_by="test:active-overlap",
                git_sha="0" * 40,
                scope="full",
                started_at=baseline_at - timedelta(minutes=20),
            )
        )
        await session.commit()
        unsafe = await find_unpublished_refresh(session, project_id=project_id)
        assert unsafe is not None
        assert unsafe.run_id == running_id


@pytest.mark.asyncio
async def test_success_swaps_head_versions_and_run_in_one_commit(database):
    _, session_factory = database
    project_id, _, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    old_edge_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Node(
                id="py:m.py::f",
                project_id=project_id,
                kind="Symbol",
                data={"id": "py:m.py::f", "name": "old"},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=baseline_at,
            )
        )
        session.add(
            Edge(
                id=old_edge_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::old",
                kind="CALLS",
                data={},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=baseline_at,
            )
        )
        session.add(
            _staged_node(
                run_id=run_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="new",
            )
        )
        session.add(
            _staged_edge(
                run_id=run_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::new",
            )
        )
        await session.commit()
        await _seal(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
        result = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
            final_stats={"coverage": {"status": "complete"}},
            requested_publish_at=baseline_at,  # must become strictly monotonic
            cleanup_stage_on_success=False,
        )

    assert result.generation == 2
    assert result.published_at > baseline_at
    assert result.counts.nodes_inserted == 1
    assert result.counts.nodes_closed == 1
    assert result.counts.edges_inserted == 1
    assert result.counts.stale_edges_closed == 1

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        run = await session.get(AnalysisRun, run_id)
        current_node = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:m.py::f",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        old_edge = await session.get(
            Edge, {"id": old_edge_id, "valid_from": baseline_at}
        )
        assert head is not None and run is not None and old_edge is not None
        assert head.current_run_id == run_id
        assert head.generation == 2
        assert run.completed_at is None
        stored_published_at = head.published_at
        if stored_published_at.tzinfo is None:  # SQLite adapter behaviour
            stored_published_at = stored_published_at.replace(tzinfo=timezone.utc)
        assert stored_published_at == result.published_at
        assert run.status == "published"
        assert run.stats["graph_publication"]["atomic"] is True
        assert run.stats["graph_publication"]["published_at"] == result.published_at.isoformat()
        assert run.stats["graph_publication"]["counts"]["nodes_inserted"] == 1
        started_at = run.started_at
        sealed_at = run.graph_coverage_sealed_at
        assert started_at is not None and sealed_at is not None
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if sealed_at.tzinfo is None:
            sealed_at = sealed_at.replace(tzinfo=timezone.utc)
        assert result.published_at >= started_at
        assert result.published_at >= sealed_at
        assert current_node.data["name"] == "new"
        old_edge_valid_to = old_edge.valid_to
        if old_edge_valid_to.tzinfo is None:  # SQLite adapter behaviour
            old_edge_valid_to = old_edge_valid_to.replace(tzinfo=timezone.utc)
        assert old_edge_valid_to == result.published_at

    # A worker crash after DB commit but before SSE/audit is a harmless retry.
    async with session_factory() as session:
        retry = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
        )
        assert retry.idempotent is True
        assert retry.generation == 2
        assert retry.published_at == result.published_at
        assert retry.counts == result.counts

    # Whole-run postprocess may later close the published source receipt as
    # partial. Promotion retry still resolves from the head/receipt and never
    # republishes or zeroes the original counts.
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.status = "partial"
        run.completed_at = result.published_at + timedelta(seconds=1)
        await session.commit()
    async with session_factory() as session:
        terminal_retry = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
        )
        assert terminal_retry.idempotent is True
        assert terminal_retry.counts == result.counts

    # Even the idempotent path revalidates the persisted coverage seal against
    # the committed publication receipt instead of trusting current_run_id.
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.graph_authoritative_sources = ["ggoss-py", "spoofed-producer"]
        await session.commit()
    async with session_factory() as session:
        with pytest.raises(
            GraphPublicationInvariantError,
            match="authoritative sources",
        ):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )


@pytest.mark.asyncio
async def test_failure_after_head_cas_rolls_back_head_graph_and_run(database, monkeypatch):
    from app import graph_publication

    _, session_factory = database
    project_id, base_run_id, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            Node(
                id="py:m.py::f",
                project_id=project_id,
                kind="Symbol",
                data={"id": "py:m.py::f", "name": "old"},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=baseline_at,
            )
        )
        session.add(
            _staged_node(
                run_id=run_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="new",
            )
        )
        session.add(
            _staged_edge(
                run_id=run_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::g",
            )
        )
        await session.commit()

    async def fail_edges(*_args, **_kwargs):
        raise RuntimeError("injected promotion failure")

    monkeypatch.setattr(graph_publication, "_promote_edges", fail_edges)
    async with session_factory() as session:
        await _seal(session, project_id=project_id, run_id=run_id)
        with pytest.raises(RuntimeError, match="injected promotion failure"):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        run = await session.get(AnalysisRun, run_id)
        current_nodes = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:m.py::f",
                    Node.valid_to.is_(None),
                )
            )
        ).scalars().all()
        staged_nodes = int(
            (
                await session.execute(
                    select(func.count()).select_from(GraphNodeStage).where(
                        GraphNodeStage.run_id == run_id
                    )
                )
            ).scalar_one()
        )
        assert head is not None and run is not None
        assert head.current_run_id == base_run_id
        assert head.generation == 1
        assert run.status == "running"
        assert run.completed_at is None
        assert len(current_nodes) == 1
        assert current_nodes[0].data["name"] == "old"
        assert current_nodes[0].valid_to is None
        assert staged_nodes == 1


@pytest.mark.asyncio
async def test_same_base_generation_allows_only_one_publication(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    first_id = await _run(session_factory, project_id)
    second_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            _staged_node(
                run_id=first_id,
                project_id=project_id,
                node_id="py:first.py::f",
                name="first",
            )
        )
        session.add(
            _staged_node(
                run_id=second_id,
                project_id=project_id,
                node_id="py:second.py::f",
                name="second",
            )
        )
        await session.commit()
        await _seal(session, project_id=project_id, run_id=first_id)
        await _seal(session, project_id=project_id, run_id=second_id)
        first = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=first_id,
        )
        assert first.generation == 2

    async with session_factory() as session:
        # The loser cannot silently rebase its already-staged facts to the
        # winner's new head generation.
        assert await capture_graph_base_generation(
            session, project_id=project_id, run_id=second_id
        ) == 1
        await session.commit()
        with pytest.raises(StaleGraphGeneration):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=second_id,
            )

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        second = await session.get(AnalysisRun, second_id)
        assert head is not None and second is not None
        assert head.current_run_id == first_id
        assert second.status == "running"
        assert (
            await session.execute(
                select(GraphNodeStage).where(GraphNodeStage.run_id == second_id)
            )
        ).scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_semantic_noop_and_certainty_downgrade_do_not_churn_history(database):
    _, session_factory = database
    project_id, _, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    stable_data = {
        "id": "py:m.py::stable",
        "name": "stable",
        "language": "python",
    }
    trusted_data = {
        "id": "py:m.py::trusted",
        "name": "trusted",
        "language": "python",
    }
    inferred_data = {**trusted_data, "name": "model-guess"}
    async with session_factory() as session:
        session.add_all(
            [
                Node(
                    id="py:m.py::stable",
                    project_id=project_id,
                    kind="Symbol",
                    data=stable_data,
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=baseline_at,
                ),
                Node(
                    id="py:m.py::trusted",
                    project_id=project_id,
                    kind="Symbol",
                    data=trusted_data,
                    certainty="verified",
                    created_by=["runtime"],
                    valid_from=baseline_at,
                ),
                GraphNodeStage(
                    run_id=run_id,
                    project_id=project_id,
                    node_id="py:m.py::stable",
                    kind="Symbol",
                    data=stable_data,
                    certainty="asserted",
                    source_name="ggoss-py",
                    semantic_hash=node_semantic_hash(
                        kind="Symbol",
                        data=stable_data,
                        certainty="asserted",
                        created_by=["ggoss-py"],
                    ),
                ),
                GraphNodeStage(
                    run_id=run_id,
                    project_id=project_id,
                    node_id="py:m.py::trusted",
                    kind="Symbol",
                    data=inferred_data,
                    certainty="inferred",
                    source_name="agent:python",
                    semantic_hash=node_semantic_hash(
                        kind="Symbol",
                        data=inferred_data,
                        certainty="inferred",
                        created_by=["agent:python"],
                    ),
                ),
            ]
        )
        await session.commit()
        await _seal(session, project_id=project_id, run_id=run_id)
        result = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
        )
        assert result.counts.nodes_unchanged == 1
        assert result.counts.nodes_downgrade_ignored == 1
        assert result.counts.nodes_inserted == 0
        assert result.counts.nodes_closed == 0

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Node)
                .where(Node.project_id == project_id)
                .order_by(Node.id)
            )
        ).scalars().all()
        assert len(rows) == 2
        assert all(row.valid_from == baseline_at.replace(tzinfo=None) for row in rows)
        trusted = next(row for row in rows if row.id == "py:m.py::trusted")
        assert trusted.certainty == "verified"
        assert trusted.data["name"] == "trusted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "current_owners",
    [
        ["ggoss-ts"],
        ["ggoss-py", "ggoss-ts"],
    ],
)
async def test_changed_fact_with_unrefreshed_owners_fails_closed(
    database, current_owners
):
    _, session_factory = database
    project_id, base_run_id, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            Node(
                id="contract:GET /orders",
                project_id=project_id,
                kind="Contract",
                data={"name": "GET /orders", "version": 1},
                certainty="asserted",
                created_by=current_owners,
                valid_from=baseline_at,
            )
        )
        changed = {"name": "GET /orders", "version": 2}
        session.add(
            GraphNodeStage(
                run_id=run_id,
                project_id=project_id,
                node_id="contract:GET /orders",
                kind="Contract",
                data=changed,
                certainty="asserted",
                source_name="ggoss-py",
                semantic_hash=node_semantic_hash(
                    kind="Contract",
                    data=changed,
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
            )
        )
        await session.commit()
        await _seal(session, project_id=project_id, run_id=run_id)
        with pytest.raises(MultiProducerContributionRequired):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        run = await session.get(AnalysisRun, run_id)
        current = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "contract:GET /orders",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        assert head is not None and run is not None
        assert head.current_run_id == base_run_id
        assert head.generation == 1
        assert run.status == "running"
        assert current.created_by == current_owners
        assert current.data["version"] == 1


@pytest.mark.asyncio
async def test_changed_edge_with_unrefreshed_owner_fails_closed(database):
    _, session_factory = database
    project_id, base_run_id, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    edge_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Edge(
                id=edge_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::g",
                kind="CALLS",
                data={"call_site": {"file": "old.py", "line": 1}},
                certainty="asserted",
                created_by=["ggoss-ts"],
                valid_from=baseline_at,
            )
        )
        session.add(
            _staged_edge(
                run_id=run_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::g",
                source_name="ggoss-py",
            )
        )
        await session.commit()
        await _seal(session, project_id=project_id, run_id=run_id)
        with pytest.raises(MultiProducerContributionRequired):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        run = await session.get(AnalysisRun, run_id)
        current = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.source_id == "py:m.py::f",
                    Edge.target_id == "py:m.py::g",
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one()
        assert head is not None and run is not None
        assert head.current_run_id == base_run_id
        assert run.status == "running"
        assert current.id == edge_id
        assert current.created_by == ["ggoss-ts"]
        assert current.data["call_site"]["file"] == "old.py"


@pytest.mark.asyncio
async def test_complete_sealed_coverage_can_replace_a_multi_owner_fact(database):
    _, session_factory = database
    project_id, _, baseline_at = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            Node(
                id="contract:GET /orders",
                project_id=project_id,
                kind="Contract",
                data={"name": "GET /orders", "version": 1},
                certainty="asserted",
                created_by=["ggoss-py", "ggoss-ts"],
                valid_from=baseline_at,
            )
        )
        changed = {"name": "GET /orders", "version": 2}
        session.add(
            GraphNodeStage(
                run_id=run_id,
                project_id=project_id,
                node_id="contract:GET /orders",
                kind="Contract",
                data=changed,
                certainty="asserted",
                source_name="ggoss-py",
                semantic_hash=node_semantic_hash(
                    kind="Contract",
                    data=changed,
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
            )
        )
        await session.commit()
        await _seal(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py", "ggoss-ts"},
        )
        result = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
        )
        assert result.counts.nodes_closed == 1
        assert result.counts.nodes_inserted == 1

    async with session_factory() as session:
        current = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "contract:GET /orders",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        assert current.created_by == ["ggoss-py"]
        assert current.data["version"] == 2


@pytest.mark.asyncio
async def test_coverage_seal_is_idempotent_but_cannot_be_changed(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        assert await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-ts", "ggoss-py"},
        ) == ("ggoss-py", "ggoss-ts")
        await session.commit()
        assert await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py", "ggoss-ts"},
        ) == ("ggoss-py", "ggoss-ts")
        await session.commit()
        with pytest.raises(GraphCoverageAlreadySealed):
            await seal_graph_coverage(
                session,
                project_id=project_id,
                run_id=run_id,
                authoritative_sources={"ggoss-py"},
            )


@pytest.mark.asyncio
async def test_promotion_rejects_unsealed_coverage(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        with pytest.raises(GraphCoverageNotSealed):
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )


@pytest.mark.asyncio
async def test_base_generation_must_be_captured_before_staging(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="running",
                triggered_by="test:late-base-capture",
                git_sha="d" * 40,
                scope="incremental",
                started_at=datetime.now(tz=timezone.utc),
            )
        )
        session.add(
            _staged_node(
                run_id=run_id,
                project_id=project_id,
                node_id="py:m.py::late",
                name="late",
            )
        )
        await session.commit()
        with pytest.raises(GraphPublicationInvariantError, match="before the first"):
            await capture_graph_base_generation(
                session,
                project_id=project_id,
                run_id=run_id,
            )


@pytest.mark.asyncio
async def test_graph_read_stamp_detects_a_generation_change(database):
    _, session_factory = database
    project_id, _, baseline_at = await _project(session_factory)
    next_run_id = uuid.uuid4()
    next_published_at = baseline_at + timedelta(microseconds=1)
    async with session_factory() as reader:
        stamp = await read_graph_stamp(reader, project_id=project_id)
        await revalidate_graph_stamp(reader, stamp=stamp)
        await reader.rollback()

        async with session_factory() as writer:
            writer.add(
                AnalysisRun(
                    id=next_run_id,
                    project_id=project_id,
                    status="completed",
                    triggered_by="test:reader-stamp",
                    git_sha="c" * 40,
                    scope="full",
                    started_at=baseline_at,
                    completed_at=next_published_at,
                    **published_run_fields(
                        generation=2,
                        published_at=next_published_at,
                    ),
                )
            )
            head = await writer.get(GraphHead, project_id)
            assert head is not None
            head.current_run_id = next_run_id
            head.generation = 2
            head.published_at = next_published_at
            await writer.commit()

        with pytest.raises(GraphGenerationChanged):
            await revalidate_graph_stamp(reader, stamp=stamp)


@pytest.mark.asyncio
async def test_cleanup_refuses_active_run_then_removes_terminal_stage(database):
    _, session_factory = database
    project_id, _, _ = await _project(session_factory)
    run_id = await _run(session_factory, project_id)
    async with session_factory() as session:
        session.add(
            _staged_node(
                run_id=run_id,
                project_id=project_id,
                node_id="py:m.py::f",
                name="f",
            )
        )
        session.add(
            _staged_edge(
                run_id=run_id,
                project_id=project_id,
                source_id="py:m.py::f",
                target_id="py:m.py::g",
            )
        )
        await session.commit()
        with pytest.raises(ActiveStagingCleanupRejected):
            await cleanup_staged_graph(
                session, project_id=project_id, run_id=run_id
            )
        await session.rollback()

        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.status = "failed"
        run.completed_at = datetime.now(tz=timezone.utc)
        await session.commit()

        cleanup = await cleanup_staged_graph(
            session, project_id=project_id, run_id=run_id
        )
        assert cleanup.nodes_deleted == 1
        assert cleanup.edges_deleted == 1
        await session.commit()

    async with session_factory() as session:
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(GraphNodeStage).where(
                        GraphNodeStage.run_id == run_id
                    )
                )
            ).scalar_one()
        ) == 0
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(GraphEdgeStage).where(
                        GraphEdgeStage.run_id == run_id
                    )
                )
            ).scalar_one()
        ) == 0
