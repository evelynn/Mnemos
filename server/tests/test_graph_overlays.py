"""Durable human/runtime overlay contracts across source publication."""

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

from app.api.analysis import graph_component_map  # noqa: E402
from app.extractor.validator import validate_claims  # noqa: E402
from app.graph_overlays import (  # noqa: E402
    GraphOverlayUnavailable,
    edge_identity,
    edge_read_view,
    effective_certainty,
    load_edge_overlays,
    load_node_human_overlays,
    materialize_edge_data,
    node_read_view,
    record_human_confirmation,
    strip_durable_overlay_fields,
)
from app.graph_publication import (  # noqa: E402
    GraphGenerationChanged,
    capture_graph_base_generation,
    promote_staged_graph,
    read_graph_stamp,
    revalidate_graph_stamp,
    seal_graph_coverage,
    stage_edge,
    stage_node,
)
from app.merge.runtime import reconcile_observations  # noqa: E402
from app.mcp.queries import find_callers, find_runtime_path, get_symbol  # noqa: E402
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.overlays import (  # noqa: E402
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeCursor,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)
from app.models.projects import Project  # noqa: E402
from app.models.runtime import RuntimeObservation  # noqa: E402
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


async def _ready_project(session: AsyncSession):
    org_id = uuid.uuid4()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    session.add(
        Organization(id=org_id, slug=f"org-{org_id.hex}", display_name="Org")
    )
    session.add(
        Project(
            id=project_id,
            name="overlay-test",
            gitlab_project_id=1,
            gitlab_url="https://example.invalid/repo.git",
            default_branch="main",
            languages=["Python"],
            organization_id=org_id,
        )
    )
    await session.flush()
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
            **published_run_fields(generation=1, published_at=now),
        )
    )
    await session.flush()
    session.add(
        GraphHead(
            project_id=project_id,
            current_run_id=run_id,
            generation=1,
            state="ready",
            published_at=now,
        )
    )
    await session.flush()
    return org_id, project_id, run_id, now


def test_source_fields_and_effective_certainty_are_separate():
    source = {
        "name": "checkout",
        "human_confirmation": {"action": "confirm"},
        "exercised": "true",
        "last_seen_at": "2026-01-01T00:00:00+00:00",
        "hit_count": 999,
    }
    clean_node = strip_durable_overlay_fields(source, target_kind="node")
    clean_edge = strip_durable_overlay_fields(source, target_kind="edge")

    assert "human_confirmation" not in clean_node
    assert clean_node["exercised"] == "true"  # node producer signal is valid
    assert clean_edge == {"name": "checkout"}
    assert source["human_confirmation"]["action"] == "confirm"  # pure
    assert effective_certainty("inferred", "confirm") == "asserted"
    assert effective_certainty("verified", "confirm") == "verified"
    assert effective_certainty("inferred", "dispute") == "inferred"


