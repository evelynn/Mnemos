"""SQLite-only L1 neighbour batching preserves the historical evidence contract."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "l1-sqlite-batching-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()

from app.extractor import runner  # noqa: E402
from app.extractor.agent import ExtractorResult  # noqa: E402
from app.graph_overlays import (  # noqa: E402
    edge_identity,
    edge_read_view,
    load_edge_overlays,
    load_node_human_overlays,
    node_read_view,
)
from app.models.graph import Edge, Node  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.overlays import (  # noqa: E402
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)
from app.models.projects import Project  # noqa: E402


NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


async def _database() -> tuple[Any, async_sessionmaker[AsyncSession], uuid.UUID]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            Organization.__table__,
            Project.__table__,
            Node.__table__,
            Edge.__table__,
            GraphNodeHumanOverlay.__table__,
            GraphEdgeHumanOverlay.__table__,
            GraphEdgeRuntimeOverlay.__table__,
        ):
            await connection.run_sync(table.create)

    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    nodes = [
        Node(
            id=f"sym:{index:03d}",
            project_id=project_id,
            valid_from=NOW,
            kind="Symbol",
            data={"name": f"symbol_{index:03d}", "ordinal": index},
            certainty="inferred" if index % 2 else "asserted",
            created_by=["test"],
        )
        for index in range(65)
    ]
    # Reverse insertion deliberately disagrees with every evidence sort.
    edges = [
        Edge(
            id=uuid.UUID(int=5),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:064",
            target_id="sym:064",
            kind="CALLS",
            data={"ordinal": 5},
            certainty="inferred",
            created_by=["test"],
        ),
        Edge(
            id=uuid.UUID(int=4),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:000",
            target_id="sym:003",
            kind="READS",
            data={"ordinal": 4},
            certainty="verified",
            created_by=["test"],
        ),
        Edge(
            id=uuid.UUID(int=3),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:002",
            target_id="sym:000",
            kind="CALLS",
            data={"ordinal": 3},
            certainty="asserted",
            created_by=["test"],
        ),
        Edge(
            id=uuid.UUID(int=2),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:000",
            target_id="sym:001",
            kind="CALLS",
            data={"ordinal": 2},
            certainty="inferred",
            created_by=["test"],
        ),
        Edge(
            id=uuid.UUID(int=1),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:000",
            target_id="sym:000",
            kind="CALLS",
            data={"ordinal": 1},
            certainty="inferred",
            created_by=["test"],
        ),
    ]
    edges.extend(
        Edge(
            id=uuid.UUID(int=100 + index),
            project_id=project_id,
            valid_from=NOW,
            source_id="sym:000",
            target_id=f"zz-out:{index:02d}",
            kind="CALLS",
            data={"direction": "out", "ordinal": index},
            certainty="asserted",
            created_by=["test"],
        )
        for index in range(11)
    )
    edges.extend(
        Edge(
            id=uuid.UUID(int=200 + index),
            project_id=project_id,
            valid_from=NOW,
            source_id=f"zz-in:{index:02d}",
            target_id="sym:000",
            kind="CALLS",
            data={"direction": "in", "ordinal": index},
            certainty="asserted",
            created_by=["test"],
        )
        for index in range(11)
    )
    async with sessions() as session:
        session.add(
            Organization(
                id=organization_id,
                slug=f"l1-batch-{organization_id.hex}",
                display_name="L1 batching",
            )
        )
        await session.flush()
        session.add(
            Project(
                id=project_id,
                name="l1 batching",
                gitlab_project_id=1,
                gitlab_url="https://example.invalid/l1-batching.git",
                default_branch="main",
                languages=["Python"],
                organization_id=organization_id,
            )
        )
        await session.flush()
        session.add_all(nodes)
        session.add_all(edges)
        session.add_all(
            [
                GraphNodeHumanOverlay(
                    project_id=project_id,
                    node_id="sym:000",
                    action="confirm",
                    actor="reviewer",
                    rationale="node reviewed",
                    recorded_at=NOW,
                    metadata_={"source": "test"},
                ),
                GraphEdgeHumanOverlay(
                    project_id=project_id,
                    source_id="sym:000",
                    target_id="sym:000",
                    kind="CALLS",
                    action="confirm",
                    actor="reviewer",
                    rationale="self-loop reviewed",
                    recorded_at=NOW,
                    metadata_={"source": "test"},
                ),
                GraphEdgeHumanOverlay(
                    project_id=project_id,
                    source_id="sym:000",
                    target_id="sym:001",
                    kind="CALLS",
                    action="dispute",
                    actor="reviewer",
                    rationale="cross edge disputed",
                    recorded_at=NOW,
                    metadata_={"source": "test"},
                ),
                GraphEdgeHumanOverlay(
                    project_id=project_id,
                    source_id="sym:064",
                    target_id="sym:064",
                    kind="CALLS",
                    action="confirm",
                    actor="reviewer",
                    rationale="boundary reviewed",
                    recorded_at=NOW,
                    metadata_={"source": "test"},
                ),
                GraphEdgeRuntimeOverlay(
                    project_id=project_id,
                    source_id="sym:000",
                    target_id="sym:000",
                    kind="CALLS",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    hit_count=7,
                    metadata_={"source": "test"},
                ),
                GraphEdgeRuntimeOverlay(
                    project_id=project_id,
                    source_id="sym:000",
                    target_id="sym:001",
                    kind="CALLS",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    hit_count=11,
                    metadata_={"source": "test"},
                ),
                GraphEdgeRuntimeOverlay(
                    project_id=project_id,
                    source_id="sym:064",
                    target_id="sym:064",
                    kind="CALLS",
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    hit_count=13,
                    metadata_={"source": "test"},
                ),
            ]
        )
        await session.commit()
    return engine, sessions, project_id


async def _historical_evidence(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    nodes: list[Node],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Embedded pre-batching L1 evidence loader used as the parity oracle."""

    result = []
    for node in nodes:
        neighbours_out = (
            (
                await session.execute(
                    select(Edge)
                    .where(
                        Edge.project_id == project_id,
                        Edge.source_id == node.id,
                        Edge.valid_to.is_(None),
                    )
                    .order_by(
                        Edge.kind.asc(),
                        Edge.target_id.asc(),
                        Edge.source_id.asc(),
                        Edge.id.asc(),
                    )
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        neighbours_in = (
            (
                await session.execute(
                    select(Edge)
                    .where(
                        Edge.project_id == project_id,
                        Edge.target_id == node.id,
                        Edge.valid_to.is_(None),
                    )
                    .order_by(
                        Edge.kind.asc(),
                        Edge.source_id.asc(),
                        Edge.target_id.asc(),
                        Edge.id.asc(),
                    )
                    .limit(10)
                )
            )
            .scalars()
            .all()
        )
        node_overlays = await load_node_human_overlays(
            session,
            project_id=project_id,
            node_ids=[node.id],
        )
        neighbour_edges = [*neighbours_in, *neighbours_out]
        edge_overlays = await load_edge_overlays(
            session,
            project_id=project_id,
            identities=(edge_identity(edge) for edge in neighbour_edges),
        )
        evidence = [
            {
                "kind": "node",
                "node_id": node.id,
                **node_read_view(node, node_overlays.get(node.id)),
            }
        ]
        for edge in neighbour_edges:
            identity = edge_identity(edge)
            evidence.append(
                {
                    "kind": "edge",
                    "edge_id": str(edge.id),
                    "edge_kind": edge.kind,
                    "source_id": edge.source_id,
                    "target_id": edge.target_id,
                    **edge_read_view(
                        edge,
                        edge_overlays.human.get(identity),
                        edge_overlays.runtime.get(identity),
                    ),
                }
            )
        result.append((node.id, evidence))
    return result


async def _sqlite_batched_evidence(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    nodes: list[Node],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Render raw evidence from the production SQLite batch loader."""

    result = []
    for start in range(0, len(nodes), runner._L1_SQLITE_BATCH_SIZE):
        block = nodes[start : start + runner._L1_SQLITE_BATCH_SIZE]
        outgoing, incoming, node_overlays, edge_overlays = (
            await runner._sqlite_l1_batch(
                session,
                project_id=project_id,
                nodes=block,
            )
        )
        for node in block:
            evidence = [
                {
                    "kind": "node",
                    "node_id": node.id,
                    **node_read_view(node, node_overlays.get(node.id)),
                }
            ]
            for edge in [*incoming[node.id], *outgoing[node.id]]:
                identity = edge_identity(edge)
                evidence.append(
                    {
                        "kind": "edge",
                        "edge_id": str(edge.id),
                        "edge_kind": edge.kind,
                        "source_id": edge.source_id,
                        "target_id": edge.target_id,
                        **edge_read_view(
                            edge,
                            edge_overlays.human.get(identity),
                            edge_overlays.runtime.get(identity),
                        ),
                    }
                )
            result.append((node.id, evidence))
    return result


def _provider_inputs(
    evidence_by_target: list[tuple[str, list[dict[str, Any]]]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Apply the exact L1 pack contract to a raw historical evidence list."""

    result = []
    for target_id, evidence in evidence_by_target:
        chunks = runner.pack_by_budget(evidence, max_tokens=3000)
        bounded = chunks[0] if chunks else []
        if len(chunks) > 1:
            chunks = runner.pack_by_budget(evidence, max_tokens=2800)
            first_chunk = chunks[0] if chunks else []
            bounded = [
                {
                    "scope": "prompt_evidence",
                    "truncated": True,
                    "included_rows": len(first_chunk),
                    "total_rows": len(evidence),
                    "included_chunks": 1,
                    "total_chunks": len(chunks),
                },
                *first_chunk,
            ]
        result.append((target_id, bounded))
    return result


def _stub_l1_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    *,
    nodes: list[Node],
    captured: list[tuple[str, list[dict[str, Any]]]],
    exhaust_budget_after: int | None = None,
) -> None:
    async def graph_stamp(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(generation=1, overlay_generation=1)

    async def priority_symbols(*_args: Any, **_kwargs: Any) -> list[Node]:
        return nodes

    async def no_entities(*_args: Any, **_kwargs: Any) -> list[Node]:
        return []

    async def no_summary(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def capture(
        _session: Any,
        _extractor: Any,
        _project_id: uuid.UUID,
        _level: int,
        target_id: str,
        evidence: list[dict[str, Any]],
        *_args: Any,
        **_kwargs: Any,
    ) -> ExtractorResult:
        captured.append((target_id, evidence))
        budget = _kwargs.get("run_budget")
        if budget is None and len(_args) >= 2:
            budget = _args[1]
        if exhaust_budget_after is not None and len(captured) >= exhaust_budget_after:
            budget.exhausted = True
        return ExtractorResult(
            summary=target_id,
            detailed=target_id,
            claims=[],
            open_questions=[],
            model_used="test",
            tokens_used=0,
            fallback_reason="test",
        )

    async def unchanged_result(
        *_args: Any,
        result: ExtractorResult,
        **_kwargs: Any,
    ) -> ExtractorResult:
        return result

    async def no_persist(*_args: Any, **_kwargs: Any) -> uuid.UUID:
        return uuid.UUID(int=1)

    monkeypatch.setattr(runner, "read_graph_stamp", graph_stamp)
    monkeypatch.setattr(runner, "_priority_symbols", priority_symbols)
    monkeypatch.setattr(runner, "_priority_data_entities", no_entities)
    monkeypatch.setattr(runner, "_current_summary", no_summary)
    monkeypatch.setattr(runner, "_summarize_with_budget", capture)
    monkeypatch.setattr(runner, "_ground_result_or_fallback", unchanged_result)
    monkeypatch.setattr(runner, "_persist_summary_result", no_persist)


@pytest.mark.asyncio
async def test_sqlite_l1_batch_is_byte_exact_across_boundary_and_overlays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, project_id = await _database()
    try:
        async with sessions() as session:
            nodes = (
                (
                    await session.execute(
                        select(Node).where(Node.project_id == project_id).order_by(Node.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            expected = await _historical_evidence(
                session,
                project_id=project_id,
                nodes=nodes,
            )
            raw_batched = await _sqlite_batched_evidence(
                session,
                project_id=project_id,
                nodes=nodes,
            )
            assert _json_bytes(raw_batched) == _json_bytes(expected)
            captured: list[tuple[str, list[dict[str, Any]]]] = []
            _stub_l1_pipeline(monkeypatch, nodes=nodes, captured=captured)

            select_count = 0

            def count_selects(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                nonlocal select_count
                if statement.lstrip().upper().startswith("SELECT"):
                    select_count += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                produced = await runner.summarise_l1(
                    session,
                    object(),  # type: ignore[arg-type]
                    project_id=project_id,
                    limit=65,
                )
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    count_selects,
                )

        assert produced == 65
        assert _json_bytes(captured) == _json_bytes(_provider_inputs(expected))
        assert [target_id for target_id, _ in captured] == [
            f"sym:{index:03d}" for index in range(65)
        ]
        # In this sparse fixture every block's selected identities fit one
        # 250-identity overlay chunk: two direction queries + node overlays +
        # human/runtime edge overlays per block. Denser blocks may use bounded
        # additional overlay chunks, so this is fixture-specific rather than a
        # universal L1 query ceiling. It deliberately avoids wall timing.
        assert select_count <= 10

        evidence_by_target = dict(raw_batched)
        self_loop_id = str(uuid.UUID(int=1))
        cross_edge_id = str(uuid.UUID(int=2))
        assert [
            row["edge_id"] for row in evidence_by_target["sym:000"][1:]
        ] == [
            str(uuid.UUID(int=1)),
            str(uuid.UUID(int=3)),
            *(str(uuid.UUID(int=200 + index)) for index in range(8)),
            str(uuid.UUID(int=1)),
            str(uuid.UUID(int=2)),
            *(str(uuid.UUID(int=100 + index)) for index in range(8)),
        ]
        assert [
            row["edge_id"]
            for row in evidence_by_target["sym:000"]
            if row.get("edge_id") == self_loop_id
        ] == [self_loop_id, self_loop_id]
        assert (
            sum(
                row.get("edge_id") == cross_edge_id
                for evidence in evidence_by_target.values()
                for row in evidence
            )
            == 2
        )
        overlaid_self_loop = next(
            row for row in evidence_by_target["sym:000"] if row.get("edge_id") == self_loop_id
        )
        assert overlaid_self_loop["confirmed"] == "confirm"
        assert overlaid_self_loop["exercised"] is True
        assert overlaid_self_loop["data"]["hit_count"] == 7
        boundary_rows = evidence_by_target["sym:064"]
        assert [row.get("edge_id") for row in boundary_rows[1:]] == [
            str(uuid.UUID(int=5)),
            str(uuid.UUID(int=5)),
        ]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sqlite_l1_budget_exhaustion_does_not_preload_the_next_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, project_id = await _database()
    try:
        async with sessions() as session:
            nodes = (
                (
                    await session.execute(
                        select(Node).where(Node.project_id == project_id).order_by(Node.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            captured: list[tuple[str, list[dict[str, Any]]]] = []
            _stub_l1_pipeline(
                monkeypatch,
                nodes=nodes,
                captured=captured,
                exhaust_budget_after=1,
            )
            budget = SimpleNamespace(exhausted=False)
            select_count = 0

            def count_selects(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                nonlocal select_count
                if statement.lstrip().upper().startswith("SELECT"):
                    select_count += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                produced = await runner.summarise_l1(
                    session,
                    object(),  # type: ignore[arg-type]
                    project_id=project_id,
                    limit=65,
                    run_budget=budget,  # type: ignore[arg-type]
                )
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    count_selects,
                )

        assert produced == 1
        assert [target_id for target_id, _ in captured] == ["sym:000"]
        # Only the first block was prefetched; sym:064's second block was not.
        assert select_count <= 5
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_non_sqlite_dialect_keeps_the_historical_per_target_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, sessions, project_id = await _database()
    try:
        async with sessions() as session:
            nodes = (
                (
                    await session.execute(
                        select(Node)
                        .where(Node.project_id == project_id)
                        .order_by(Node.id.asc())
                        .limit(4)
                    )
                )
                .scalars()
                .all()
            )
            expected = await _historical_evidence(
                session,
                project_id=project_id,
                nodes=nodes,
            )
            captured: list[tuple[str, list[dict[str, Any]]]] = []
            _stub_l1_pipeline(monkeypatch, nodes=nodes, captured=captured)

            async def never_batch(*_args: Any, **_kwargs: Any) -> Any:
                raise AssertionError("non-SQLite dialect entered SQLite batching")

            monkeypatch.setattr(runner, "_sqlite_l1_batch", never_batch)

            class NonSQLiteSession:
                bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

                async def execute(self, statement: Any) -> Any:
                    return await session.execute(statement)

                async def commit(self) -> None:
                    await session.commit()

            select_count = 0

            def count_selects(
                _connection: Any,
                _cursor: Any,
                statement: str,
                _parameters: Any,
                _context: Any,
                _executemany: bool,
            ) -> None:
                nonlocal select_count
                if statement.lstrip().upper().startswith("SELECT"):
                    select_count += 1

            event.listen(engine.sync_engine, "before_cursor_execute", count_selects)
            try:
                produced = await runner.summarise_l1(
                    NonSQLiteSession(),  # type: ignore[arg-type]
                    object(),  # type: ignore[arg-type]
                    project_id=project_id,
                    limit=4,
                )
            finally:
                event.remove(
                    engine.sync_engine,
                    "before_cursor_execute",
                    count_selects,
                )

        assert produced == 4
        assert _json_bytes(captured) == _json_bytes(_provider_inputs(expected))
        assert select_count == 20
    finally:
        await engine.dispose()
