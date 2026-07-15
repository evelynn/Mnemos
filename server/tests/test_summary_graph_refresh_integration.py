from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import event, func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "summary-graph-refresh-integration-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.extractor.packing import evidence_hash  # noqa: E402
from app.extractor.agent import Extractor  # noqa: E402
from app.extractor.runner import summarise_l1  # noqa: E402
from app.graph_overlays import record_human_confirmation  # noqa: E402
from app.models.findings import Summary  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.organization import Organization  # noqa: E402, F401
from app.models.overlays import (  # noqa: E402
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)
from app.models.projects import Project  # noqa: E402
from app.orchestrator.jobs import (  # noqa: E402
    _supersede_stale_graph_summaries,
)
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


async def _summary_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)
        await connection.run_sync(GraphHead.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
        await connection.run_sync(GraphEdgeHumanOverlay.__table__.create)
        await connection.run_sync(GraphEdgeRuntimeOverlay.__table__.create)
        await connection.run_sync(Summary.__table__.create)
    return engine, sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.mark.asyncio
async def test_cache_hit_survives_stale_closure_without_rewriting_provenance():
    engine, Session = await _summary_database()
    project_id = uuid.uuid4()
    prior_run_id = uuid.uuid4()
    current_run_id = uuid.uuid4()
    cached_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    node_data = {"name": "cached"}
    current_evidence = [
        {
            "kind": "node",
            "node_id": "sym:cached",
            "data": node_data,
            "source_certainty": "asserted",
            "effective_certainty": "asserted",
            "certainty": "asserted",
            "confirmed": None,
        }
    ]

    class NeverExtractor:
        async def summarize(self, *_args, **_kwargs):
            raise AssertionError("an exact evidence-hash cache hit must use no provider")

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="summary freshness",
                gitlab_project_id=1,
                gitlab_url="https://example.invalid/project",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=current_run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha="a" * 40,
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(generation=2, published_at=now),
            )
        )
        await session.commit()
        session.add_all(
            [
                Node(
                    id="sym:cached",
                    project_id=project_id,
                    kind="Symbol",
                    data=node_data,
                    certainty="asserted",
                    created_by=["test"],
                ),
                GraphHead(
                    project_id=project_id,
                    current_run_id=current_run_id,
                    generation=2,
                    overlay_generation=3,
                    state="ready",
                    published_at=now,
                ),
                Summary(
                    id=cached_id,
                    project_id=project_id,
                    target_id="sym:cached",
                    level=1,
                    analysis_run_id=prior_run_id,
                    validated_graph_generation=1,
                    validated_overlay_generation=2,
                    validated_at=now,
                    summary="Cached grounded summary.",
                    detailed="Cached grounded summary.",
                    claims=[
                        {
                            "claim": "The cached symbol exists.",
                            "evidence": [
                                {
                                    "kind": "node",
                                    "node_id": "sym:cached",
                                    "certainty": "asserted",
                                }
                            ],
                        }
                    ],
                    open_questions=[],
                    evidence_hash=evidence_hash(current_evidence),
                    model_used="test-provider",
                    tokens_used=7,
                    generated_at=now,
                ),
                Summary(
                    id=stale_id,
                    project_id=project_id,
                    target_id="sym:removed",
                    level=1,
                    analysis_run_id=prior_run_id,
                    validated_graph_generation=1,
                    validated_overlay_generation=2,
                    validated_at=now,
                    summary="No longer visited.",
                    detailed="No longer visited.",
                    claims=[
                        {
                            "claim": "The removed symbol existed.",
                            "evidence": [
                                {
                                    "kind": "node",
                                    "node_id": "sym:removed",
                                    "certainty": "asserted",
                                }
                            ],
                        }
                    ],
                    open_questions=[],
                    evidence_hash="f" * 64,
                    model_used="test-provider",
                    tokens_used=7,
                    generated_at=now,
                ),
            ]
        )
        await session.commit()

        retained_ids: set[uuid.UUID] = set()
        produced = await summarise_l1(
            session,
            NeverExtractor(),  # type: ignore[arg-type]
            project_id=project_id,
            limit=1,
            analysis_run_id=current_run_id,
            retained_summary_ids=retained_ids,
        )
        closed = await _supersede_stale_graph_summaries(
            session,
            project_id=project_id,
            run_id=current_run_id,
            retained_summary_ids=retained_ids,
        )
        await session.commit()
        cached = await session.get(Summary, cached_id)
        stale = await session.get(Summary, stale_id)

    await engine.dispose()
    assert produced == 0
    assert retained_ids == {cached_id}
    assert closed == 1
    assert cached is not None and cached.superseded_by is None
    assert cached.analysis_run_id == prior_run_id
    assert cached.summary == "Cached grounded summary."
    assert cached.validated_graph_generation == 2
    assert cached.validated_overlay_generation == 3
    assert cached.validated_at is not None
    assert stale is not None and stale.superseded_by is not None