@pytest.mark.asyncio
async def test_http_component_map_and_mcp_share_one_overlay_revision(database):
    """Human evidence must mean the same thing to UI and AI re-query clients."""

    async with database() as session:
        _, project_id, _, now = await _ready_project(session)
        caller = Node(
            id="sym:parity:caller",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "caller"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        callee = Node(
            id="sym:parity:callee",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "callee"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        call = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now,
            source_id=caller.id,
            target_id=callee.id,
            kind="CALLS",
            data={"invocation_site": "caller:1"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        runtime = GraphEdgeRuntimeOverlay(
            project_id=project_id,
            source_id=caller.id,
            target_id=callee.id,
            kind="CALLS",
            first_seen_at=now,
            last_seen_at=now + timedelta(seconds=1),
            hit_count=7,
            metadata_={"source": "test"},
        )
        session.add_all([caller, callee, call, runtime])
        await session.commit()

        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="node",
            target_id=callee.id,
            action="confirm",
            actor="user:reviewer",
            rationale="Reviewed the callee against the exact source revision.",
        )
        await session.commit()
        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="edge",
            target_id=str(call.id),
            action="confirm",
            actor="user:reviewer",
            rationale="Reviewed the call site against the exact source revision.",
        )
        await session.commit()

        stamp = await read_graph_stamp(session, project_id=project_id)
        component_map = await graph_component_map(
            project_id, None, db=session
        )
        mcp_symbol = await get_symbol(
            session, project_id=project_id, symbol_id=callee.id
        )
        mcp_callers = await find_callers(
            session, project_id=project_id, symbol_id=callee.id
        )
        runtime_path = await find_runtime_path(
            session,
            project_id=project_id,
            entry_contract_id=caller.id,
        )
        accepted, rejected = await validate_claims(
            session,
            project_id=project_id,
            claims=[
                {
                    "claim": "The confirmed caller reaches the callee.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": callee.id,
                            "certainty": "inferred",
                        },
                        {
                            "kind": "edge",
                            "edge_id": str(call.id),
                            "certainty": "inferred",
                        },
                    ],
                }
            ],
        )
        await revalidate_graph_stamp(session, stamp=stamp)

        http_node = next(
            row for row in component_map["nodes"] if row["id"] == callee.id
        )
        http_edge = next(
            row for row in component_map["edges"] if row["id"] == str(call.id)
        )
        assert mcp_symbol is not None
        mcp_node = mcp_symbol["symbol"]
        mcp_edge = mcp_callers["edges"][0]

        for field in (
            "certainty",
            "source_certainty",
            "effective_certainty",
            "confirmed",
        ):
            assert mcp_node[field] == http_node[field]
            assert mcp_edge[field] == http_edge[field]
        assert http_node["certainty"] == "asserted"
        assert http_edge["certainty"] == "asserted"
        assert http_edge["exercised"] is True
        assert mcp_edge["exercised"] is True
        assert mcp_edge["runtime"]["hit_count"] == 7
        assert runtime_path["common_paths"] == [
            {"frequency": 7, "chain": [callee.id], "depth": 1}
        ]
        assert rejected == []
        assert [
            evidence["certainty"]
            for evidence in accepted[0]["evidence"]
        ] == ["asserted", "asserted"]
        assert stamp.overlay_generation == 2


@pytest.mark.asyncio
async def test_human_confirmation_survives_physical_node_replacement(database):
    async with database() as session:
        _, project_id, _, now = await _ready_project(session)
        old = Node(
            id="sym:checkout",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "checkout"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        session.add(old)
        await session.commit()
        before_overlay = await read_graph_stamp(session, project_id=project_id)
        await session.commit()

        result = await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="node",
            target_id=old.id,
            action="confirm",
            actor="user:operator",
            rationale="Reviewed against the implementation and tests.",
            recorded_at=now + timedelta(seconds=1),
        )
        await session.commit()

        with pytest.raises(GraphGenerationChanged, match="overlay 0->1"):
            await revalidate_graph_stamp(session, stamp=before_overlay)
        after_overlay = await read_graph_stamp(session, project_id=project_id)
        assert after_overlay.generation == before_overlay.generation
        assert after_overlay.overlay_generation == 1
        await session.commit()

        assert result.source_certainty == "inferred"
        assert result.effective_certainty == "asserted"
        current = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == old.id,
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        # Source truth is immutable; only compatibility data is materialized.
        assert current.certainty == "inferred"
        assert current.data["human_confirmation"]["action"] == "confirm"
        assert (
            await session.execute(select(GraphNodeHumanOverlay))
        ).scalar_one().actor == "user:operator"

        # Simulate a source promotion replacing the physical bitemporal row
        # without copying its compatibility JSON.
        replacement_at = now + timedelta(seconds=2)
        current.valid_to = replacement_at
        replacement = Node(
            id=current.id,
            project_id=project_id,
            valid_from=replacement_at,
            kind="Symbol",
            data={"name": "checkout"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        session.add(replacement)
        await session.commit()

        overlays = await load_node_human_overlays(
            session, project_id=project_id, node_ids=[replacement.id]
        )
        view = node_read_view(replacement, overlays[replacement.id])
        assert replacement.certainty == "inferred"
        assert view["source_certainty"] == "inferred"
        assert view["effective_certainty"] == "asserted"
        assert view["data"]["human_confirmation"]["by"] == "user:operator"


@pytest.mark.asyncio
async def test_overlay_revision_cache_tracks_transaction_object_across_commits(
    database,
):
    async with database() as session:
        _, project_id, _, now = await _ready_project(session)
        node = Node(
            id="sym:transaction-token",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "transaction-token"},
            certainty="inferred",
            created_by=["agent-extract"],
        )
        session.add(node)
        await session.commit()

        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="node",
            target_id=node.id,
            action="confirm",
            actor="user:operator",
            rationale="first transaction",
            recorded_at=now + timedelta(seconds=1),
        )
        first_token = session.info["mnemos_overlay_generation_advanced"]
        assert first_token[0] is session.get_transaction()
        await session.commit()

        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="node",
            target_id=node.id,
            action="dispute",
            actor="user:operator",
            rationale="second transaction",
            recorded_at=now + timedelta(seconds=2),
        )
        second_token = session.info["mnemos_overlay_generation_advanced"]
        assert second_token[0] is session.get_transaction()
        assert second_token[0] is not first_token[0]
        await session.commit()

        head = await session.get(GraphHead, project_id)
        assert head is not None
        assert head.overlay_generation == 2


