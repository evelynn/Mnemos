"""Plan-scoped diff listings must not cross organisation boundaries."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import diffs as diffs_api
from app.models import all_models as _all_models  # noqa: F401
from app.models.base import Base
from app.models.organization import Organization
from app.models.plans import DiffSubmission, Plan
from app.models.projects import Project
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


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


async def _seed_two_orgs(database):
    own_org_id = uuid.uuid4()
    foreign_org_id = uuid.uuid4()
    own_project_id = uuid.uuid4()
    foreign_project_id = uuid.uuid4()
    own_plan_id = uuid.uuid4()
    foreign_plan_id = uuid.uuid4()

    async with database() as session:
        session.add_all(
            [
                Organization(
                    id=own_org_id,
                    slug=f"own-{own_org_id.hex}",
                    display_name="Own organisation",
                ),
                Organization(
                    id=foreign_org_id,
                    slug=f"foreign-{foreign_org_id.hex}",
                    display_name="Foreign organisation",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Project(
                    id=own_project_id,
                    name="own-project",
                    gitlab_project_id=101,
                    gitlab_url="https://example.invalid/own",
                    default_branch="main",
                    languages=["python"],
                    organization_id=own_org_id,
                ),
                Project(
                    id=foreign_project_id,
                    name="foreign-project",
                    gitlab_project_id=202,
                    gitlab_url="https://example.invalid/foreign",
                    default_branch="main",
                    languages=["python"],
                    organization_id=foreign_org_id,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Plan(
                    id=own_plan_id,
                    project_id=own_project_id,
                    status="approved",
                    spec={"title": "own"},
                    tasks=[],
                    requester="test",
                ),
                Plan(
                    id=foreign_plan_id,
                    project_id=foreign_project_id,
                    status="approved",
                    spec={"title": "foreign"},
                    tasks=[],
                    requester="test",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                DiffSubmission(
                    id=uuid.uuid4(),
                    plan_id=own_plan_id,
                    task_id="own-task",
                    status="pending_approval",
                    diff="OWN_SOURCE_PAYLOAD",
                ),
                DiffSubmission(
                    id=uuid.uuid4(),
                    plan_id=foreign_plan_id,
                    task_id="foreign-task",
                    status="pending_approval",
                    diff="FOREIGN_SOURCE_PAYLOAD",
                ),
            ]
        )
        await session.commit()

    user = SimpleNamespace(organization_id=own_org_id)
    return user, own_plan_id, foreign_plan_id


@pytest.mark.asyncio
async def test_list_plan_submissions_returns_same_org_payload(database) -> None:
    user, own_plan_id, _ = await _seed_two_orgs(database)

    async with database() as session:
        rows = await diffs_api.list_plan_submissions(
            own_plan_id,
            user=user,
            db=session,
        )

    assert [(row.task_id, row.diff) for row in rows] == [
        ("own-task", "OWN_SOURCE_PAYLOAD")
    ]


@pytest.mark.asyncio
async def test_list_plan_submissions_cross_org_is_indistinguishable_from_missing(
    database,
) -> None:
    user, _, foreign_plan_id = await _seed_two_orgs(database)

    async with database() as session:
        with pytest.raises(HTTPException) as foreign_error:
            await diffs_api.list_plan_submissions(
                foreign_plan_id,
                user=user,
                db=session,
            )
        with pytest.raises(HTTPException) as missing_error:
            await diffs_api.list_plan_submissions(
                uuid.uuid4(),
                user=user,
                db=session,
            )

    assert foreign_error.value.status_code == 404
    assert foreign_error.value.detail == "not_found"
    assert (
        foreign_error.value.status_code,
        foreign_error.value.detail,
    ) == (
        missing_error.value.status_code,
        missing_error.value.detail,
    )
    assert "FOREIGN_SOURCE_PAYLOAD" not in str(foreign_error.value.detail)
