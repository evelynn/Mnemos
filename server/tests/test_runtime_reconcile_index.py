"""Behavioural guards for the indexed runtime reconciliation.

``reconcile_observations`` used to re-select and re-scan every current
edge of the observed kind *per observation* (O(observations x edges)).
It now loads and indexes current edges once per observed kind. These
tests pin the behaviour that refactor must not change:

* a templated contract-node route still reconciles a literal observation,
* many observations of one kind still each resolve to their own edge,
* edge selection is deterministic when several edges could match,
* an empty observed operation still counts as unmatched.

The equivalent upstream test (``test_pr25_otlp_tier2`` /
``test_graph_overlays``) needs a live database in some configurations;
this file runs on in-memory SQLite so the contract stays covered here.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "runtime-reconcile-index-test")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.merge.runtime import reconcile_observations  # noqa: E402
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.overlays import GraphEdgeRuntimeOverlay  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.models.runtime import RuntimeObservation  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


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
            name="reconcile-index",
            gitlab_project_id=94001,
            gitlab_url="https://example.invalid/reconcile-index",
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
    return org_id, project_id, now


def _exposes(project_id: uuid.UUID, now: datetime, source: str, contract: str):
    """A component -> contract EXPOSES edge, keyed by the contract node id."""
    return [
        Node(
            id=source,
            project_id=project_id,
            valid_from=now,
            kind="Component",
            data={"name": source},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
        Node(
            id=contract,
            project_id=project_id,
            valid_from=now,
            kind="Contract",
            data={"name": contract},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
        Edge(
            source_id=source,
            target_id=contract,
            project_id=project_id,
            valid_from=now,
            kind="EXPOSES",
            data={},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
    ]


def _edge_with_data(
    project_id: uuid.UUID,
    now: datetime,
    *,
    source: str,
    target: str,
    data: dict,
):
    """An EXPOSES edge whose route lives on ``edge.data`` rather than the
    target contract node id."""
    return [
        Node(
            id=source,
            project_id=project_id,
            valid_from=now,
            kind="Component",
            data={"name": source},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
        Node(
            id=target,
            project_id=project_id,
            valid_from=now,
            kind="Contract",
            data={"name": target},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
        Edge(
            source_id=source,
            target_id=target,
            project_id=project_id,
            valid_from=now,
            kind="EXPOSES",
            data=data,
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
    ]


def _observation(org_id, project_id, now, *, operation: str, kind="EXPOSES"):
    return RuntimeObservation(
        id=uuid.uuid4(),
        organization_id=org_id,
        project_id=project_id,
        service="svc",
        operation=operation,
        kind=kind,
        seen_count=2,
        first_seen_at=now - timedelta(minutes=5),
        last_seen_at=now,
    )


async def _exercised_targets(session, project_id) -> set[str]:
    rows = (
        await session.execute(
            GraphEdgeRuntimeOverlay.__table__.select().where(
                GraphEdgeRuntimeOverlay.project_id == project_id
            )
        )
    ).mappings().all()
    return {row["target_id"] for row in rows}


@pytest.mark.asyncio
async def test_literal_observation_matches_templated_contract_node(database):
    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        session.add_all(
            _exposes(project_id, now, "comp:orders", "contract:GET /orders/{id}")
        )
        session.add(_observation(org_id, project_id, now, operation="/orders/42"))
        await session.commit()

        result = await reconcile_observations(session, project_id, org_id)
        assert result["matched"] == 1
        assert result["unmatched"] == 0
        assert await _exercised_targets(session, project_id) == {
            "contract:GET /orders/{id}"
        }


@pytest.mark.asyncio
async def test_many_observations_each_resolve_to_their_own_edge(database):
    """The per-kind index is built once and reused across observations."""
    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        routes = ["/alpha", "/beta", "/gamma", "/delta"]
        for route in routes:
            session.add_all(
                _exposes(
                    project_id, now, f"comp{route}", f"contract:GET {route}"
                )
            )
        for route in routes:
            session.add(_observation(org_id, project_id, now, operation=route))
        # Never matches anything — must stay unmatched, not borrow an edge.
        session.add(
            _observation(org_id, project_id, now, operation="/nonexistent")
        )
        await session.commit()

        result = await reconcile_observations(session, project_id, org_id)
        assert result["matched"] == len(routes)
        assert result["unmatched"] == 1
        assert await _exercised_targets(session, project_id) == {
            f"contract:GET {route}" for route in routes
        }


@pytest.mark.asyncio
async def test_ambiguous_match_is_deterministic(database):
    """Two edges can carry the same route; selection must not vary."""
    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        session.add_all(
            _exposes(project_id, now, "comp:aaa", "contract:GET /shared")
        )
        session.add(
            Edge(
                source_id="comp:zzz",
                target_id="contract:GET /shared-alt",
                project_id=project_id,
                valid_from=now,
                kind="EXPOSES",
                data={"route": "/shared"},
                certainty="asserted",
                created_by=["ggoss-py"],
            )
        )
        session.add(
            Node(
                id="comp:zzz",
                project_id=project_id,
                valid_from=now,
                kind="Component",
                data={"name": "zzz"},
                certainty="asserted",
                created_by=["ggoss-py"],
            )
        )
        session.add(_observation(org_id, project_id, now, operation="/shared"))
        await session.commit()

        result = await reconcile_observations(session, project_id, org_id)
        assert result["matched"] == 1
        # Ordered by (source_id, target_id): "comp:aaa" precedes "comp:zzz".
        assert await _exercised_targets(session, project_id) == {
            "contract:GET /shared"
        }


@pytest.mark.asyncio
async def test_edge_scan_is_one_query_per_kind_not_per_observation(database):
    """The efficiency claim itself, as an executable oracle.

    The old loop issued one full current-edge SELECT per observation. With
    8 observations over 2 kinds that was 8 edge queries; it must now be 2.
    """
    import sqlalchemy as sa

    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        for index in range(4):
            session.add_all(
                _exposes(
                    project_id,
                    now,
                    f"comp:svc{index}",
                    f"contract:GET /route{index}",
                )
            )
        for index in range(4):
            session.add(
                _observation(
                    org_id, project_id, now, operation=f"/route{index}"
                )
            )
            session.add(
                _observation(
                    org_id,
                    project_id,
                    now,
                    operation=f"/route{index}",
                    kind="CALLS",
                )
            )
        await session.commit()

        edge_selects = 0
        unordered_edge_selects = 0

        def _count(conn, cursor, statement, parameters, context, executemany):
            nonlocal edge_selects, unordered_edge_selects
            normalized = " ".join(statement.split()).lower()
            if normalized.startswith("select") and " from edges" in normalized:
                edge_selects += 1
                if "order by" not in normalized:
                    unordered_edge_selects += 1

        bind = session.get_bind()
        target = getattr(bind, "sync_engine", bind)
        sa.event.listen(target, "before_cursor_execute", _count)
        try:
            result = await reconcile_observations(session, project_id, org_id)
        finally:
            sa.event.remove(target, "before_cursor_execute", _count)

    assert result["matched"] == 4
    assert result["unmatched"] == 4, "CALLS observations have no CALLS edge"
    assert edge_selects == 2, (
        f"expected one current-edge scan per observed kind, got {edge_selects}"
    )
    # The candidate scan must be explicitly ordered so that, when several
    # edges match one observation, the winner does not depend on storage
    # order. SQLite happens to return these rows via the current-identity
    # index (already sorted), so only the emitted clause is observable
    # here; the effect itself shows up on PostgreSQL heap order.
    assert unordered_edge_selects == 0, "current-edge scan lost its ORDER BY"


@pytest.mark.asyncio
async def test_first_match_wins_when_raw_and_templated_edges_both_match(database):
    """Pins ``min(positions)`` *and* the explicit edge ordering.

    Two edges match one observation through different keys: the earlier
    edge carries the templated route (matched after normalisation), the
    later one carries the literal route (matched raw). The pre-index loop
    broke on the first matching edge in order, so the templated edge wins.
    Taking the last position instead — or dropping the ORDER BY, since the
    literal edge is inserted first here — selects the other edge.
    """
    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        # Inserted first, sorts second: natural row order != sorted order.
        session.add_all(
            _edge_with_data(
                project_id,
                now,
                source="comp:bbb",
                target="contract:bbb",
                data={"operation": "/api/orders/42"},
            )
        )
        session.add_all(
            _edge_with_data(
                project_id,
                now,
                source="comp:aaa",
                target="contract:aaa",
                data={"operation": "/api/orders/{id}"},
            )
        )
        session.add(
            _observation(org_id, project_id, now, operation="/api/orders/42")
        )
        await session.commit()

        result = await reconcile_observations(session, project_id, org_id)
        assert result["matched"] == 1
        assert await _exercised_targets(session, project_id) == {"contract:aaa"}


@pytest.mark.asyncio
async def test_empty_operation_is_unmatched(database):
    async with database() as session:
        org_id, project_id, now = await _ready_project(session)
        session.add_all(
            _exposes(project_id, now, "comp:orders", "contract:GET /orders")
        )
        session.add(_observation(org_id, project_id, now, operation=""))
        await session.commit()

        result = await reconcile_observations(session, project_id, org_id)
        assert result == {"matched": 0, "unmatched": 1, "new_hits": 0}
        assert await _exercised_targets(session, project_id) == set()