@pytest.mark.asyncio
async def test_edge_confirmation_uses_logical_identity_not_physical_uuid(database):
    async with database() as session:
        _, project_id, _, now = await _ready_project(session)
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now,
            source_id="sym:client",
            target_id="contract:GET /orders",
            kind="CALLS",
            data={},
            certainty="inferred",
            created_by=["ggoss-ts"],
        )
        session.add(edge)
        await session.commit()
        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="edge",
            target_id=str(edge.id),
            action="dispute",
            actor="user:reviewer",
            rationale="The static call resolves to a different service.",
            recorded_at=now + timedelta(seconds=1),
        )
        await session.commit()

        key = edge_identity(edge)
        edge.valid_to = now + timedelta(seconds=2)
        replacement = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now + timedelta(seconds=2),
            source_id=key[0],
            target_id=key[1],
            kind=key[2],
            data={},
            certainty="asserted",
            created_by=["ggoss-ts"],
        )
        session.add(replacement)
        await session.commit()

        bundle = await load_edge_overlays(
            session, project_id=project_id, identities=[key]
        )
        assert len((await session.execute(select(GraphEdgeHumanOverlay))).all()) == 1
        view = edge_read_view(replacement, bundle.human[key], None)
        assert view["confirmed"] == "dispute"
        assert view["source_certainty"] == "asserted"
        assert view["effective_certainty"] == "asserted"


