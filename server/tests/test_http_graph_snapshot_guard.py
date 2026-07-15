"""HTTP graph consumers pin and revalidate an atomic publication head."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.api import analysis, artifacts, ask, data, findings, flow
from app.api import graph_guard
from app.graph_publication import (
    GraphGenerationChanged,
    GraphHeadNeedsRebuild,
    GraphReadStamp,
)


@pytest.mark.asyncio
async def test_http_guard_returns_structured_full_repair_error(monkeypatch) -> None:
    async def _unavailable(_db, *, project_id):  # noqa: ANN001
        assert isinstance(project_id, uuid.UUID)
        raise GraphHeadNeedsRebuild("full staged rebuild required")

    monkeypatch.setattr(graph_guard, "read_graph_stamp", _unavailable)
    dependency = graph_guard.require_readable_current_graph(
        uuid.uuid4(), object()
    )
    with pytest.raises(HTTPException) as raised:
        await anext(dependency)

    assert raised.value.status_code == 409
    detail = raised.value.detail
    assert detail["schema"] == "mnemos.error.v1"
    assert detail["error"] == graph_guard.GRAPH_SNAPSHOT_UNAVAILABLE_CODE
    assert detail["repair"]["scope"] == "full"


@pytest.mark.asyncio
async def test_http_guard_allows_published_graph(monkeypatch) -> None:
    stamp = GraphReadStamp(
        project_id=uuid.uuid4(),
        generation=4,
        overlay_generation=2,
        current_run_id=uuid.uuid4(),
        published_at=datetime.now(tz=timezone.utc),
    )
    revalidated: list[GraphReadStamp] = []

    async def _read(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return stamp

    async def _revalidate(_db, *, stamp):  # noqa: ANN001
        revalidated.append(stamp)

    monkeypatch.setattr(graph_guard, "read_graph_stamp", _read)
    monkeypatch.setattr(graph_guard, "revalidate_graph_stamp", _revalidate)
    dependency = graph_guard.require_readable_current_graph(stamp.project_id, object())
    assert await anext(dependency) is None
    with pytest.raises(StopAsyncIteration):
        await anext(dependency)
    assert revalidated == [stamp]


@pytest.mark.asyncio
async def test_http_guard_discards_result_when_generation_changes(monkeypatch) -> None:
    stamp = GraphReadStamp(
        project_id=uuid.uuid4(),
        generation=8,
        overlay_generation=5,
        current_run_id=uuid.uuid4(),
        published_at=datetime.now(tz=timezone.utc),
    )

    async def _read(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return stamp

    async def _changed(_db, *, stamp):  # noqa: ANN001
        raise GraphGenerationChanged(f"generation {stamp.generation} changed")

    monkeypatch.setattr(graph_guard, "read_graph_stamp", _read)
    monkeypatch.setattr(graph_guard, "revalidate_graph_stamp", _changed)
    dependency = graph_guard.require_readable_current_graph(stamp.project_id, object())
    assert await anext(dependency) is None
    with pytest.raises(HTTPException) as raised:
        await anext(dependency)
    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == graph_guard.GRAPH_SNAPSHOT_CHANGED_CODE
    assert raised.value.detail["retryable"] is True
    assert raised.value.detail["snapshot"]["overlay_generation"] == 5


def _dependency_functions(route) -> set[object]:  # noqa: ANN001
    return {
        dependency.dependency
        for dependency in getattr(route, "dependencies", [])
    }


def _assert_guard_is_function_scoped(route) -> None:  # noqa: ANN001
    guards = [
        dependency
        for dependency in getattr(route, "dependencies", [])
        if dependency.dependency is graph_guard.require_readable_current_graph
    ]
    assert len(guards) == 1
    assert guards[0].scope == "function"


def test_all_project_scoped_ask_data_and_flow_routes_are_guarded() -> None:
    for router in (ask.router, data.router, flow.router):
        assert router.routes
        for route in router.routes:
            assert graph_guard.require_readable_current_graph in _dependency_functions(
                route
            )
            _assert_guard_is_function_scoped(route)


def test_analysis_current_graph_routes_are_guarded() -> None:
    guarded_paths = {
        "/api/v1/projects/{project_id}/graph/stats",
        "/api/v1/projects/{project_id}/graph/search",
        "/api/v1/projects/{project_id}/graph/component_map",
        "/api/v1/projects/{project_id}/graph/certainty_breakdown",
        "/api/v1/projects/{project_id}/summaries",
        "/api/v1/projects/{project_id}/llm_cost",
        "/api/v1/projects/{project_id}/llm_fallback_breakdown",
    }
    found: set[str] = set()
    for route in analysis.router.routes:
        if getattr(route, "path", None) not in guarded_paths:
            continue
        found.add(route.path)
        assert graph_guard.require_readable_current_graph in _dependency_functions(
            route
        )
        _assert_guard_is_function_scoped(route)
    assert found == guarded_paths


def test_finding_current_metrics_are_guarded() -> None:
    guarded_paths = {
        "/api/v1/projects/{project_id}/findings",
        "/api/v1/projects/{project_id}/findings/summary",
        "/api/v1/projects/{project_id}/findings/roi",
    }
    found: set[str] = set()
    for route in findings.router.routes:
        if getattr(route, "path", None) not in guarded_paths:
            continue
        found.add(route.path)
        assert graph_guard.require_readable_current_graph in _dependency_functions(
            route
        )
        _assert_guard_is_function_scoped(route)
    assert found == guarded_paths


def test_confirmation_mutation_uses_its_writer_lock_not_a_read_guard() -> None:
    route = next(
        route
        for route in analysis.router.routes
        if getattr(route, "path", None)
        == "/api/v1/projects/{project_id}/graph/confirm_fact"
    )
    dependencies = _dependency_functions(route)
    assert graph_guard.require_readable_current_graph not in dependencies


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirmation_http_success_commits_exactly_one_overlay_revision(
    http_client,
    db_session,
    admin_user,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful mutation must not self-reject during dependency teardown."""

    from sqlalchemy import func, select

    from app.models.graph import AnalysisRun, GraphHead, Node
    from app.models.organization import Organization
    from app.models.overlays import GraphNodeHumanOverlay
    from app.models.projects import Project
    from app.testing.graph_publication import published_run_fields

    now = datetime.now(tz=timezone.utc)
    org = Organization(
        slug=f"confirm-{uuid.uuid4().hex[:12]}",
        display_name="Confirmation test",
    )
    db_session.add(org)
    await db_session.flush()
    admin_user.organization_id = org.id
    project = Project(
        name="confirmation-http",
        gitlab_project_id=uuid.uuid4().int % 2_000_000_000,
        gitlab_url="https://example.invalid/confirmation-http",
        default_branch="main",
        languages=["python"],
        organization_id=org.id,
    )
    db_session.add(project)
    await db_session.flush()
    run = AnalysisRun(
        project_id=project.id,
        status="completed",
        triggered_by="test",
        git_sha="a" * 40,
        scope="full",
        started_at=now,
        completed_at=now,
        **published_run_fields(generation=1, published_at=now),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add_all(
        [
            GraphHead(
                project_id=project.id,
                current_run_id=run.id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=now,
            ),
            Node(
                id="py:confirmation.py::target",
                project_id=project.id,
                valid_from=now,
                kind="Symbol",
                data={"name": "target"},
                certainty="inferred",
                created_by=["ggoss-py"],
            ),
        ]
    )
    await db_session.commit()

    async def _audit_noop(**_kwargs) -> None:  # noqa: ANN003
        return None

    monkeypatch.setattr(analysis, "audit_record", _audit_noop)
    login = await http_client.post(
        "/api/v1/auth/login",
        json={"username": admin_user.username, "password": "pw-test-123456"},
    )
    assert login.status_code == 200

    project_id = project.id
    response = await http_client.post(
        f"/api/v1/projects/{project_id}/graph/confirm_fact",
        json={
            "target_kind": "node",
            "target_id": "py:confirmation.py::target",
            "action": "confirm",
            "rationale": "The operator inspected the source and confirmed it.",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["effective_certainty"] == "asserted"

    db_session.expire_all()
    head = await db_session.get(GraphHead, project_id)
    overlay_count = (
        await db_session.execute(
            select(func.count())
            .select_from(GraphNodeHumanOverlay)
            .where(GraphNodeHumanOverlay.project_id == project_id)
        )
    ).scalar_one()
    assert head is not None
    assert head.generation == 1
    assert head.overlay_generation == 1
    assert overlay_count == 1


def test_http_task_context_pack_is_guarded_but_diagnostic_index_is_not() -> None:
    dependencies_by_path = {
        route.path: _dependency_functions(route) for route in artifacts.router.routes
    }
    task_pack = "/api/v1/projects/{project_id}/artifacts/task-context-pack.json"
    project_index = "/api/v1/projects/{project_id}/artifacts/project-index.json"
    assert graph_guard.require_readable_current_graph in dependencies_by_path[task_pack]
    task_pack_route = next(
        route for route in artifacts.router.routes if route.path == task_pack
    )
    _assert_guard_is_function_scoped(task_pack_route)
    # The bounded project index intentionally remains available for repair
    # diagnosis and withholds graph rows itself when a refresh is unsafe.
    assert (
        graph_guard.require_readable_current_graph
        not in dependencies_by_path[project_index]
    )