@pytest.mark.asyncio
async def test_edge_confirmation_invalidates_l1_evidence_cache():
    engine, Session = await _summary_database()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    edge_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)

    class CapturingExtractor:
        model = "test-capture"

        def __init__(self):
            self.calls: list[list[dict]] = []

        async def summarize(self, level, target_id, evidence):
            self.calls.append(evidence)
            return Extractor._stub(level, target_id, evidence, "no_backend")

    extractor = CapturingExtractor()
    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="overlay cache invalidation",
                gitlab_project_id=2,
                gitlab_url="https://example.invalid/overlay-cache",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha="b" * 40,
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(generation=1, published_at=now),
            )
        )
        await session.flush()
        session.add_all(
            [
                Node(
                    id="sym:caller",
                    project_id=project_id,
                    kind="Symbol",
                    data={"name": "caller"},
                    certainty="asserted",
                    created_by=["test"],
                ),
                Edge(
                    id=edge_id,
                    project_id=project_id,
                    source_id="sym:caller",
                    target_id="sym:callee",
                    kind="CALLS",
                    data={},
                    certainty="inferred",
                    created_by=["test"],
                    valid_from=now,
                ),
                GraphHead(
                    project_id=project_id,
                    current_run_id=run_id,
                    generation=1,
                    overlay_generation=0,
                    state="ready",
                    published_at=now,
                ),
            ]
        )
        await session.commit()

        assert await summarise_l1(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=1,
            analysis_run_id=run_id,
        ) == 1
        first_hash = (
            await session.execute(
                select(Summary.evidence_hash).where(
                    Summary.project_id == project_id,
                    Summary.superseded_by.is_(None),
                )
            )
        ).scalar_one()

        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="edge",
            target_id=str(edge_id),
            action="confirm",
            actor="user:test",
            rationale="source review",
        )
        await session.commit()

        assert await summarise_l1(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=1,
            analysis_run_id=run_id,
        ) == 1
        current = (
            await session.execute(
                select(Summary).where(
                    Summary.project_id == project_id,
                    Summary.superseded_by.is_(None),
                )
            )
        ).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 2
    first_edge = next(row for row in extractor.calls[0] if row["kind"] == "edge")
    second_edge = next(row for row in extractor.calls[1] if row["kind"] == "edge")
    assert first_edge["certainty"] == "inferred"
    assert second_edge["source_certainty"] == "inferred"
    assert second_edge["certainty"] == "asserted"
    assert second_edge["confirmed"] == "confirm"
    assert current.evidence_hash != first_hash
    assert current.validated_overlay_generation == 1


@pytest.mark.asyncio
async def test_large_retain_set_is_restored_in_portable_sqlite_batches():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Summary.__table__.create)

    update_binds: list[tuple[str, int]] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _capture_update_binds(
        _connection, _cursor, statement, parameters, _context, executemany
    ):
        normalized = statement.strip().lower()
        if not executemany and normalized.startswith(
            "update summaries set superseded_by"
        ):
            update_binds.append((normalized, len(parameters)))

    project_id = uuid.uuid4()
    prior_run_id = uuid.uuid4()
    current_run_id = uuid.uuid4()
    retained_ids = {uuid.uuid4() for _ in range(1_001)}
    stale_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    rows = [
        {
            "id": summary_id,
            "project_id": project_id,
            "target_id": f"sym:{summary_id}",
            "level": 1,
            "analysis_run_id": prior_run_id,
            "summary": "Retained.",
            "detailed": "Retained.",
            "claims": [],
            "open_questions": [],
            "evidence_hash": None,
            "model_used": "test-provider",
            "tokens_used": 1,
            "fallback_reason": None,
            "generated_at": now,
            "superseded_by": None,
        }
        for summary_id in retained_ids
    ]
    rows.append(
        {
            **rows[0],
            "id": stale_id,
            "target_id": "sym:stale",
        }
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        await session.execute(insert(Summary), rows)
        await session.commit()
        closed = await _supersede_stale_graph_summaries(
            session,
            project_id=project_id,
            run_id=current_run_id,
            retained_summary_ids=retained_ids,
        )
        await session.commit()
        current_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Summary).where(
                        Summary.project_id == project_id,
                        Summary.superseded_by.is_(None),
                    )
                )
            ).scalar_one()
        )
        stale = await session.get(Summary, stale_id)

    await engine.dispose()
    restore_binds = [
        bind_count
        for statement, bind_count in update_binds
        if "summaries.id in" in statement
    ]
    assert closed == 1
    assert current_count == len(retained_ids)
    assert stale is not None and stale.superseded_by is not None
    assert len(restore_binds) == 2
    # SET value + project_id + closure marker are the three non-ID binds.
    assert [bind_count - 3 for bind_count in restore_binds] == [900, 101]