@pytest.mark.asyncio
async def test_runtime_overlay_survives_promotion_and_replay_is_idempotent(database):
    async with database() as session:
        org_id, project_id, _, now = await _ready_project(session)
        source = Node(
            id="sym:orders:create",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "create"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        contract = Node(
            id="contract:POST /orders",
            project_id=project_id,
            valid_from=now,
            kind="Contract",
            data={"name": "POST /orders"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now,
            source_id=source.id,
            target_id=contract.id,
            kind="EXPOSES",
            data={},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        observation = RuntimeObservation(
            id=uuid.uuid4(),
            organization_id=org_id,
            project_id=project_id,
            service="orders",
            operation="/orders",
            kind="EXPOSES",
            seen_count=3,
            first_seen_at=now - timedelta(minutes=5),
            last_seen_at=now,
        )
        session.add_all([source, contract, edge, observation])
        await session.commit()

        first = await reconcile_observations(session, project_id, org_id)
        await session.commit()
        assert first == {"matched": 1, "unmatched": 0, "new_hits": 3}
        head = await session.get(GraphHead, project_id)
        assert head is not None and head.overlay_generation == 1
        await session.commit()

        replay = await reconcile_observations(session, project_id, org_id)
        await session.commit()
        assert replay == {"matched": 1, "unmatched": 0, "new_hits": 0}
        head = await session.get(GraphHead, project_id)
        assert head is not None and head.overlay_generation == 1
        overlay = (
            await session.execute(select(GraphEdgeRuntimeOverlay))
        ).scalar_one()
        cursor = (
            await session.execute(select(GraphEdgeRuntimeCursor))
        ).scalar_one()
        assert overlay.hit_count == 3
        assert cursor.applied_seen_count == 3

        observation.seen_count = 5
        observation.last_seen_at = now + timedelta(minutes=1)
        await session.commit()
        advanced = await reconcile_observations(session, project_id, org_id)
        await session.commit()
        assert advanced["new_hits"] == 2
        head = await session.get(GraphHead, project_id)
        assert head is not None and head.overlay_generation == 2
        await session.refresh(overlay)
        assert overlay.hit_count == 5

        # A fresh physical edge has no embedded runtime fields, but the
        # logical overlay remains queryable and is re-materializable.
        replacement_at = now + timedelta(minutes=2)
        edge.valid_to = replacement_at
        replacement = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=replacement_at,
            source_id=edge.source_id,
            target_id=edge.target_id,
            kind=edge.kind,
            data={},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        session.add(replacement)
        await session.commit()

        key = edge_identity(replacement)
        bundle = await load_edge_overlays(
            session, project_id=project_id, identities=[key]
        )
        data = materialize_edge_data(
            replacement.data, bundle.human.get(key), bundle.runtime[key]
        )
        assert data["exercised"] == "true"
        assert data["hit_count"] == 5

        # The central graph serializer consumes the overlay even before a
        # promoter compatibility-materializes it into replacement.data.
        graph = await graph_component_map(project_id, None, db=session)
        shown = next(item for item in graph["edges"] if item["id"] == str(replacement.id))
        assert shown["exercised"] is True


@pytest.mark.asyncio
async def test_migrated_runtime_credit_is_consumed_once_and_new_hits_count(database):
    """Legacy hit credit covers only observations present at migration time."""

    async with database() as session:
        org_id, project_id, _, now = await _ready_project(session)
        source = Node(
            id="sym:migrated:create",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "create"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        contract = Node(
            id="contract:POST /migrated",
            project_id=project_id,
            valid_from=now,
            kind="Contract",
            data={"name": "POST /migrated"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now,
            source_id=source.id,
            target_id=contract.id,
            kind="EXPOSES",
            data={},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        runtime = GraphEdgeRuntimeOverlay(
            project_id=project_id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            kind=edge.kind,
            first_seen_at=now - timedelta(hours=1),
            last_seen_at=now,
            hit_count=10,
            metadata_={
                "migrated_from_edge_data": True,
                "migrated_at": now.isoformat(),
                "legacy_hit_credit_remaining": 10,
            },
            updated_at=now,
        )
        observations = [
            RuntimeObservation(
                id=uuid.uuid4(),
                organization_id=org_id,
                project_id=project_id,
                service="legacy-a",
                operation="/migrated",
                kind="EXPOSES",
                seen_count=6,
                first_seen_at=now - timedelta(minutes=20),
                last_seen_at=now - timedelta(minutes=5),
            ),
            RuntimeObservation(
                id=uuid.uuid4(),
                organization_id=org_id,
                project_id=project_id,
                service="legacy-b",
                operation="/migrated",
                kind="EXPOSES",
                seen_count=4,
                first_seen_at=now - timedelta(minutes=10),
                last_seen_at=now - timedelta(minutes=2),
            ),
            RuntimeObservation(
                id=uuid.uuid4(),
                organization_id=org_id,
                project_id=project_id,
                service="post-migration",
                operation="/migrated",
                kind="EXPOSES",
                seen_count=1,
                first_seen_at=now + timedelta(seconds=1),
                last_seen_at=now + timedelta(seconds=1),
            ),
        ]
        session.add_all([source, contract, edge, runtime, *observations])
        await session.commit()

        first = await reconcile_observations(session, project_id, org_id)
        await session.commit()
        assert first == {"matched": 3, "unmatched": 0, "new_hits": 1}
        await session.refresh(runtime)
        assert runtime.hit_count == 11
        assert runtime.metadata_["legacy_hit_credit_remaining"] == 0

        replay = await reconcile_observations(session, project_id, org_id)
        await session.commit()
        assert replay == {"matched": 3, "unmatched": 0, "new_hits": 0}
        await session.refresh(runtime)
        assert runtime.hit_count == 11


@pytest.mark.asyncio
async def test_actual_atomic_promotion_reprojects_human_and_runtime_overlays(database):
    async with database() as session:
        org_id, project_id, _, now = await _ready_project(session)
        symbol = Node(
            id="sym:checkout",
            project_id=project_id,
            valid_from=now,
            kind="Symbol",
            data={"name": "checkout-v1"},
            certainty="inferred",
            created_by=["ggoss-py"],
        )
        contract = Node(
            id="contract:POST /checkout",
            project_id=project_id,
            valid_from=now,
            kind="Contract",
            data={"name": "POST /checkout"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        edge = Edge(
            id=uuid.uuid4(),
            project_id=project_id,
            valid_from=now,
            source_id=symbol.id,
            target_id=contract.id,
            kind="EXPOSES",
            data={"callsite": "old"},
            certainty="asserted",
            created_by=["ggoss-py"],
        )
        observation = RuntimeObservation(
            id=uuid.uuid4(),
            organization_id=org_id,
            project_id=project_id,
            service="checkout",
            operation="/checkout",
            kind="EXPOSES",
            seen_count=4,
            first_seen_at=now - timedelta(minutes=2),
            last_seen_at=now,
        )
        session.add_all([symbol, contract, edge, observation])
        await session.commit()

        await record_human_confirmation(
            session,
            project_id=project_id,
            target_kind="node",
            target_id=symbol.id,
            action="confirm",
            actor="user:operator",
            rationale="Confirmed against the checked-out implementation.",
            recorded_at=now + timedelta(seconds=1),
        )
        await session.commit()
        await reconcile_observations(session, project_id, org_id)
        await session.commit()
        head_before_promotion = await session.get(GraphHead, project_id)
        assert head_before_promotion is not None
        assert head_before_promotion.overlay_generation == 2
        await session.commit()

        next_run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=next_run_id,
                project_id=project_id,
                status="running",
                triggered_by="test:overlay-promotion",
                git_sha="b" * 40,
                scope="incremental",
                started_at=now + timedelta(minutes=1),
            )
        )
        await session.flush()
        assert (
            await capture_graph_base_generation(
                session, project_id=project_id, run_id=next_run_id
            )
            == 1
        )
        await stage_node(
            session,
            project_id=project_id,
            run_id=next_run_id,
            node_id=symbol.id,
            kind="Symbol",
            data={
                "name": "checkout-v2",
                # Neither source nor a model may counterfeit durable evidence.
                "human_confirmation": {"action": "dispute", "by": "analyzer"},
            },
            certainty="inferred",
            source_name="ggoss-py",
        )
        await stage_edge(
            session,
            project_id=project_id,
            run_id=next_run_id,
            source_id=edge.source_id,
            target_id=edge.target_id,
            kind=edge.kind,
            data={
                "callsite": "new",
                "exercised": "false",
                "hit_count": 999,
            },
            certainty="asserted",
            source_name="ggoss-py",
        )
        await seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=next_run_id,
            authoritative_sources=[],
        )
        await session.commit()

        promoted = await promote_staged_graph(
            session,
            project_id=project_id,
            run_id=next_run_id,
        )
        assert promoted.generation == 2

        current_symbol = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id,
                    Node.id == symbol.id,
                    Node.valid_to.is_(None),
                )
            )
        ).scalar_one()
        current_edge = (
            await session.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.source_id == edge.source_id,
                    Edge.target_id == edge.target_id,
                    Edge.kind == edge.kind,
                    Edge.valid_to.is_(None),
                )
            )
        ).scalar_one()
        promoted_head = await session.get(GraphHead, project_id)
        assert promoted_head is not None
        assert promoted_head.overlay_generation == 2
        assert current_symbol.certainty == "inferred"
        assert current_symbol.data["name"] == "checkout-v2"
        assert current_symbol.data["human_confirmation"]["action"] == "confirm"
        assert current_symbol.data["human_confirmation"]["by"] == "user:operator"
        assert current_edge.data["callsite"] == "new"
        assert current_edge.data["exercised"] == "true"
        assert current_edge.data["hit_count"] == 4

        overlays = await load_node_human_overlays(
            session, project_id=project_id, node_ids=[current_symbol.id]
        )
        view = node_read_view(current_symbol, overlays[current_symbol.id])
        assert view["source_certainty"] == "inferred"
        assert view["effective_certainty"] == "asserted"


