"""Project list/detail/create tenancy is exact-org and NULL fail-closed."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.projects import ProjectCreate, create_project, list_projects
from app.auth.org_scope import require_project_org
from app.models.base import Base
from app.models.organization import Organization
from app.models.projects import Project
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


@pytest.mark.asyncio
async def test_project_org_boundaries_against_real_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[Organization.__table__, Project.__table__],
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_a = Organization(
        id=uuid.uuid4(),
        slug=f"project-a-{uuid.uuid4().hex[:8]}",
        display_name="Project A org",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"project-b-{uuid.uuid4().hex[:8]}",
        display_name="Project B org",
    )

    def _project(name: str, gitlab_id: int, org_id):
        return Project(
            id=uuid.uuid4(),
            name=name,
            gitlab_project_id=gitlab_id,
            gitlab_url=f"https://gitlab.example/{name}",
            default_branch="main",
            languages=["python"],
            organization_id=org_id,
        )

    project_a = _project("a", 3001, org_a.id)
    project_b = _project("b", 3002, org_b.id)
    project_null = _project("legacy-null", 3003, None)

    try:
        async with factory() as session:
            session.add_all(
                [org_a, org_b, project_a, project_b, project_null]
            )
            await session.commit()

            user_a = SimpleNamespace(id=uuid.uuid4(), organization_id=org_a.id)
            assert [row.id for row in await list_projects(user_a, session)] == [
                project_a.id
            ]
            orgless = SimpleNamespace(id=uuid.uuid4(), organization_id=None)
            assert await list_projects(orgless, session) == []

            project_dep = require_project_org()
            assert await project_dep(project_a.id, user_a, session) is user_a
            for hidden_id in (project_b.id, project_null.id, uuid.uuid4()):
                with pytest.raises(HTTPException) as raised:
                    await project_dep(hidden_id, user_a, session)
                assert (raised.value.status_code, raised.value.detail) == (
                    404,
                    "not_found",
                )

            with pytest.raises(HTTPException) as raised:
                await create_project(
                    ProjectCreate(
                        name="orphan",
                        gitlab_project_id=3999,
                        gitlab_url="https://gitlab.example/orphan",
                        languages=["python"],
                    ),
                    orgless,
                    session,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                403,
                "organization_required",
            )
    finally:
        await engine.dispose()
