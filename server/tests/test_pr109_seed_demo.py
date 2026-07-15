"""PR-109 — ``python -m app.cli seed-demo`` behavioural tests.

Karpathy policy in action: pure-function builders are exercised
directly, not just grep'd for. ``_build_graph``, ``_build_findings``,
``_build_runs``, ``_build_summaries`` all return ORM instances that
are inspected for the structural invariants the dashboard depends on:

- every finding's ``subject_node_id`` (when set) corresponds to a
  real node in the graph (otherwise the dashboard's drill-down breaks)
- exactly one P1 finding so the new "Triage now" card has something
  to show on the demo dashboard
- the duplicate_endpoint detector's seed (two EXPOSES edges with the
  same contract target) actually exists in the edge list
- the schema_mismatch detector's seed (WRITES → DataEntity that
  doesn't appear as a current valid node) is wired
- the opaque_component_failing detector's seed (a Component with
  opacity=binary plus a CALLS edge with runtime_errors > 0) is wired
- the three analysis runs cover safe status values the analysis
  tab renders (completed / failed / queued)

The ``seed_demo`` async orchestrator itself is integration-tested
with a mock SessionLocal — verifying it commits, doesn't double-add
the demo user on a re-run, and refuses on MNEMOS_ENV=production
without --force.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


PID = uuid.UUID("00000000-0000-0000-0000-0000deadbeef")


# ─── pure builders (real execution, not grep) ─────────────────────


def test_build_graph_returns_nodes_and_edges():
    from app.seed_demo import _build_graph

    nodes, edges = _build_graph(PID)
    assert len(nodes) >= 15, f"expected ≥15 demo nodes, got {len(nodes)}"
    assert len(edges) >= 10, f"expected ≥10 demo edges, got {len(edges)}"
    # Every node carries the project id we passed in.
    assert all(n.project_id == PID for n in nodes)
    assert all(e.project_id == PID for e in edges)
    # Every node has a non-empty kind from the documented set.
    KINDS = {"Component", "Symbol", "Contract", "DataEntity"}
    bad = [n for n in nodes if n.kind not in KINDS]
    assert not bad, f"nodes with unexpected kind: {[(n.id, n.kind) for n in bad]}"


def test_graph_covers_all_dashboard_panels():
    """Every kind the GUI renders must appear — otherwise the tab
    is empty for the demo user and they think the feature is broken."""
    from app.seed_demo import _build_graph

    nodes, _ = _build_graph(PID)
    kinds = {n.kind for n in nodes}
    assert kinds == {"Component", "Symbol", "Contract", "DataEntity"}


def test_duplicate_endpoint_seed_is_present():
    """The duplicate_endpoint detector flags Contract nodes whose
    EXPOSES inbound edges come from ≥2 distinct symbols. Confirm
    the seed actually creates such a contract."""
    from app.seed_demo import _build_graph

    _, edges = _build_graph(PID)
    exposes_by_contract: dict[str, set[str]] = {}
    for e in edges:
        if e.kind == "EXPOSES":
            exposes_by_contract.setdefault(e.target_id, set()).add(e.source_id)
    duped = {c: srcs for c, srcs in exposes_by_contract.items() if len(srcs) >= 2}
    assert duped, "demo graph must include at least one duplicated EXPOSES"


def test_schema_mismatch_seed_is_present():
    """WRITES/READS edges whose target isn't a current valid
    DataEntity trip schema_mismatch. The seed creates exactly that
    shape with the 'LegacyOrders' entity flagged inferred (so it
    wouldn't be treated as live by the detector)."""
    from app.seed_demo import _build_graph

    nodes, edges = _build_graph(PID)
    data_ids = {n.id for n in nodes if n.kind == "DataEntity"}
    write_targets = {e.target_id for e in edges if e.kind in ("READS", "WRITES")}
    # The "LegacyOrders" target exists but is marked inferred —
    # the detector treats inferred as not currently valid.
    legacy = "data:orders.dbo.LegacyOrders"
    assert legacy in data_ids
    assert legacy in write_targets
    legacy_node = next(n for n in nodes if n.id == legacy)
    assert legacy_node.certainty == "inferred"


def test_opaque_component_failing_seed_is_present():
    """The detector aggregates CALLS-edge runtime_errors/calls per
    opaque Component. Confirm a Component with opacity=binary exists
    AND an inbound CALLS carries the numeric runtime telemetry."""
    from app.seed_demo import _build_graph

    nodes, edges = _build_graph(PID)
    opaque = [
        n for n in nodes
        if n.kind == "Component"
        and (n.data or {}).get("opacity") in ("opaque", "binary")
    ]
    assert opaque, "demo graph must include at least one opaque Component"
    opaque_ids = {n.id for n in opaque}
    calls_into_opaque = [
        e for e in edges
        if e.kind == "CALLS" and e.target_id in opaque_ids
    ]
    assert calls_into_opaque
    # And carries the runtime telemetry the detector aggregates.
    any_with_errors = any(
        int((e.data or {}).get("runtime_errors", 0)) > 0
        for e in calls_into_opaque
    )
    assert any_with_errors


def test_findings_cover_all_priority_buckets():
    """Triage card needs a P1; trend graphs need a mix of P2/P3/P4.
    The seed must light up every visual layer."""
    from app.merge.risk import priority_label
    from app.seed_demo import _build_findings

    findings = _build_findings(PID)
    priorities = {priority_label(f.risk_score) for f in findings}
    # P1 (≥75) lights the Triage card; P2/P3/P4 fill the priority
    # distribution chart on the findings tab.
    assert "P1" in priorities, "demo must include at least one P1"
    assert len(priorities) >= 3, (
        "demo must span at least 3 priority buckets, "
        f"got: {sorted(priorities)}"
    )


def test_findings_subject_nodes_reference_real_graph_nodes():
    """A finding whose subject_node_id points at a node not in
    the demo graph breaks the dashboard's drill-down. Validate
    every subject reference resolves."""
    from app.seed_demo import _build_findings, _build_graph

    nodes, _ = _build_graph(PID)
    node_ids = {n.id for n in nodes}
    bad = []
    for f in _build_findings(PID):
        if f.subject_node_id and f.subject_node_id not in node_ids:
            bad.append((f.kind, f.subject_node_id))
    assert not bad, f"findings reference missing nodes: {bad}"


def test_findings_include_lifecycle_examples():
    """The status badge palette on /findings shows open /
    acknowledged / resolved differently. A demo with only open
    findings makes the acknowledged/resolved badges untested."""
    from app.seed_demo import _build_findings

    statuses = {f.status for f in _build_findings(PID)}
    assert {"open", "acknowledged", "resolved"} <= statuses


def test_runs_cover_three_status_values():
    """The analysis tab renders different state machines for
    completed / failed / queued. Seed one of each without planting an
    ownerless running writer that would permanently block graph reads."""
    from app.seed_demo import _build_runs

    runs = _build_runs(PID)
    statuses = {r.status for r in runs}
    assert {"completed", "failed", "queued"} <= statuses
    completed = next(run for run in runs if run.status == "completed")
    publication = completed.stats["graph_publication"]
    assert completed.graph_base_generation == 0
    assert completed.graph_authoritative_sources == ["seed-demo"]
    assert publication["atomic"] is True
    assert publication["generation"] == 1


def test_summaries_target_real_graph_nodes():
    from app.seed_demo import _build_graph, _build_summaries

    nodes, _ = _build_graph(PID)
    node_ids = {n.id for n in nodes}
    for s in _build_summaries(PID):
        assert s.target_id in node_ids


def test_all_seed_rows_carry_seed_demo_marker():
    """Every demo row must be tagged so a re-run can wipe them.
    Untagged rows are orphans that ``--keep`` followed by a wipe
    can't clean up."""
    from app.seed_demo import _build_graph

    nodes, edges = _build_graph(PID)
    assert all("seed-demo" in n.created_by for n in nodes)
    assert all("seed-demo" in e.created_by for e in edges)


# ─── seed_demo orchestrator (mocked-session integration) ─────────


@pytest.mark.asyncio
async def test_seed_demo_refuses_on_production_without_force():
    """The single most important safety: seeding fake findings on
    top of a real production graph would mislead a real triage."""
    from app import seed_demo as mod

    with patch("app.config.get_settings", return_value=SimpleNamespace(
        mnemos_env="production"
    )):
        with pytest.raises(RuntimeError) as ei:
            await mod.seed_demo(force=False)
        assert "production" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_seed_demo_proceeds_on_production_with_force():
    """Operator can override the production guard when they really
    mean it. Verifies the gate is a flag, not a hard refusal."""
    from app import seed_demo as mod

    fake_session = _build_fake_session()
    with patch("app.config.get_settings", return_value=SimpleNamespace(
        mnemos_env="production"
    )), patch.object(mod, "SessionLocal", return_value=fake_session), \
            patch.object(mod, "hash_password", return_value="$fake$hash$"):
        summary = await mod.seed_demo(force=True)
    assert "project_id" in summary
    assert fake_session._commit_called


@pytest.mark.asyncio
async def test_seed_demo_commits_and_returns_counts():
    from app import seed_demo as mod

    fake_session = _build_fake_session()
    with patch("app.config.get_settings", return_value=SimpleNamespace(
        mnemos_env="test"
    )), patch.object(mod, "SessionLocal", return_value=fake_session), \
            patch.object(mod, "hash_password", return_value="$fake$hash$"):
        summary = await mod.seed_demo()

    assert summary["project_name"] == "demo-orders-service"
    assert summary["nodes"] >= 15
    assert summary["edges"] >= 10
    assert summary["findings"] >= 5
    assert summary["runs"] == 3
    assert fake_session._commit_called
    # A fresh seed adds the org + user + project + nodes + edges +
    # findings + runs + summaries + 1 audit log row.
    add_count = len(fake_session._added)
    assert add_count >= 30, f"expected ≥30 add() calls, got {add_count}"
    heads = [row for row in fake_session._added if row.__class__.__name__ == "GraphHead"]
    assert len(heads) == 1
    assert heads[0].state == "ready"
    assert heads[0].generation == 1
    completed = next(
        row
        for row in fake_session._added
        if row.__class__.__name__ == "AnalysisRun" and row.status == "completed"
    )
    assert heads[0].current_run_id == completed.id


@pytest.mark.asyncio
async def test_seed_demo_keeps_existing_demo_user_password():
    """Re-running seed-demo must NOT rotate the demo user's password
    — an operator who saved it once and re-runs seed expects to
    still log in with that credential."""
    from app import seed_demo as mod
    from app.models.auth import User

    existing_user = User(
        username=mod.DEMO_USER_NAME,
        password_hash="$existing$hash$",
        role="admin",
    )
    existing_user.id = 42
    fake_session = _build_fake_session(existing_user=existing_user)
    with patch("app.config.get_settings", return_value=SimpleNamespace(
        mnemos_env="test"
    )), patch.object(mod, "SessionLocal", return_value=fake_session):
        summary = await mod.seed_demo()

    # No fresh password printed when reusing an existing user.
    assert "password" not in summary
    assert "password_note" in summary


@pytest.mark.asyncio
async def test_ready_demo_head_can_be_wiped_and_reseeded_with_foreign_keys(
    monkeypatch,
):
    """The published-run RESTRICT FK must not break seed-demo idempotency."""
    from sqlalchemy import func, select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app import seed_demo as mod
    from app.models import (  # noqa: F401
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
    from app.models.base import Base
    from app.models.graph import AnalysisRun, GraphHead
    from app.models.projects import Project
    from app.graph_publication import promote_staged_graph
    from app.source_snapshot import find_unpublished_refresh
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(mod, "SessionLocal", session_factory)
    monkeypatch.setattr(mod, "hash_password", lambda _raw: "$fake$hash$")
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(mnemos_env="test"),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

        first = await mod.seed_demo(force=True)
        second = await mod.seed_demo(force=True)
        first_id = uuid.UUID(first["project_id"])
        second_id = uuid.UUID(second["project_id"])
        assert second_id != first_id

        async with session_factory() as session:
            assert int(
                (await session.execute(text("PRAGMA foreign_keys"))).scalar_one()
            ) == 1
            assert await session.get(GraphHead, first_id) is None
            assert int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(AnalysisRun)
                        .where(AnalysisRun.project_id == first_id)
                    )
                ).scalar_one()
            ) == 0
            assert int(
                (
                    await session.execute(
                        select(func.count())
                        .select_from(Project)
                        .where(Project.name == mod.DEMO_PROJECT_NAME)
                    )
                ).scalar_one()
            ) == 1
            head = await session.get(GraphHead, second_id)
            assert head is not None
            assert head.state == "ready"
            assert head.generation == 1
            assert head.current_run_id is not None
            assert (
                await find_unpublished_refresh(session, project_id=second_id)
                is None
            )
            published_run_id = head.current_run_id
            await session.rollback()
            retry = await promote_staged_graph(
                session,
                project_id=second_id,
                run_id=published_run_id,
            )
            assert retry.idempotent is True
            assert retry.generation == 1
    finally:
        await engine.dispose()