@pytest.mark.asyncio
async def test_overlay_write_fails_closed_without_ready_head(database):
    async with database() as session:
        org_id = uuid.uuid4()
        project_id = uuid.uuid4()
        session.add(Organization(id=org_id, slug=org_id.hex, display_name="Org"))
        session.add(
            Project(
                id=project_id,
                name="not-ready",
                gitlab_project_id=2,
                gitlab_url="https://example.invalid/repo.git",
                default_branch="main",
                languages=[],
                organization_id=org_id,
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=None,
                generation=0,
                state="needs_rebuild",
                published_at=None,
            )
        )
        await session.commit()

        with pytest.raises(GraphOverlayUnavailable):
            await record_human_confirmation(
                session,
                project_id=project_id,
                target_kind="node",
                target_id="sym:missing",
                action="confirm",
                actor="user:test",
                rationale="This graph has not been atomically published yet.",
            )
        await session.rollback()
        assert (await session.execute(select(GraphNodeHumanOverlay))).first() is None


@pytest.mark.asyncio
async def test_runtime_cursor_database_fks_reject_cross_project_or_missing_overlay(
    database,
):
    async with database() as session:
        org_a, project_a, _, now = await _ready_project(session)
        await session.commit()
        org_b, project_b, _, _ = await _ready_project(session)
        await session.commit()

        observation_b = RuntimeObservation(
            id=uuid.uuid4(),
            organization_id=org_b,
            project_id=project_b,
            service="other",
            operation="/other",
            kind="CALLS",
            seen_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        runtime_a = GraphEdgeRuntimeOverlay(
            project_id=project_a,
            source_id="sym:a",
            target_id="sym:b",
            kind="CALLS",
            first_seen_at=now,
            last_seen_at=now,
            hit_count=1,
            metadata_={"source": "test"},
        )
        session.add_all([observation_b, runtime_a])
        await session.commit()

        # The observation exists, and the logical overlay exists, but they
        # belong to different projects.  Writer-side filtering is not the
        # only tenant boundary: the composite DB FK rejects the cursor.
        session.add(
            GraphEdgeRuntimeCursor(
                project_id=project_a,
                source_id=runtime_a.source_id,
                target_id=runtime_a.target_id,
                kind=runtime_a.kind,
                observation_id=observation_b.id,
                applied_seen_count=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        observation_a = RuntimeObservation(
            id=uuid.uuid4(),
            organization_id=org_a,
            project_id=project_a,
            service="missing-edge",
            operation="/missing",
            kind="CALLS",
            seen_count=1,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(observation_a)
        await session.commit()
        session.add(
            GraphEdgeRuntimeCursor(
                project_id=project_a,
                source_id="sym:missing",
                target_id="sym:missing-target",
                kind="CALLS",
                observation_id=observation_a.id,
                applied_seen_count=1,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()


def test_overlay_migration_is_linear_after_atomic_publication():
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0028_graph_fact_overlays.py"
    ).read_text(encoding="utf-8")
    assert 'down_revision = "0027_atomic_graph_publication"' in migration
    for table in (
        "graph_node_human_overlays",
        "graph_edge_human_overlays",
        "graph_edge_runtime_overlays",
        "graph_edge_runtime_cursors",
    ):
        assert table in migration
    # Durable overlays outlive physical bitemporal rows, so the upgrade must
    # recover the latest evidence-bearing historical version even when the
    # logical fact is absent at migration time.
    assert "valid_to IS NULL" not in migration
    assert "'migrated_at', CURRENT_TIMESTAMP" in migration
    assert "'legacy_hit_credit_remaining'" in migration
