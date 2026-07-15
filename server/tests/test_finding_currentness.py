"""Logical finding identity and exact graph-revision currentness."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api.findings import (  # noqa: E402
    findings_roi,
    findings_summary,
    list_findings as http_list_findings,
)
from app.artifacts.agent_context import _risk_findings  # noqa: E402
from app.finding_identity import (  # noqa: E402
    FindingIdentityError,
    canonical_finding_identity,
    finding_identity_key,
)
from app.graph_overlays import record_human_confirmation  # noqa: E402
from app.mcp.queries import list_findings as mcp_list_findings  # noqa: E402
from app.merge.findings import run_all  # noqa: E402
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead  # noqa: E402
from app.models.overlays import GraphEdgeRuntimeOverlay  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def disable_finding_notifications(monkeypatch):
    async def _noop(_findings):
        return None

    monkeypatch.setattr("app.notify.outbound.notify_new_findings", _noop)


async def _ready_project(
    session: AsyncSession,
    *,
    generation: int = 1,
    overlay_generation: int = 0,
) -> tuple[uuid.UUID, datetime]:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    session.add(
        Project(
            id=project_id,
            name=f"finding-currentness-{project_id.hex}",
            gitlab_project_id=int(project_id.int % 2_000_000_000),
            gitlab_url="https://example.invalid/finding-currentness",
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
            git_sha="a" * 40,
            scope="full",
            started_at=now,
            completed_at=now,
            **published_run_fields(
                generation=generation,
                published_at=now,
            ),
        )
    )
    await session.flush()
    session.add(
        GraphHead(
            project_id=project_id,
            current_run_id=run_id,
            generation=generation,
            overlay_generation=overlay_generation,
            state="ready",
            published_at=now,
        )
    )
    await session.commit()
    return project_id, now


async def _publish_head(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    generation: int,
    overlay_generation: int = 0,
) -> None:
    now = datetime.now(tz=timezone.utc)
    run_id = uuid.uuid4()
    session.add(
        AnalysisRun(
            id=run_id,
            project_id=project_id,
            status="completed",
            triggered_by="test:next-publication",
            git_sha=f"{generation:x}".rjust(40, "0"),
            scope="full",
            started_at=now,
            completed_at=now,
            **published_run_fields(
                generation=generation,
                published_at=now,
            ),
        )
    )
    await session.flush()
    head = await session.get(GraphHead, project_id)
    assert head is not None
    head.current_run_id = run_id
    head.generation = generation
    head.overlay_generation = overlay_generation
    head.published_at = now
    await session.commit()


def _missing_data_edge(
    project_id: uuid.UUID,
    *,
    source_id: str,
    target_id: str,
    valid_from: datetime,
) -> Edge:
    return Edge(
        id=uuid.uuid4(),
        project_id=project_id,
        source_id=source_id,
        target_id=target_id,
        kind="READS",
        data={},
        certainty="asserted",
        created_by=["test"],
        valid_from=valid_from,
    )


def test_identity_contract_is_canonical_and_rejects_ambiguous_subjects():
    edge = ("sym:한글", "data:orders", "READS")
    canonical = canonical_finding_identity(
        kind="schema_mismatch",
        subject_edge=edge,
    )
    key = finding_identity_key(kind="schema_mismatch", subject_edge=edge)

    assert canonical.startswith("mnemos.finding.identity.v1|kind=")
    assert len(key) == 64
    assert key == finding_identity_key(
        kind="schema_mismatch",
        subject_edge=edge,
    )
    assert key != finding_identity_key(
        kind="schema_mismatch",
        subject_edge=(edge[0], "data:other", edge[2]),
    )
    with pytest.raises(FindingIdentityError, match="exactly one"):
        finding_identity_key(
            kind="schema_mismatch",
            subject_node_id="sym:a",
            subject_edge=edge,
        )


@pytest.mark.asyncio
async def test_two_same_kind_edge_findings_have_distinct_logical_rows(database):
    async with database() as session:
        project_id, now = await _ready_project(session)
        edge_a = _missing_data_edge(
            project_id,
            source_id="sym:a",
            target_id="data:missing-a",
            valid_from=now,
        )
        edge_b = _missing_data_edge(
            project_id,
            source_id="sym:b",
            target_id="data:missing-b",
            valid_from=now,
        )
        session.add_all([edge_a, edge_b])
        await session.commit()

        stats = await run_all(session, project_id)
        rows = (
            await session.execute(
                select(Finding).where(
                    Finding.project_id == project_id,
                    Finding.kind == "schema_mismatch",
                )
            )
        ).scalars().all()

        assert stats["schema_mismatches"] == 2
        assert len(rows) == 2
        assert len({row.identity_key for row in rows}) == 2
        assert {row.subject_edge_id for row in rows} == {edge_a.id, edge_b.id}
        assert all(row.validated_graph_generation == 1 for row in rows)
        assert all(row.validated_overlay_generation == 0 for row in rows)
        assert all(row.validated_at is not None for row in rows)


@pytest.mark.asyncio
async def test_human_confirmation_resolves_unverified_claim(database):
    async with database() as session:
        project_id, now = await _ready_project(session)
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            source_id="sym:caller",
            target_id="sym:callee",
            kind="CALLS",
            data={},
            certainty="inferred",
            created_by=["test"],
            valid_from=now - timedelta(days=31),
        )
        session.add(edge)
        await session.commit()

        first = await run_all(session, project_id)
        assert first["unverified_claims"] == 1

        result = await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="edge",
            target_id=str(edge.id),
            action="confirm",
            actor="user:test",
            rationale="reviewed against source",
        )
        assert result.source_certainty == "inferred"
        assert result.effective_certainty == "asserted"
        await session.commit()

        second = await run_all(session, project_id)
        finding = (
            await session.execute(
                select(Finding).where(
                    Finding.project_id == project_id,
                    Finding.kind == "unverified_claim",
                )
            )
        ).scalar_one()

        assert second["unverified_claims"] == 0
        assert second["auto_resolved"] == 1
        assert finding.status == "resolved"
        assert finding.resolved_by == "system:graph-refresh"


@pytest.mark.asyncio
async def test_runtime_overlay_prevents_dead_path_finding(database):
    async with database() as session:
        project_id, now = await _ready_project(session, overlay_generation=1)
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            source_id="sym:caller",
            target_id="sym:callee",
            kind="CALLS",
            data={},
            certainty="asserted",
            created_by=["test"],
            valid_from=now - timedelta(days=31),
        )
        session.add(edge)
        session.add(
            GraphEdgeRuntimeOverlay(
                project_id=project_id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                kind=edge.kind,
                first_seen_at=now - timedelta(days=1),
                last_seen_at=now,
                hit_count=3,
                metadata_={"source": "test"},
            )
        )
        await session.commit()

        stats = await run_all(session, project_id)

        assert stats["dead_paths"] == 0
        assert (
            await session.execute(
                select(Finding).where(
                    Finding.project_id == project_id,
                    Finding.kind == "dead_path_suspected",
                )
            )
        ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_disappear_auto_resolve_reappear_and_user_disposition(database):
    async with database() as session:
        project_id, now = await _ready_project(session)
        first_edge = _missing_data_edge(
            project_id,
            source_id="sym:reader",
            target_id="data:missing",
            valid_from=now,
        )
        session.add(first_edge)
        await session.commit()
        await run_all(session, project_id)
        finding = (
            await session.execute(
                select(Finding).where(Finding.project_id == project_id)
            )
        ).scalar_one()
        original_id = finding.id

        first_edge.valid_to = now + timedelta(seconds=1)
        await _publish_head(session, project_id=project_id, generation=2)
        stats = await run_all(session, project_id)
        await session.refresh(finding)

        assert stats["auto_resolved"] == 1
        assert finding.status == "resolved"
        assert finding.resolved_by == "system:graph-refresh"
        assert finding.validated_graph_generation == 1
        assert await mcp_list_findings(session, project_id=project_id) == []

        second_edge = _missing_data_edge(
            project_id,
            source_id="sym:reader",
            target_id="data:missing",
            valid_from=now + timedelta(seconds=2),
        )
        session.add(second_edge)
        await _publish_head(session, project_id=project_id, generation=3)
        await run_all(session, project_id)
        await session.refresh(finding)

        assert finding.id == original_id
        assert finding.subject_edge_id == second_edge.id
        assert finding.status == "open"
        assert finding.resolved_at is None
        assert finding.resolved_by is None
        assert finding.validated_graph_generation == 3

        finding.status = "resolved"
        finding.resolved_at = now + timedelta(seconds=3)
        finding.resolved_by = "user:operator"
        await session.commit()
        second_edge.valid_to = now + timedelta(seconds=4)
        await _publish_head(session, project_id=project_id, generation=4)
        await run_all(session, project_id)

        third_edge = _missing_data_edge(
            project_id,
            source_id="sym:reader",
            target_id="data:missing",
            valid_from=now + timedelta(seconds=5),
        )
        session.add(third_edge)
        await _publish_head(session, project_id=project_id, generation=5)
        await run_all(session, project_id)
        await session.refresh(finding)

        assert finding.id == original_id
        assert finding.subject_edge_id == third_edge.id
        assert finding.status == "resolved"
        assert finding.resolved_by == "user:operator"
        assert finding.validated_graph_generation == 5


@pytest.mark.asyncio
async def test_current_consumers_require_exact_source_and_overlay_marker(database):
    async with database() as session:
        project_id, now = await _ready_project(
            session,
            generation=7,
            overlay_generation=3,
        )

        def _finding(
            name: str,
            *,
            graph_generation: int | None,
            overlay_generation: int | None,
            status: str = "open",
            risk: int = 10,
        ) -> Finding:
            return Finding(
                id=uuid.uuid4(),
                project_id=project_id,
                kind="schema_mismatch",
                identity_key=finding_identity_key(
                    kind="schema_mismatch",
                    subject_node_id=f"sym:{name}",
                ),
                severity="warning",
                status=status,
                subject_node_id=f"sym:{name}",
                detail={"name": name},
                risk_score=risk,
                first_seen_at=now,
                last_seen_at=now,
                resolved_at=(now if status == "resolved" else None),
                resolved_by=("user:operator" if status == "resolved" else None),
                validated_graph_generation=graph_generation,
                validated_overlay_generation=overlay_generation,
                validated_at=(
                    now
                    if graph_generation is not None
                    and overlay_generation is not None
                    else None
                ),
            )

        exact = _finding(
            "exact",
            graph_generation=7,
            overlay_generation=3,
            risk=11,
        )
        graph_stale = _finding(
            "graph-stale",
            graph_generation=6,
            overlay_generation=3,
            risk=20,
        )
        overlay_stale = _finding(
            "overlay-stale",
            graph_generation=7,
            overlay_generation=2,
            risk=30,
        )
        legacy = _finding(
            "legacy",
            graph_generation=None,
            overlay_generation=None,
            risk=40,
        )
        historical_resolved = _finding(
            "historical-resolved",
            graph_generation=6,
            overlay_generation=2,
            status="resolved",
            risk=50,
        )
        session.add_all(
            [exact, graph_stale, overlay_stale, legacy, historical_resolved]
        )
        await session.commit()

        http_rows = await http_list_findings(project_id, None, db=session)
        mcp_rows = await mcp_list_findings(session, project_id=project_id)
        agent_rows = await _risk_findings(session, project_id, limit=20)
        health = await findings_summary(project_id, None, db=session)
        roi = await findings_roi(project_id, None, db=session)

        assert [row.id for row in http_rows] == [exact.id]
        assert [row["id"] for row in mcp_rows] == [str(exact.id)]
        assert [row["finding_id"] for row in agent_rows] == [str(exact.id)]
        assert health["total"] == 1
        assert health["open"] == 1
        assert roi["open_risk_remaining"] == 11
        assert roi["risk_eliminated"] == 50
        assert roi["findings_resolved"] == 1

        # A source publication whose findings postprocess has not succeeded
        # must immediately hide every previous "current" row/open-risk value.
        await _publish_head(
            session,
            project_id=project_id,
            generation=8,
            overlay_generation=3,
        )
        assert await mcp_list_findings(session, project_id=project_id) == []
        roi_after_partial = await findings_roi(project_id, None, db=session)
        assert roi_after_partial["open_risk_remaining"] == 0
        assert roi_after_partial["risk_eliminated"] == 50


@pytest.mark.asyncio
async def test_database_rejects_duplicate_logical_identity(database):
    async with database() as session:
        project_id, now = await _ready_project(session)
        key = finding_identity_key(
            kind="schema_mismatch",
            subject_node_id="sym:duplicate",
        )
        for _ in range(2):
            session.add(
                Finding(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    kind="schema_mismatch",
                    identity_key=key,
                    severity="warning",
                    status="open",
                    subject_node_id="sym:duplicate",
                    detail={},
                    risk_score=1,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_detector_failure_rolls_back_entire_revision(monkeypatch, database):
    async with database() as session:
        project_id, now = await _ready_project(session)
        session.add(
            Edge(
                id=uuid.uuid4(),
                project_id=project_id,
                source_id="sym:claim",
                target_id="sym:unknown",
                kind="DEPENDS_ON",
                data={},
                certainty="inferred",
                created_by=["test"],
                valid_from=now - timedelta(days=60),
            )
        )
        await session.commit()

        async def _fail(*_args, **_kwargs):
            raise RuntimeError("detector failed")

        monkeypatch.setattr("app.merge.findings.detect_dynamic_calls", _fail)
        with pytest.raises(RuntimeError, match="detector failed"):
            await run_all(session, project_id)

        rows = (
            await session.execute(
                select(Finding).where(Finding.project_id == project_id)
            )
        ).scalars().all()
        assert rows == []


def test_0031_migration_pins_bounded_legacy_duplicate_rekey():
    migration = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0031_finding_graph_currentness.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "0031_finding_graph_currentness"' in migration
    assert 'down_revision = "0030_summary_graph_generation"' in migration
    assert "LIMIT 1000" in migration
    assert "row_number() OVER" in migration
    assert "duplicate_rank > 1" in migration
    assert "|legacy_id=" in migration
    assert "uq_findings_project_kind_identity" in migration
