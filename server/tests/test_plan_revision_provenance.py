"""Plan worktrees and decisions are pinned to one source publication."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api import plans as plans_api  # noqa: E402
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.auth import User  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.plans import Plan  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.plan_provenance import (  # noqa: E402
    PlanBaseRevisionMismatch,
    PlanSourceRevisionStale,
    PlanSourceRevisionUnavailable,
    bind_plan_source_revision,
    lock_current_plan_for_action,
    lock_current_plan_source_revision,
)
from app.testing.graph_publication import published_run_fields  # noqa: E402


_PUBLISHED_AT = datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
_GIT_SHA = "a" * 40


@pytest_asyncio.fixture
async def session() -> AsyncSession:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as db:
        yield db
    await engine.dispose()


async def _seed_publication(
    session: AsyncSession,
    *,
    overlay_generation: int = 4,
) -> tuple[Project, AnalysisRun, GraphHead]:
    project = Project(
        id=uuid.uuid4(),
        name="plan-revision",
        gitlab_project_id=71,
        gitlab_url="https://example.invalid/plan-revision",
        default_branch="main",
        languages=["python"],
    )
    run = AnalysisRun(
        id=uuid.uuid4(),
        project_id=project.id,
        status="completed",
        triggered_by="test",
        git_sha=_GIT_SHA,
        scope="full",
        started_at=_PUBLISHED_AT - timedelta(seconds=2),
        completed_at=_PUBLISHED_AT,
        **published_run_fields(
            generation=7,
            published_at=_PUBLISHED_AT,
            coverage_sealed_at=_PUBLISHED_AT - timedelta(seconds=1),
            authoritative_sources=["ggoss-py"],
        ),
    )
    head = GraphHead(
        project_id=project.id,
        current_run_id=run.id,
        generation=7,
        overlay_generation=overlay_generation,
        state="ready",
        published_at=_PUBLISHED_AT,
    )
    session.add_all([project, run])
    await session.flush()
    session.add(head)
    await session.commit()
    return project, run, head


async def _seed_exact_project_user_org(
    session: AsyncSession,
    project: Project,
) -> User:
    """Give decision tests the production exact non-NULL org boundary."""

    organization = Organization(
        id=uuid.uuid4(),
        slug=f"plan-{uuid.uuid4().hex}",
        display_name="Plan revision test org",
    )
    session.add(organization)
    await session.flush()
    project.organization_id = organization.id
    user = User(
        id=uuid.uuid4(),
        username=f"plan-operator-{uuid.uuid4().hex}",
        password_hash="unused",
        role="operator",
        organization_id=organization.id,
    )
    session.add(user)
    await session.commit()
    return user


@pytest.mark.asyncio
async def test_current_revision_uses_receipt_and_rejects_arbitrary_base(
    session: AsyncSession,
) -> None:
    project, run, _head = await _seed_publication(session)
    project_id = project.id
    run_id = run.id

    revision = await lock_current_plan_source_revision(
        session,
        project_id=project_id,
    )

    assert revision.run_id == run_id
    assert revision.git_sha == _GIT_SHA
    assert revision.source_generation == 7
    assert revision.overlay_generation == 4
    await session.rollback()

    with pytest.raises(PlanBaseRevisionMismatch):
        await lock_current_plan_source_revision(
            session,
            project_id=project_id,
            requested_base_sha="b" * 40,
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_noncanonical_publication_sha_cannot_authorise_plan(
    session: AsyncSession,
) -> None:
    project, run, _head = await _seed_publication(session)
    run.git_sha = "HEAD"
    await session.commit()

    with pytest.raises(
        PlanSourceRevisionUnavailable,
        match="canonical full Git commit",
    ):
        await lock_current_plan_source_revision(session, project_id=project.id)
    await session.rollback()


@pytest.mark.asyncio
async def test_database_rejects_partially_bound_plan_revision(
    session: AsyncSession,
) -> None:
    project, run, _head = await _seed_publication(session)
    session.add(
        Plan(
            id=uuid.uuid4(),
            project_id=project.id,
            status="pending_approval",
            spec={"title": "partial provenance"},
            tasks=[],
            requester="test",
            source_run_id=run.id,
        )
    )

    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_legacy_and_overlay_stale_plans_fail_closed(
    session: AsyncSession,
) -> None:
    project, _run, _head = await _seed_publication(session)
    project_id = project.id
    legacy_id = uuid.uuid4()
    current_id = uuid.uuid4()
    legacy = Plan(
        id=legacy_id,
        project_id=project_id,
        status="pending_approval",
        spec={"title": "legacy"},
        tasks=[],
        requester="test",
    )
    current = Plan(
        id=current_id,
        project_id=project_id,
        status="pending_approval",
        spec={"title": "current"},
        tasks=[],
        requester="test",
    )
    revision = await lock_current_plan_source_revision(
        session,
        project_id=project_id,
    )
    bind_plan_source_revision(current, revision)
    session.add_all([legacy, current])
    await session.commit()

    with pytest.raises(PlanSourceRevisionStale):
        await lock_current_plan_for_action(
            session,
            plan_id=legacy_id,
            project_id=project_id,
        )
    await session.rollback()

    locked, _ = await lock_current_plan_for_action(
        session,
        plan_id=current_id,
        project_id=project_id,
    )
    assert locked.id == current.id
    await session.rollback()

    head = await session.get(GraphHead, project_id)
    assert head is not None
    head.overlay_generation += 1
    await session.commit()
    with pytest.raises(PlanSourceRevisionStale):
        await lock_current_plan_for_action(
            session,
            plan_id=current_id,
            project_id=project_id,
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_plan_lock_refreshes_a_preloaded_identity_map_row(
    session: AsyncSession,
) -> None:
    project, _run, _head = await _seed_publication(session)
    revision = await lock_current_plan_source_revision(
        session,
        project_id=project.id,
    )
    plan = Plan(
        id=uuid.uuid4(),
        project_id=project.id,
        status="pending_approval",
        spec={"title": "identity map"},
        tasks=[],
        requester="test",
    )
    bind_plan_source_revision(plan, revision)
    session.add(plan)
    await session.commit()

    # Simulate a value changed by another transaction while this Session has
    # a stale ORM object. synchronize_session=False deliberately preserves
    # the cached value so populate_existing on the lock is what closes it.
    await session.execute(
        update(Plan)
        .where(Plan.id == plan.id)
        .values(source_overlay_generation=revision.overlay_generation - 1)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    assert plan.source_overlay_generation == revision.overlay_generation

    with pytest.raises(PlanSourceRevisionStale):
        await lock_current_plan_for_action(
            session,
            plan_id=plan.id,
            project_id=project.id,
        )
    await session.rollback()


@pytest.mark.asyncio
async def test_submit_persists_exact_revision_and_mismatch_has_no_side_effect(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, run, _head = await _seed_publication(session)
    project_id = project.id
    worktree_calls: list[str | None] = []

    async def _impact(*args, **kwargs):  # noqa: ANN002,ANN003
        return {
            "directly_affected": [],
            "transitively_affected": [],
            "opaque_components_touched": [],
        }

    async def _worktree(plan_id, project_id, base_sha=None):  # noqa: ANN001
        worktree_calls.append(base_sha)
        path = tmp_path / str(plan_id)
        path.mkdir()
        return path

    async def _destroy(*args, **kwargs):  # noqa: ANN002,ANN003
        return None

    monkeypatch.setattr(plans_api, "impact_analysis", _impact)
    monkeypatch.setattr(plans_api, "create_worktree", _worktree)
    monkeypatch.setattr(plans_api, "destroy_worktree", _destroy)
    user = await _seed_exact_project_user_org(session, project)
    body = plans_api.PlanSubmit(
        spec=plans_api.PlanSpec(
            title="pin it",
            motivation="reviewed graph evidence",
        ),
        tasks=[],
        target_component_id="sym:target",
        requester="agent:test",
    )

    out = await plans_api.submit_plan(project_id, body, user, session)

    assert worktree_calls == [_GIT_SHA]
    assert out.source_run_id == run.id
    assert out.source_git_sha == _GIT_SHA
    assert out.source_graph_generation == 7
    assert out.source_overlay_generation == 4
    stored = (
        await session.execute(select(Plan).where(Plan.id == out.id))
    ).scalar_one()
    assert stored.worktree_meta["base_sha"] == _GIT_SHA

    bad = body.model_copy(update={"base_sha": "b" * 40})
    with pytest.raises(HTTPException) as caught:
        await plans_api.submit_plan(project_id, bad, user, session)
    assert caught.value.status_code == 409
    assert caught.value.detail == "plan_base_revision_mismatch"
    assert worktree_calls == [_GIT_SHA]
    assert (
        await session.scalar(select(func.count()).select_from(Plan))
    ) == 1

    async def _unavailable(*args, **kwargs):  # noqa: ANN002,ANN003
        from app.sandbox.worktree import WorktreeRevisionUnavailable

        raise WorktreeRevisionUnavailable("published commit is not mirrored")

    monkeypatch.setattr(plans_api, "create_worktree", _unavailable)
    with pytest.raises(HTTPException) as unavailable:
        await plans_api.submit_plan(project_id, body, user, session)
    assert unavailable.value.status_code == 409
    assert unavailable.value.detail == "plan_worktree_revision_unavailable"
    assert (
        await session.scalar(select(func.count()).select_from(Plan))
    ) == 1


@pytest.mark.asyncio
async def test_decision_revalidates_provenance_before_status_mutation(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _run, head = await _seed_publication(session)
    revision = await lock_current_plan_source_revision(
        session,
        project_id=project.id,
    )
    plan = Plan(
        id=uuid.uuid4(),
        project_id=project.id,
        status="pending_approval",
        spec={"title": "approve exact"},
        tasks=[],
        requester="test",
        worktree_path=str(tmp_path / str(uuid.uuid4())),
    )
    canonical_path = Path(plan.worktree_path)

    async def _verify(plan_id, expected_sha):  # noqa: ANN001
        assert plan_id == plan.id
        assert expected_sha == _GIT_SHA
        return canonical_path

    monkeypatch.setattr(plans_api, "worktree_path", lambda _plan_id: canonical_path)
    monkeypatch.setattr(plans_api, "verify_worktree_revision", _verify)
    bind_plan_source_revision(plan, revision)
    session.add(plan)
    await session.commit()
    user = await _seed_exact_project_user_org(session, project)

    approved = await plans_api.decide_plan(
        plan.id,
        plans_api.PlanDecision(status="approve"),
        user,
        session,
    )
    assert approved.status == "approved"

    head.overlay_generation += 1
    await session.commit()
    with pytest.raises(HTTPException) as caught:
        await plans_api.decide_plan(
            plan.id,
            plans_api.PlanDecision(status="reject"),
            user,
            session,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "plan_source_revision_stale"
    await session.refresh(plan)
    assert plan.status == "approved"


@pytest.mark.asyncio
async def test_approval_rejects_a_worktree_head_mismatch(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project, _run, _head = await _seed_publication(session)
    revision = await lock_current_plan_source_revision(
        session,
        project_id=project.id,
    )
    plan = Plan(
        id=uuid.uuid4(),
        project_id=project.id,
        status="pending_approval",
        spec={"title": "tampered worktree"},
        tasks=[],
        requester="test",
    )
    bind_plan_source_revision(plan, revision)
    canonical_path = tmp_path / str(plan.id)
    plan.worktree_path = str(canonical_path)
    session.add(plan)
    await session.commit()

    async def _mismatch(*args, **kwargs):  # noqa: ANN002,ANN003
        from app.sandbox.worktree import WorktreeRevisionMismatch

        raise WorktreeRevisionMismatch("changed HEAD")

    monkeypatch.setattr(plans_api, "worktree_path", lambda _plan_id: canonical_path)
    monkeypatch.setattr(plans_api, "verify_worktree_revision", _mismatch)
    user = await _seed_exact_project_user_org(session, project)

    with pytest.raises(HTTPException) as caught:
        await plans_api.decide_plan(
            plan.id,
            plans_api.PlanDecision(status="approve"),
            user,
            session,
        )
    assert caught.value.status_code == 409
    assert caught.value.detail == "plan_worktree_revision_mismatch"
    await session.refresh(plan)
    assert plan.status == "pending_approval"


def test_mcp_mutations_use_internal_revision_lock_not_post_commit_retry() -> None:
    from app.mcp.server import _REVISION_LOCKED_DEV_MUTATIONS

    assert _REVISION_LOCKED_DEV_MUTATIONS == {
        "submit_plan",
        "edit_file_in_worktree",
        "run_in_sandbox",
        "submit_diff",
    }


def test_0032_migration_preserves_legacy_rows_but_guards_downgrade() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0032_plan_revision_provenance.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision = "0031_finding_graph_currentness"' in migration
    assert "source_run_id" in migration
    assert "source_git_sha" in migration
    assert "source_graph_generation" in migration
    assert "source_overlay_generation" in migration
    assert "revision-bound plans exist" in migration


def _postgres_url() -> str | None:
    raw = os.environ.get("MNEMOS_TEST_POSTGRES_URL") or os.environ.get(
        "DATABASE_URL",
        "",
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
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url)
    schema = f"mnemos_plan_revision_{uuid.uuid4().hex}"
    schema_engine: AsyncEngine | None = None
    try:
        async with root_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        schema_engine = root_engine.execution_options(
            schema_translate_map={None: schema}
        )
        async with schema_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(schema_engine, expire_on_commit=False)
    finally:
        if schema_engine is not None:
            await schema_engine.dispose()
        try:
            async with root_engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await root_engine.dispose()


@pytest.mark.asyncio
async def test_postgres_plan_action_holds_head_lock_through_commit(
    postgres_database,
) -> None:
    factory = postgres_database
    async with factory() as session:
        project, _run, _head = await _seed_publication(session)
        project_id = project.id
        revision = await lock_current_plan_source_revision(
            session,
            project_id=project_id,
        )
        plan = Plan(
            id=uuid.uuid4(),
            project_id=project_id,
            status="pending_approval",
            spec={"title": "fenced decision"},
            tasks=[],
            requester="test",
        )
        bind_plan_source_revision(plan, revision)
        session.add(plan)
        await session.commit()
        plan_id = plan.id

    action_locked = asyncio.Event()
    release_action = asyncio.Event()
    head_advanced = asyncio.Event()

    async def _action() -> None:
        async with factory() as session:
            plan, _revision = await lock_current_plan_for_action(
                session,
                plan_id=plan_id,
                project_id=project_id,
            )
            plan.status = "approved"
            action_locked.set()
            await release_action.wait()
            await session.commit()

    async def _advance_head() -> None:
        await action_locked.wait()
        async with factory() as session:
            head = (
                await session.execute(
                    select(GraphHead)
                    .where(GraphHead.project_id == project_id)
                    .with_for_update()
                )
            ).scalar_one()
            head.overlay_generation += 1
            await session.commit()
        head_advanced.set()

    action_task = asyncio.create_task(_action())
    await asyncio.wait_for(action_locked.wait(), timeout=10)
    advance_task = asyncio.create_task(_advance_head())
    await asyncio.sleep(0.1)
    assert not head_advanced.is_set(), "head update crossed the Plan action lock"

    release_action.set()
    await asyncio.wait_for(
        asyncio.gather(action_task, advance_task),
        timeout=10,
    )
