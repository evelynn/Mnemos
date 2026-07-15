"""Independent red-team coverage for Phase-B graph publication.

The implementation tests in ``test_graph_publication_phase_a.py`` verify the
publication primitives in isolation.  This file deliberately attacks the
cross-layer contract instead:

* ``run_ingest`` may commit run-scoped staging while the published Node/Edge
  snapshot and GraphHead remain byte-for-byte unchanged;
* failed and cooperatively-cancelled refreshes leave the prior graph readable;
* a successful promotion is invisible until commit and retry-idempotent;
* the coverage seal is a write fence, including within one SQLAlchemy session;
* identical multi-producer contributions retain a monotonic owner union while
  conflicting payloads fail closed;
* project/run provenance and one-current-identity rules are database invariants;
* the first default incremental request becomes a truthful full rebuild.

SQLite exercises the portable E2 path with independent file-backed
connections.  When ``DATABASE_URL`` (or ``MNEMOS_TEST_POSTGRES_URL``) selects
PostgreSQL, an isolated temporary schema also exercises real row-lock and MVCC
behaviour with independent connections.  No production schema is reused.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "graph-publication-phase-b-redteam-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

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
from app.analyzers.runner import RunRecord  # noqa: E402
from app.graph_overlays import record_human_confirmation  # noqa: E402
from app.graph_publication import (  # noqa: E402
    GraphCoverageAlreadySealed,
    StagedIdentityConflict,
    capture_graph_base_generation,
    node_semantic_hash,
    promote_staged_graph,
    read_graph_stamp,
    seal_graph_coverage,
    stage_edge,
    stage_node,
)
from app.models.base import Base  # noqa: E402
from app.models.findings import Summary  # noqa: E402
from app.models.graph import (  # noqa: E402
    AnalysisRun,
    Edge,
    GraphEdgeStage,
    GraphHead,
    GraphNodeStage,
    Node,
)
from app.models.projects import Project  # noqa: E402
from app.orchestrator.source_checkout import PreparedSource  # noqa: E402
from app.summary_freshness import lock_ready_summary_generation  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, _run_id: uuid.UUID, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest_asyncio.fixture
async def database(tmp_path: Path):
    """A WAL-backed SQLite database so readers use independent connections."""

    database_path = tmp_path / "phase-b-redteam.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


async def _seed_ready_project(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    languages: list[str] | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, datetime]:
    """Create a ready generation plus one queued refresh and live graph."""

    project_id = uuid.uuid4()
    base_run_id = uuid.uuid4()
    run_id = uuid.uuid4()
    published_at = datetime(2026, 7, 15, 1, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name=f"phase-b-{project_id.hex[:8]}",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/phase-b-redteam",
                default_branch="main",
                languages=languages or ["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=base_run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:redteam-baseline",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at - timedelta(minutes=1),
                completed_at=published_at,
                graph_base_generation=0,
                graph_authoritative_sources=["ggoss-py"],
                graph_coverage_sealed_at=published_at,
                stats={
                    "baseline": True,
                    "graph_publication": {
                        "contract_version": "mnemos.graph-publication.v1",
                        "generation": 1,
                        "base_generation": 0,
                        "published_at": published_at.isoformat(),
                        "authoritative_sources": ["ggoss-py"],
                        "coverage_sealed_at": published_at.isoformat(),
                        "counts": {
                            "nodes_inserted": 1,
                            "nodes_closed": 0,
                            "nodes_unchanged": 0,
                            "nodes_downgrade_ignored": 0,
                            "edges_inserted": 1,
                            "edges_closed": 0,
                            "edges_unchanged": 0,
                            "edges_downgrade_ignored": 0,
                            "stale_nodes_closed": 0,
                            "stale_edges_closed": 0,
                        },
                        "atomic": True,
                    },
                },
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test:redteam-refresh",
                git_sha="b" * 40,
                scope="full",
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=base_run_id,
                generation=1,
                state="ready",
                published_at=published_at,
            )
        )
        session.add(
            Node(
                id="py:old.py::old",
                project_id=project_id,
                kind="Symbol",
                data={"id": "py:old.py::old", "name": "old"},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=published_at,
            )
        )
        session.add(
            Edge(
                id=uuid.uuid4(),
                project_id=project_id,
                source_id="py:old.py::old",
                target_id="py:old.py::old",
                kind="CALLS",
                data={"call_site": {"file": "old.py", "line": 1}},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=published_at,
            )
        )
        await session.commit()
    return project_id, base_run_id, run_id, published_at


async def _make_running(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> None:
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.status = "running"
        run.started_at = datetime.now(tz=timezone.utc)
        await capture_graph_base_generation(
            session, project_id=project_id, run_id=run_id
        )
        await session.commit()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


async def _published_snapshot(
    session_factory: async_sessionmaker[AsyncSession], project_id: uuid.UUID
) -> tuple[str, Any]:
    """Return a stable digest plus the publication read stamp."""

    async with session_factory() as session:
        head = await session.get(GraphHead, project_id)
        assert head is not None
        stamp = await read_graph_stamp(session, project_id=project_id)
        nodes = (
            await session.execute(
                select(Node)
                .where(Node.project_id == project_id, Node.valid_to.is_(None))
                .order_by(Node.id)
            )
        ).scalars().all()
        edges = (
            await session.execute(
                select(Edge)
                .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
                .order_by(Edge.source_id, Edge.target_id, Edge.kind)
            )
        ).scalars().all()
        payload = {
            "head": {
                "generation": head.generation,
                "current_run_id": str(head.current_run_id),
                "published_at": str(head.published_at),
            },
            "nodes": [
                {
                    "id": row.id,
                    "kind": row.kind,
                    "data": row.data,
                    "certainty": row.certainty,
                    "created_by": row.created_by,
                }
                for row in nodes
            ],
            "edges": [
                {
                    "source_id": row.source_id,
                    "target_id": row.target_id,
                    "kind": row.kind,
                    "data": row.data,
                    "certainty": row.certainty,
                    "created_by": row.created_by,
                }
                for row in edges
            ],
        }
    return hashlib.sha256(_canonical(payload).encode()).hexdigest(), stamp


async def _wire_jobs(
    monkeypatch: pytest.MonkeyPatch,
    session_factory: async_sessionmaker[AsyncSession],
    source_path: Path,
):
    from app.orchestrator import jobs, stages as stage_module

    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(stage_module, "SessionLocal", session_factory)

    async def _acquired(_project_id: uuid.UUID, _run_id: uuid.UUID) -> bool:
        return True

    async def _released(_project_id: uuid.UUID, _run_id: uuid.UUID) -> None:
        return None

    async def _audit(**_kwargs: Any) -> None:
        return None

    async def _findings(_session: AsyncSession, _project_id: uuid.UUID) -> dict:
        return {}

    async def _prepared(*_args: Any, **_kwargs: Any) -> PreparedSource:
        return PreparedSource(
            path=source_path,
            authoritative_root=True,
            root_id=f"redteam:{source_path.name}",
            source_kind="manual_directory",
            mutable=False,
        )

    monkeypatch.setattr(jobs, "_acquire_project_lock", _acquired)
    monkeypatch.setattr(jobs, "_release_project_lock", _released)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "prepare_source", _prepared)
    monkeypatch.setattr(jobs, "analyzer_available", lambda language: language == "python")
    return jobs


class _BlockingRunner:
    """Commit one stage batch, then fail or await cooperative cancellation."""

    def __init__(
        self,
        *,
        staged: asyncio.Event,
        resume: asyncio.Event,
        mode: str,
    ) -> None:
        self.staged = staged
        self.resume = resume
        self.mode = mode

    async def run(self, verb: str, _path: str, **_kwargs: Any):
        if verb != "symbols":
            return
        for index in range(50):
            node_id = f"py:new.py::staged_{index}"
            yield RunRecord(
                stream="stdout",
                payload={
                    "record_type": "symbol",
                    "source_name": "ggoss-py",
                    "data": {
                        "id": node_id,
                        "name": f"staged_{index}",
                        "location": {"file": "new.py", "line": index + 1},
                        "certainty": "asserted",
                    },
                },
            )
        # The analyzer generator is resumed only after _run_analyzer_stage has
        # committed its 50-record batch and persisted progress.
        self.staged.set()
        await self.resume.wait()
        if self.mode == "failure":
            raise RuntimeError("redteam_analyzer_failure")


class _SuccessfulRunner:
    async def run(self, verb: str, _path: str, **_kwargs: Any):
        if verb == "symbols":
            for node_id, name in (
                ("py:new.py::caller", "caller"),
                ("py:new.py::callee", "callee"),
            ):
                yield RunRecord(
                    stream="stdout",
                    payload={
                        "record_type": "symbol",
                        "source_name": "ggoss-py",
                        "data": {
                            "id": node_id,
                            "name": name,
                            "location": {"file": "new.py", "line": 1},
                            "certainty": "asserted",
                        },
                    },
                )
        elif verb == "calls":
            yield RunRecord(
                stream="stdout",
                payload={
                    "record_type": "edge",
                    "source_name": "ggoss-py",
                    "data": {
                        "source_id": "py:new.py::caller",
                        "target_id": "py:new.py::callee",
                        "kind": "CALLS",
                        "certainty": "asserted",
                        "metadata": {"call_site": {"file": "new.py", "line": 2}},
                    },
                },
            )


@pytest.mark.asyncio
async def test_seal_rejects_late_stage_in_the_same_transaction(database):
    _, session_factory = database
    project_id, _, run_id, _ = await _seed_ready_project(session_factory)
    await _make_running(
        session_factory, project_id=project_id, run_id=run_id
    )

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
        await session.rollback()


@pytest.mark.asyncio
async def test_identical_owner_union_is_monotonic_and_conflicts_fail_closed(database):
    _, session_factory = database
    project_id, _, run_id, _ = await _seed_ready_project(session_factory)
    await _make_running(
        session_factory, project_id=project_id, run_id=run_id
    )
    node_data = {"id": "contract:GET /shared", "name": "GET /shared"}
    edge_data = {"call_site": {"file": "shared.py", "line": 2}}

    async with session_factory() as session:
        for producer, changed in (
            ("ggoss-py", True),
            ("ggoss-ts", True),
            # Replaying the canonical first owner must not erase ggoss-ts.
            ("ggoss-py", False),
        ):
            assert (
                await stage_node(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    node_id="contract:GET /shared",
                    kind="Contract",
                    data=node_data,
                    certainty="asserted",
                    source_name=producer,
                )
                is changed
            )
            assert (
                await stage_edge(
                    session,
                    project_id=project_id,
                    run_id=run_id,
                    source_id="py:new.py::caller",
                    target_id="contract:GET /shared",
                    kind="CALLS",
                    data=edge_data,
                    certainty="asserted",
                    source_name=producer,
                )
                is changed
            )
        await session.commit()

    async with session_factory() as session:
        staged_node = await session.get(
            GraphNodeStage, (run_id, "contract:GET /shared")
        )
        staged_edge = await session.get(
            GraphEdgeStage,
            (run_id, "py:new.py::caller", "contract:GET /shared", "CALLS"),
        )
        assert staged_node is not None and staged_edge is not None
        assert staged_node.source_names == ["ggoss-py", "ggoss-ts"]
        assert staged_edge.source_names == ["ggoss-py", "ggoss-ts"]
        # A changed replay from the canonical first owner is still a conflict:
        # ggoss-ts already contributed the prior payload.  The outcome must not
        # depend on which producer sorts first into ``source_name``.
        with pytest.raises(StagedIdentityConflict):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="contract:GET /shared",
                kind="Contract",
                data={**node_data, "name": "canonical-owner-conflict"},
                certainty="asserted",
                source_name="ggoss-py",
            )
        with pytest.raises(StagedIdentityConflict):
            await stage_edge(
                session,
                project_id=project_id,
                run_id=run_id,
                source_id="py:new.py::caller",
                target_id="contract:GET /shared",
                kind="CALLS",
                data={"call_site": {"file": "canonical.py", "line": 7}},
                certainty="asserted",
                source_name="ggoss-py",
            )
        with pytest.raises(StagedIdentityConflict):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="contract:GET /shared",
                kind="Contract",
                data={**node_data, "name": "conflicting"},
                certainty="asserted",
                source_name="ggoss-java",
            )
        with pytest.raises(StagedIdentityConflict):
            await stage_edge(
                session,
                project_id=project_id,
                run_id=run_id,
                source_id="py:new.py::caller",
                target_id="contract:GET /shared",
                kind="CALLS",
                data={"call_site": {"file": "different.py", "line": 9}},
                certainty="asserted",
                source_name="ggoss-java",
            )
        await session.rollback()

    async with session_factory() as session:
        await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources=set(),
        )
        await session.commit()
    async with session_factory() as session:
        result = await promote_staged_graph(
            session, project_id=project_id, run_id=run_id
        )
        assert result.idempotent is False

    async with session_factory() as session:
        current_node = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "contract:GET /shared",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        current_edge = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.source_id == "py:new.py::caller",
                    Edge.target_id == "contract:GET /shared",
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one()
        assert current_node.created_by == ["ggoss-py", "ggoss-ts"]
        assert current_edge.created_by == ["ggoss-py", "ggoss-ts"]


@pytest.mark.asyncio
async def test_database_rejects_cross_project_head_and_stage_provenance(database):
    _, session_factory = database
    project_a, _, run_a, _ = await _seed_ready_project(session_factory)
    project_b, base_b, _, _ = await _seed_ready_project(session_factory)

    # A ready head must not be able to cite a completed run from another
    # project even though that run UUID exists.
    async with session_factory() as session:
        head_a = await session.get(GraphHead, project_a)
        assert head_a is not None
        head_a.current_run_id = base_b
        with pytest.raises(IntegrityError):
            await session.commit()

    data = {"id": "py:cross-project", "name": "poison"}
    async with session_factory() as session:
        session.add(
            GraphNodeStage(
                run_id=run_a,
                project_id=project_b,
                node_id="py:cross-project",
                kind="Symbol",
                data=data,
                certainty="asserted",
                source_name="ggoss-py",
                source_names=["ggoss-py"],
                semantic_hash=node_semantic_hash(
                    kind="Symbol",
                    data=data,
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_partial_unique_indexes_allow_history_but_reject_two_current_rows(database):
    engine, session_factory = database
    project_id, _, _, published_at = await _seed_ready_project(session_factory)
    older = published_at + timedelta(seconds=1)
    newer = published_at + timedelta(seconds=2)

    async with session_factory() as session:
        session.add_all(
            [
                Node(
                    id="py:identity",
                    project_id=project_id,
                    kind="Symbol",
                    data={"version": 1},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=older,
                    valid_to=newer,
                ),
                Node(
                    id="py:identity",
                    project_id=project_id,
                    kind="Symbol",
                    data={"version": 2},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=newer,
                ),
                Edge(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    source_id="py:a",
                    target_id="py:b",
                    kind="CALLS",
                    data={"version": 1},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=older,
                    valid_to=newer,
                ),
                Edge(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    source_id="py:a",
                    target_id="py:b",
                    kind="CALLS",
                    data={"version": 2},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=newer,
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        session.add(
            Node(
                id="py:identity",
                project_id=project_id,
                kind="Symbol",
                data={"version": 3},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=newer + timedelta(seconds=1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with session_factory() as session:
        session.add(
            Edge(
                id=uuid.uuid4(),
                project_id=project_id,
                source_id="py:a",
                target_id="py:b",
                kind="CALLS",
                data={"version": 3},
                certainty="asserted",
                created_by=["ggoss-py"],
                valid_from=newer + timedelta(seconds=1),
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()

    async with engine.connect() as connection:
        index_sql = dict(
            (
                await connection.execute(
                    text(
                        "SELECT name, sql FROM sqlite_master "
                        "WHERE type='index' AND name IN "
                        "('uq_nodes_current_identity','uq_edges_current_identity')"
                    )
                )
            ).all()
        )
    assert set(index_sql) == {
        "uq_nodes_current_identity",
        "uq_edges_current_identity",
    }
    assert all("WHERE valid_to IS NULL" in sql for sql in index_sql.values())


@pytest.mark.parametrize("mode", ["failure", "cancelled"])
@pytest.mark.asyncio
async def test_run_ingest_failure_or_cancel_keeps_old_head_and_checksum_readable(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
):
    _, session_factory = database
    source_path = tmp_path / f"source-{mode}"
    source_path.mkdir()
    (source_path / "new.py").write_text("def staged():\n    return 1\n", encoding="utf-8")
    project_id, base_run_id, run_id, _ = await _seed_ready_project(session_factory)
    before_digest, before_stamp = await _published_snapshot(
        session_factory, project_id
    )
    jobs = await _wire_jobs(monkeypatch, session_factory, source_path)
    staged = asyncio.Event()
    resume = asyncio.Event()
    runner = _BlockingRunner(staged=staged, resume=resume, mode=mode)
    monkeypatch.setattr(jobs, "runner_for", lambda _language: runner)

    task = asyncio.create_task(
        jobs.run_ingest(
            {"progress": _Bus()},
            str(project_id),
            str(run_id),
            str(source_path),
            {"scope": "full", "summarize": False},
        )
    )
    await asyncio.wait_for(staged.wait(), timeout=15)

    during_digest, during_stamp = await asyncio.wait_for(
        _published_snapshot(session_factory, project_id), timeout=5
    )
    assert during_digest == before_digest
    assert during_stamp == before_stamp
    async with session_factory() as session:
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(GraphNodeStage).where(
                        GraphNodeStage.run_id == run_id
                    )
                )
            ).scalar_one()
        ) == 50
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(Node).where(
                        Node.project_id == project_id,
                        Node.valid_to.is_(None),
                    )
                )
            ).scalar_one()
        ) == 1
        if mode == "cancelled":
            run = await session.get(AnalysisRun, run_id)
            assert run is not None
            run.status = "cancelled"
            run.completed_at = datetime.now(tz=timezone.utc)
            await session.commit()

    resume.set()
    await asyncio.wait_for(task, timeout=15)

    after_digest, after_stamp = await _published_snapshot(session_factory, project_id)
    assert after_digest == before_digest
    assert after_stamp == before_stamp
    assert after_stamp.current_run_id == base_run_id
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and head is not None
        assert run.status == ("failed" if mode == "failure" else "cancelled")
        assert head.current_run_id == base_run_id
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(GraphNodeStage).where(
                        GraphNodeStage.run_id == run_id
                    )
                )
            ).scalar_one()
        ) == 0


@pytest.mark.asyncio
async def test_successful_run_is_commit_atomic_and_promotion_retry_is_idempotent(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    source_path = tmp_path / "source-success"
    source_path.mkdir()
    (source_path / "new.py").write_text(
        "def caller():\n    return callee()\n\ndef callee():\n    return 1\n",
        encoding="utf-8",
    )
    project_id, base_run_id, run_id, _ = await _seed_ready_project(session_factory)
    before_digest, before_stamp = await _published_snapshot(
        session_factory, project_id
    )
    jobs = await _wire_jobs(monkeypatch, session_factory, source_path)
    monkeypatch.setattr(jobs, "runner_for", lambda _language: _SuccessfulRunner())

    from app import graph_publication as publication_module

    real_promote_nodes = publication_module._promote_nodes
    inside_transaction = asyncio.Event()
    finish_transaction = asyncio.Event()

    async def _paused_promote_nodes(*args: Any, **kwargs: Any):
        result = await real_promote_nodes(*args, **kwargs)
        inside_transaction.set()
        await finish_transaction.wait()
        return result

    monkeypatch.setattr(publication_module, "_promote_nodes", _paused_promote_nodes)
    task = asyncio.create_task(
        jobs.run_ingest(
            {"progress": _Bus()},
            str(project_id),
            str(run_id),
            str(source_path),
            {"scope": "full", "summarize": False},
        )
    )
    await asyncio.wait_for(inside_transaction.wait(), timeout=15)

    # The writer has already CAS-updated its private head and replaced nodes,
    # but an independent connection must still see the complete old snapshot.
    during_digest, during_stamp = await asyncio.wait_for(
        _published_snapshot(session_factory, project_id), timeout=5
    )
    assert during_digest == before_digest
    assert during_stamp == before_stamp
    assert during_stamp.current_run_id == base_run_id

    finish_transaction.set()
    await asyncio.wait_for(task, timeout=15)
    after_digest, after_stamp = await _published_snapshot(session_factory, project_id)
    assert after_digest != before_digest
    assert after_stamp.generation == before_stamp.generation + 1
    assert after_stamp.current_run_id == run_id

    async with session_factory() as session:
        retry = await promote_staged_graph(
            session, project_id=project_id, run_id=run_id
        )
        assert retry.idempotent is True
        assert retry.generation == after_stamp.generation
    final_digest, final_stamp = await _published_snapshot(session_factory, project_id)
    assert final_digest == after_digest
    assert final_stamp == after_stamp


@pytest.mark.asyncio
async def test_run_ingest_findings_failure_is_partial_after_head_publication(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    source_path = tmp_path / "source-postprocess-failure"
    source_path.mkdir()
    (source_path / "new.py").write_text(
        "def caller():\n    return callee()\n\ndef callee():\n    return 1\n",
        encoding="utf-8",
    )
    project_id, _base_run_id, run_id, _ = await _seed_ready_project(
        session_factory
    )
    jobs = await _wire_jobs(monkeypatch, session_factory, source_path)
    monkeypatch.setattr(jobs, "runner_for", lambda _language: _SuccessfulRunner())

    async def _broken_findings(
        _session: AsyncSession,
        _project_id: uuid.UUID,
    ) -> dict[str, int]:
        raise RuntimeError("findings postprocess failed")

    monkeypatch.setattr(jobs, "rebuild_findings", _broken_findings)
    bus = _Bus()
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(run_id),
        str(source_path),
        {"scope": "full", "summarize": False},
    )

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        published_node = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:new.py::caller",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one_or_none()
        assert run is not None and run.status == "partial"
        assert run.completed_at is not None
        assert run.stats["graph_publication"]["atomic"] is True
        assert run.stats["postprocess_error"]["stage"] == "findings"
        assert head is not None and head.current_run_id == run_id
        assert head.generation == 2
        assert published_node is not None
    event_names = [event.get("event") for event in bus.events]
    assert "run_published" in event_names
    assert "run_partial" in event_names
    assert "run_failed" not in event_names


@pytest.mark.asyncio
async def test_first_incremental_request_becomes_effective_full_rebuild(
    database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    source_path = tmp_path / "source-first-run"
    source_path.mkdir()
    (source_path / "new.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="phase-b-first-run",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/phase-b-first-run",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test:first-default",
                git_sha="c" * 40,
                scope="incremental",
            )
        )
        await session.commit()

    jobs = await _wire_jobs(monkeypatch, session_factory, source_path)
    monkeypatch.setattr(jobs, "runner_for", lambda _language: _SuccessfulRunner())
    await jobs.run_ingest(
        {"progress": _Bus()},
        str(project_id),
        str(run_id),
        str(source_path),
        {"scope": "incremental", "summarize": False},
    )

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and head is not None
        assert run.status == "completed"
        assert run.scope == "full"
        assert run.stats["refresh"]["requested_mode"] == "incremental"
        assert run.stats["refresh"]["mode"] == "full"
        assert run.stats["graph_publication"]["atomic"] is True
        assert head.state == "ready"
        assert head.current_run_id == run_id
        assert head.generation == 1
        assert int(
            (
                await session.execute(
                    select(func.count()).select_from(Node).where(
                        Node.project_id == project_id,
                        Node.valid_to.is_(None),
                    )
                )
            ).scalar_one()
        ) == 2


def _postgres_url() -> str | None:
    raw = os.environ.get("MNEMOS_TEST_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL", ""
    )
    if not raw.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    prefix, rest = raw.split("://", 1)
    if prefix.startswith("postgresql+"):
        return "postgresql+asyncpg://" + rest
    return None


@pytest_asyncio.fixture
async def postgres_database():
    """Create and remove a private schema when a PostgreSQL URL is available."""

    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url)
    schema = f"mnemos_phase_b_redteam_{uuid.uuid4().hex}"
    schema_engine: AsyncEngine | None = None
    try:
        async with root_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        async with schema_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield schema_engine, async_sessionmaker(
            schema_engine, expire_on_commit=False
        )
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        finally:
            await root_engine.dispose()


@pytest.mark.asyncio
async def test_postgres_independent_connections_enforce_seal_and_atomic_visibility(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
):
    """Real PostgreSQL FOR UPDATE fencing and MVCC publication visibility."""

    _, session_factory = postgres_database
    project_id, base_run_id, run_id, _ = await _seed_ready_project(session_factory)
    await _make_running(
        session_factory, project_id=project_id, run_id=run_id
    )
    writer_has_lock = asyncio.Event()
    release_writer = asyncio.Event()
    seal_completed = asyncio.Event()

    async def _writer() -> None:
        async with session_factory() as session:
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="py:postgres-staged",
                kind="Symbol",
                data={"id": "py:postgres-staged", "name": "staged"},
                certainty="asserted",
                source_name="ggoss-py",
            )
            await session.flush()
            writer_has_lock.set()
            await release_writer.wait()
            await session.commit()

    async def _sealer() -> None:
        await writer_has_lock.wait()
        async with session_factory() as session:
            await seal_graph_coverage(
                session,
                project_id=project_id,
                run_id=run_id,
                authoritative_sources=set(),
            )
            await session.commit()
        seal_completed.set()

    writer_task = asyncio.create_task(_writer())
    await asyncio.wait_for(writer_has_lock.wait(), timeout=10)
    seal_task = asyncio.create_task(_sealer())
    await asyncio.sleep(0.1)
    assert not seal_completed.is_set(), "seal must wait for the open stage writer"
    release_writer.set()
    await asyncio.wait_for(asyncio.gather(writer_task, seal_task), timeout=10)

    async with session_factory() as session:
        with pytest.raises(GraphCoverageAlreadySealed):
            await stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id="py:postgres-late",
                kind="Symbol",
                data={"id": "py:postgres-late", "name": "late"},
                certainty="asserted",
                source_name="ggoss-py",
            )
        await session.rollback()

    before_digest, before_stamp = await _published_snapshot(
        session_factory, project_id
    )
    from app import graph_publication as publication_module

    real_promote_nodes = publication_module._promote_nodes
    inside_transaction = asyncio.Event()
    finish_transaction = asyncio.Event()

    async def _paused(*args: Any, **kwargs: Any):
        result = await real_promote_nodes(*args, **kwargs)
        inside_transaction.set()
        await finish_transaction.wait()
        return result

    monkeypatch.setattr(publication_module, "_promote_nodes", _paused)

    async def _promote():
        async with session_factory() as session:
            return await promote_staged_graph(
                session, project_id=project_id, run_id=run_id
            )

    promotion_task = asyncio.create_task(_promote())
    await asyncio.wait_for(inside_transaction.wait(), timeout=10)
    during_digest, during_stamp = await _published_snapshot(
        session_factory, project_id
    )
    assert during_digest == before_digest
    assert during_stamp == before_stamp
    assert during_stamp.current_run_id == base_run_id
    finish_transaction.set()
    result = await asyncio.wait_for(promotion_task, timeout=10)
    assert result.idempotent is False
    after_digest, after_stamp = await _published_snapshot(session_factory, project_id)
    assert after_digest != before_digest
    assert after_stamp.current_run_id == run_id
    assert after_stamp.generation == before_stamp.generation + 1


@pytest.mark.asyncio
async def test_postgres_overlay_commit_serializes_with_source_promotion(
    postgres_database,
):
    """A durable overlay and source promotion share one GraphHead lock order."""

    _, session_factory = postgres_database
    project_id, _, run_id, _ = await _seed_ready_project(session_factory)
    await _make_running(
        session_factory, project_id=project_id, run_id=run_id
    )
    async with session_factory() as session:
        await stage_node(
            session,
            project_id=project_id,
            run_id=run_id,
            node_id="py:old.py::old",
            kind="Symbol",
            data={"id": "py:old.py::old", "name": "reindexed"},
            certainty="asserted",
            source_name="ggoss-py",
        )
        await session.commit()
    async with session_factory() as session:
        await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
        await session.commit()

    overlay_locked = asyncio.Event()
    release_overlay = asyncio.Event()

    async def _write_overlay() -> None:
        async with session_factory() as session:
            await record_human_confirmation(
                session,
                project_id=project_id,
                target_kind="node",
                target_id="py:old.py::old",
                action="confirm",
                actor="test:postgres-overlay",
                rationale="independent row-lock proof",
            )
            overlay_locked.set()
            await release_overlay.wait()
            await session.commit()

    overlay_task = asyncio.create_task(_write_overlay())
    await asyncio.wait_for(overlay_locked.wait(), timeout=10)

    # PostgreSQL READ COMMITTED readers still see the complete pre-overlay
    # revision while the writer has changed both Node and GraphHead privately.
    async with session_factory() as session:
        during_stamp = await read_graph_stamp(session, project_id=project_id)
        during_node = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:old.py::old",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        assert during_stamp.generation == 1
        assert during_stamp.overlay_generation == 0
        assert "human_confirmation" not in during_node.data

    async def _promote():
        async with session_factory() as session:
            return await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
            )

    promotion_task = asyncio.create_task(_promote())
    await asyncio.sleep(0.1)
    assert not promotion_task.done(), "promotion must wait for the overlay head lock"
    release_overlay.set()
    await asyncio.wait_for(overlay_task, timeout=10)
    promotion = await asyncio.wait_for(promotion_task, timeout=10)
    assert promotion.generation == 2

    async with session_factory() as session:
        final_stamp = await read_graph_stamp(session, project_id=project_id)
        final_node = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == "py:old.py::old",
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
    assert final_stamp.generation == 2
    assert final_stamp.overlay_generation == 1
    assert final_node.data["name"] == "reindexed"
    assert final_node.data["human_confirmation"]["action"] == "confirm"


@pytest.mark.asyncio
async def test_postgres_summary_writers_serialize_and_leave_one_current(
    postgres_database,
):
    """Independent writers linearize on GraphHead before replace-and-insert."""

    _, session_factory = postgres_database
    project_id, _, _, _ = await _seed_ready_project(session_factory)
    target_id = "module:concurrent-summary"
    writer_a_locked = asyncio.Event()
    release_writer_a = asyncio.Event()
    writer_b_locked = asyncio.Event()

    async def _replace_summary(
        label: str,
        *,
        locked: asyncio.Event,
        release: asyncio.Event | None = None,
    ) -> uuid.UUID:
        row_id = uuid.uuid4()
        async with session_factory() as session:
            revision = await lock_ready_summary_generation(
                session,
                project_id=project_id,
                expected_generation=1,
                expected_overlay_generation=0,
            )
            locked.set()
            await session.execute(
                Summary.__table__.update()
                .where(
                    Summary.project_id == project_id,
                    Summary.target_id == target_id,
                    Summary.level == 3,
                    Summary.superseded_by.is_(None),
                    Summary.validated_graph_generation.is_not(None),
                )
                .values(superseded_by=row_id)
            )
            session.add(
                Summary(
                    id=row_id,
                    project_id=project_id,
                    target_id=target_id,
                    level=3,
                    validated_graph_generation=revision.source_generation,
                    validated_overlay_generation=revision.overlay_generation,
                    validated_at=datetime.now(tz=timezone.utc),
                    summary=f"writer:{label}",
                    model_used="test-provider",
                )
            )
            await session.flush()
            if release is not None:
                await release.wait()
            await session.commit()
        return row_id

    writer_a = asyncio.create_task(
        _replace_summary(
            "a",
            locked=writer_a_locked,
            release=release_writer_a,
        )
    )
    await asyncio.wait_for(writer_a_locked.wait(), timeout=10)
    writer_b = asyncio.create_task(
        _replace_summary("b", locked=writer_b_locked)
    )
    await asyncio.sleep(0.1)
    assert not writer_b_locked.is_set(), "writer B must wait for A's head lock"
    assert not writer_b.done()

    release_writer_a.set()
    writer_a_id, writer_b_id = await asyncio.wait_for(
        asyncio.gather(writer_a, writer_b), timeout=10
    )
    assert writer_b_locked.is_set()

    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Summary).where(
                    Summary.project_id == project_id,
                    Summary.target_id == target_id,
                    Summary.level == 3,
                )
            )
        ).scalars().all()

    assert {row.id for row in rows} == {writer_a_id, writer_b_id}
    assert [
        row.id
        for row in rows
        if row.validated_graph_generation is not None
        and row.superseded_by is None
    ] == [writer_b_id]
    assert next(row for row in rows if row.id == writer_a_id).superseded_by == (
        writer_b_id
    )


@pytest.mark.asyncio
async def test_postgres_retry_bookkeeping_preserves_concurrent_cancel_flag(
    postgres_database,
    monkeypatch: pytest.MonkeyPatch,
):
    """Published retry metadata merges after the cancellation row lock."""

    from app.orchestrator import jobs

    _, session_factory = postgres_database
    project_id, published_run_id, _, _ = await _seed_ready_project(
        session_factory
    )
    async with session_factory() as session:
        run = await session.get(AnalysisRun, published_run_id)
        assert run is not None
        run.status = "published"
        run.completed_at = None
        original_receipt = dict(run.stats["graph_publication"])
        await session.commit()

    monkeypatch.setattr(jobs, "SessionLocal", session_factory)

    class _Queue:
        async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> object:
            return object()

    bus = _Bus()
    async with session_factory() as cancellation_session:
        locked_run = (
            await cancellation_session.execute(
                select(AnalysisRun)
                .where(AnalysisRun.id == published_run_id)
                .with_for_update()
            )
        ).scalar_one()
        locked_run.stats = {
            **dict(locked_run.stats),
            "postprocess": {
                "cancel_requested": True,
                "cancel_requested_by": "user:concurrent-operator",
            },
        }
        await cancellation_session.flush()

        deferred = asyncio.create_task(
            jobs._defer_project_lock_retry(
                {"redis": _Queue()},
                bus=bus,
                project_id=project_id,
                run_id=published_run_id,
                path="C:/source-not-needed-for-resume",
                options={"scope": "full"},
            )
        )
        await asyncio.sleep(0.1)
        assert not deferred.done(), "retry merge must wait for cancellation lock"
        await cancellation_session.commit()

    assert await asyncio.wait_for(deferred, timeout=10) is True
    async with session_factory() as session:
        run = await session.get(AnalysisRun, published_run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and run.status == "published"
        assert run.stats["graph_publication"] == original_receipt
        assert run.stats["postprocess"]["cancel_requested"] is True
        assert run.stats["project_lock_wait"]["attempt"] == 1
        assert head is not None and head.current_run_id == published_run_id
        assert head.generation == original_receipt["generation"]
    assert any(event.get("event") == "run_deferred" for event in bus.events)
    assert not any(event.get("event") == "run_failed" for event in bus.events)
