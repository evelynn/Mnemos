"""Fail-closed Finding mutations at the canonical graph revision boundary."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api import findings as findings_api  # noqa: E402
from app.api import plans as plans_api  # noqa: E402
from app.finding_currentness import (  # noqa: E402
    FINDING_NOT_CURRENT_CODE,
    lock_current_finding_for_action,
)
from app.finding_identity import finding_identity_key  # noqa: E402
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.auth import User  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.findings import Finding  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.plans import Plan  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402


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


async def _seed_action_target(
    factory: async_sessionmaker[AsyncSession],
    *,
    finding_generation: int | None = 1,
    finding_overlay_generation: int | None = 0,
) -> tuple[uuid.UUID, uuid.UUID, User]:
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    finding_id = uuid.uuid4()
    published_at = datetime.now(tz=timezone.utc)
    async with factory() as session:
        session.add(
            Organization(
                id=organization_id,
                slug=f"finding-action-{organization_id.hex}",
                display_name="Finding action tests",
            )
        )
        await session.flush()
        user = User(
            id=user_id,
            username=f"operator-{user_id.hex}",
            password_hash="test",
            role="operator",
            organization_id=organization_id,
        )
        session.add(user)
        session.add(
            Project(
                id=project_id,
                name=f"finding-action-{project_id.hex}",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/finding-action",
                default_branch="main",
                languages=["python"],
                organization_id=organization_id,
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:finding-action",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at,
                completed_at=published_at,
                **published_run_fields(
                    generation=1,
                    published_at=published_at,
                ),
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=published_at,
            )
        )
        session.add(
            Finding(
                id=finding_id,
                project_id=project_id,
                kind="schema_mismatch",
                identity_key=finding_identity_key(
                    kind="schema_mismatch",
                    subject_node_id="py:service.py::read_orders",
                ),
                severity="warning",
                status="open",
                subject_node_id="py:service.py::read_orders",
                detail={"reason": "test"},
                risk_score=60,
                remediation="Update the source contract.",
                validated_graph_generation=finding_generation,
                validated_overlay_generation=finding_overlay_generation,
                validated_at=(
                    published_at
                    if finding_generation is not None
                    and finding_overlay_generation is not None
                    else None
                ),
            )
        )
        await session.commit()
        # The API only reads identity and tenant fields from the principal.
        return project_id, finding_id, user


@pytest.mark.asyncio
async def test_patch_finding_accepts_only_exact_current_revision(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, finding_id, user = await _seed_action_target(database)

    async def _no_audit(**_kwargs):
        return None

    monkeypatch.setattr(findings_api, "audit_record", _no_audit)
    async with database() as session:
        result = await findings_api.patch_finding(
            finding_id=finding_id,
            body=findings_api.FindingPatch(status="resolved"),
            user=user,
            db=session,
        )

    assert result.status == "resolved"
    async with database() as session:
        stored = await session.get(Finding, finding_id)
        assert stored is not None
        assert stored.status == "resolved"
        assert stored.resolved_by == f"user:{user.id}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("graph_generation", "overlay_generation"),
    [(None, None), (2, 0), (1, 1)],
)
async def test_patch_finding_rejects_null_or_stale_revision_without_mutation(
    database,
    graph_generation: int | None,
    overlay_generation: int | None,
):
    _, finding_id, user = await _seed_action_target(
        database,
        finding_generation=graph_generation,
        finding_overlay_generation=overlay_generation,
    )
    async with database() as session:
        with pytest.raises(HTTPException) as caught:
            await findings_api.patch_finding(
                finding_id=finding_id,
                body=findings_api.FindingPatch(status="false_positive"),
                user=user,
                db=session,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == FINDING_NOT_CURRENT_CODE

    async with database() as session:
        stored = await session.get(Finding, finding_id)
        assert stored is not None
        assert stored.status == "open"
        assert stored.resolved_at is None
        assert stored.resolved_by is None


@pytest.mark.asyncio
async def test_plan_from_stale_finding_has_no_side_effects(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, finding_id, user = await _seed_action_target(
        database,
        finding_generation=2,
    )

    async def _must_not_run(*_args, **_kwargs):
        raise AssertionError("stale finding reached a downstream side effect")

    monkeypatch.setattr(plans_api, "impact_analysis", _must_not_run)
    monkeypatch.setattr(plans_api, "create_worktree", _must_not_run)
    async with database() as session:
        with pytest.raises(HTTPException) as caught:
            await plans_api.plan_from_finding(
                finding_id=finding_id,
                user=user,
                db=session,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == FINDING_NOT_CURRENT_CODE

    async with database() as session:
        plan_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Plan)
                )
            ).scalar_one()
        )
        assert plan_count == 0


@pytest.mark.asyncio
async def test_ready_head_with_invalid_receipt_is_same_stable_conflict(
    database,
):
    project_id, finding_id, user = await _seed_action_target(database)
    async with database() as session:
        head = await session.get(GraphHead, project_id)
        assert head is not None
        run = await session.get(AnalysisRun, head.current_run_id)
        assert run is not None and isinstance(run.stats, dict)
        stats = dict(run.stats)
        receipt = dict(stats["graph_publication"])
        receipt["atomic"] = False
        stats["graph_publication"] = receipt
        run.stats = stats
        await session.commit()

    async with database() as session:
        with pytest.raises(HTTPException) as caught:
            await findings_api.patch_finding(
                finding_id=finding_id,
                body=findings_api.FindingPatch(status="resolved"),
                user=user,
                db=session,
            )
        assert caught.value.status_code == 409
        assert caught.value.detail == FINDING_NOT_CURRENT_CODE

    async with database() as session:
        stored = await session.get(Finding, finding_id)
        assert stored is not None and stored.status == "open"


@pytest.mark.asyncio
async def test_cross_org_finding_action_is_indistinguishable_from_missing(
    database,
):
    _, finding_id, owner = await _seed_action_target(database)
    foreign_user = User(
        id=uuid.uuid4(),
        username="foreign-operator",
        password_hash="test",
        role="operator",
        organization_id=uuid.uuid4(),
    )

    async with database() as session:
        with pytest.raises(HTTPException) as foreign_error:
            await findings_api.patch_finding(
                finding_id=finding_id,
                body=findings_api.FindingPatch(status="resolved"),
                user=foreign_user,
                db=session,
            )
        with pytest.raises(HTTPException) as missing_error:
            await findings_api.patch_finding(
                finding_id=uuid.uuid4(),
                body=findings_api.FindingPatch(status="resolved"),
                user=owner,
                db=session,
            )

    assert (foreign_error.value.status_code, foreign_error.value.detail) == (
        missing_error.value.status_code,
        missing_error.value.detail,
    ) == (404, "not_found")


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
    """Create an isolated schema when CI exposes its PostgreSQL service."""

    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    root_engine = create_async_engine(url)
    schema = f"mnemos_finding_action_{uuid.uuid4().hex}"
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
async def test_postgres_head_advance_cannot_cross_finding_action_commit(
    postgres_database,
):
    """The action holds GraphHead until its finding mutation is committed."""

    factory = postgres_database
    project_id, finding_id, _ = await _seed_action_target(factory)
    action_locked = asyncio.Event()
    release_action = asyncio.Event()
    head_advanced = asyncio.Event()

    async def _action() -> None:
        async with factory() as session:
            finding = await lock_current_finding_for_action(
                session,
                project_id=project_id,
                finding_id=finding_id,
            )
            finding.status = "acknowledged"
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
    assert not head_advanced.is_set(), "head update crossed the action lock"

    release_action.set()
    await asyncio.wait_for(asyncio.gather(action_task, advance_task), timeout=10)

    async with factory() as session:
        finding = await session.get(Finding, finding_id)
        head = await session.get(GraphHead, project_id)
        assert finding is not None and finding.status == "acknowledged"
        assert head is not None and head.overlay_generation == 1
