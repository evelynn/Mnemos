"""MCP mutation authorization remains current across long Gate-B work."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.mcp import dev_tools  # noqa: E402
from app.mcp.auth import (  # noqa: E402
    MCP_AUTHENTICATION_INVALID_CODE,
    MCPAuthContext,
    resolve_mcp_auth_context,
)
from app.models.all_models import ApiKey, Organization, Project, User  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead  # noqa: E402
from app.models.plans import DiffSubmission, Plan  # noqa: E402
from app.safety.tokens import hash_token  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402

_GIT_SHA = "a" * 40
_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed(factory) -> tuple[uuid.UUID, uuid.UUID, MCPAuthContext]:
    organization_id = uuid.uuid4()
    project_id = uuid.uuid4()
    user_id = uuid.uuid4()
    key_id = uuid.uuid4()
    run_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    raw_token = "gate-b-auth-revalidation-token"
    now = datetime.now(timezone.utc)

    async with factory() as session:
        session.add(
            Organization(
                id=organization_id,
                slug=f"mcp-gate-{uuid.uuid4().hex}",
                display_name="MCP Gate-B",
            )
        )
        await session.flush()
        session.add_all(
            [
                Project(
                    id=project_id,
                    name="MCP Gate-B project",
                    gitlab_project_id=8301,
                    gitlab_url="https://example.invalid/mcp-gate",
                    default_branch="main",
                    languages=["python"],
                    organization_id=organization_id,
                ),
                User(
                    id=user_id,
                    username=f"mcp-gate-operator-{uuid.uuid4().hex}",
                    password_hash="unused",
                    role="operator",
                    organization_id=organization_id,
                ),
            ]
        )
        await session.flush()
        run = AnalysisRun(
            id=run_id,
            project_id=project_id,
            status="completed",
            triggered_by="test",
            git_sha=_GIT_SHA,
            scope="full",
            started_at=now,
            completed_at=now,
            **published_run_fields(
                generation=1,
                published_at=now,
                coverage_sealed_at=now,
                authoritative_sources=["ggoss-py"],
            ),
        )
        session.add(run)
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=now,
            )
        )
        session.add(
            ApiKey(
                id=key_id,
                user_id=user_id,
                project_id=project_id,
                token_hash=hash_token(raw_token),
                label="Gate-B auth",
            )
        )
        session.add(
            Plan(
                id=plan_id,
                project_id=project_id,
                status="approved",
                spec={"title": "Gate-B auth"},
                tasks=[],
                requester="mcp:test",
                worktree_path="test-worktree",
                source_run_id=run_id,
                source_git_sha=_GIT_SHA,
                source_graph_generation=1,
                source_overlay_generation=0,
            )
        )
        await session.commit()
        context = await resolve_mcp_auth_context(
            session,
            token_hash=hash_token(raw_token),
            expected_project_id=project_id,
        )

    return project_id, plan_id, context


async def _no_worktree_check(*_args, **_kwargs) -> None:
    return None


async def _stable_diff(*_args, **_kwargs) -> str:
    return _DIFF


def _review_result():
    return SimpleNamespace(findings=[], verdict="pass")


async def _mutate_auth(
    session: AsyncSession,
    context: MCPAuthContext,
    mutation: str,
) -> None:
    if mutation == "revoke":
        statement = (
            update(ApiKey)
            .where(ApiKey.id == context.api_key_id)
            .values(revoked_at=datetime.now(timezone.utc))
        )
    elif mutation == "disable":
        statement = (
            update(User)
            .where(User.id == context.user_id)
            .values(disabled_at=datetime.now(timezone.utc))
        )
    else:
        statement = (
            update(User)
            .where(User.id == context.user_id)
            .values(role="viewer")
        )
    await session.execute(statement.execution_options(synchronize_session=False))
    await session.commit()


@pytest.mark.parametrize("mutation", ["revoke", "disable", "demote"])
async def test_gate_b_auth_change_after_review_rollback_blocks_persistence(
    database,
    monkeypatch,
    mutation,
):
    project_id, plan_id, context = await _seed(database)

    async def review(session, **_kwargs):
        # Deterministic interleaving at the exact vulnerable point: the initial
        # auth/Plan transaction has rolled back and Gate-B is still running.
        await _mutate_auth(session, context, mutation)
        return _review_result()

    monkeypatch.setattr(dev_tools, "_verify_plan_worktree", _no_worktree_check)
    monkeypatch.setattr(dev_tools, "compute_diff", _stable_diff)
    monkeypatch.setattr(dev_tools, "run_pipeline", review)

    async with database() as session:
        result = await dev_tools.submit_diff(
            session,
            auth_context=context,
            expected_project_id=project_id,
            plan_id=plan_id,
            task_id="task-1",
            diff=_DIFF,
        )
        submission_count = await session.scalar(
            select(func.count()).select_from(DiffSubmission)
        )
        plan = await session.get(Plan, plan_id)

    assert result == {
        "schema": "mnemos.error.v1",
        "error": MCP_AUTHENTICATION_INVALID_CODE,
        "retryable": False,
    }
    assert submission_count == 0
    assert plan is not None and plan.status == "approved"


async def test_gate_b_same_auth_context_persists_one_bound_submission(
    database,
    monkeypatch,
):
    project_id, plan_id, context = await _seed(database)

    async def review(_session, **_kwargs):
        return _review_result()

    monkeypatch.setattr(dev_tools, "_verify_plan_worktree", _no_worktree_check)
    monkeypatch.setattr(dev_tools, "compute_diff", _stable_diff)
    monkeypatch.setattr(dev_tools, "run_pipeline", review)

    async with database() as session:
        result = await dev_tools.submit_diff(
            session,
            auth_context=context,
            expected_project_id=project_id,
            plan_id=plan_id,
            task_id="task-1",
            diff=_DIFF,
        )
        submissions = (await session.execute(select(DiffSubmission))).scalars().all()

    assert result["status"] == "pending_approval"
    assert len(submissions) == 1
    assert submissions[0].plan_id == plan_id
    assert submissions[0].review_run_id is not None
    assert submissions[0].review_source_generation == 1
    assert submissions[0].review_overlay_generation == 0


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
    schema = f"mnemos_mcp_auth_{uuid.uuid4().hex}"
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


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["revoke", "disable", "demote"])
async def test_postgres_auth_change_committed_during_gate_b_blocks_submission(
    postgres_database,
    monkeypatch,
    mutation,
):
    factory = postgres_database
    project_id, plan_id, context = await _seed(factory)
    review_started = asyncio.Event()
    auth_changed = asyncio.Event()

    async def review(_session, **_kwargs):
        review_started.set()
        await auth_changed.wait()
        return _review_result()

    monkeypatch.setattr(dev_tools, "_verify_plan_worktree", _no_worktree_check)
    monkeypatch.setattr(dev_tools, "compute_diff", _stable_diff)
    monkeypatch.setattr(dev_tools, "run_pipeline", review)

    async def submit():
        async with factory() as session:
            return await dev_tools.submit_diff(
                session,
                auth_context=context,
                expected_project_id=project_id,
                plan_id=plan_id,
                task_id="task-1",
                diff=_DIFF,
            )

    task = asyncio.create_task(submit())
    await asyncio.wait_for(review_started.wait(), timeout=10)
    try:
        async with factory() as session:
            await _mutate_auth(session, context, mutation)
    finally:
        auth_changed.set()
    result = await asyncio.wait_for(task, timeout=15)

    async with factory() as session:
        submission_count = await session.scalar(
            select(func.count()).select_from(DiffSubmission)
        )

    assert result["error"] == MCP_AUTHENTICATION_INVALID_CODE
    assert submission_count == 0