def _build_fake_session(*, existing_user=None):
    """Async-session-shaped mock that records add() and commit()."""
    session = MagicMock()
    session._added = []
    session._commit_called = False
    session._existing_user = existing_user

    async def aexit(*a):
        return False

    async def aenter():
        return session

    # Make ``async with SessionLocal() as s`` work — SessionLocal()
    # returns the session, which is an async context manager.
    session.__aenter__ = AsyncMock(side_effect=aenter)
    session.__aexit__ = AsyncMock(side_effect=aexit)

    # add() is sync.
    def _add(obj):
        session._added.append(obj)
        # Stamp a fake PK on Project so the downstream code that
        # references project.id keeps working.
        if obj.__class__.__name__ == "Project" and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if obj.__class__.__name__ == "Organization" and getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if obj.__class__.__name__ == "User" and getattr(obj, "id", None) is None:
            obj.id = 99
        if (
            obj.__class__.__name__ == "AnalysisRun"
            and getattr(obj, "id", None) is None
        ):
            obj.id = uuid.uuid4()
    session.add = _add

    async def _commit():
        session._commit_called = True
    session.commit = _commit

    async def _flush():
        return None
    session.flush = _flush

    # execute() returns a result object whose ``scalar_one_or_none``
    # / ``scalars().all()`` shape depends on the query. We branch on
    # the SQL string: lookups for Organization return None on the
    # first call (so org gets created), lookups for User return the
    # existing_user or None, lookups for Project ids during wipe
    # return empty.
    state = {"org_lookups": 0}

    async def _execute(stmt, *args, **kwargs):
        text = str(stmt).lower()
        result = MagicMock()
        if "from organizations" in text:
            state["org_lookups"] += 1
            result.scalar_one_or_none = MagicMock(return_value=None)
        elif "from users" in text:
            result.scalar_one_or_none = MagicMock(return_value=session._existing_user)
        else:
            # Default for the Project-id wipe lookup: empty scalars.
            scalars = MagicMock()
            scalars.all = MagicMock(return_value=[])
            result.scalars = MagicMock(return_value=scalars)
        return result
    session.execute = _execute

    # The CLI/seed call site uses ``async with SessionLocal() as s``;
    # SessionLocal() must return the session itself (which is the
    # async context manager).
    return session
