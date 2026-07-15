"""MCP audit list/detail queries must share the canonical tenant predicate."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.audit import list_audit, list_mcp_sessions, mcp_session_requests
from app.models.audit import AuditLog
from app.models.base import Base
from app.models.organization import Organization
from app.models.projects import Project
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


@pytest.mark.asyncio
async def test_mcp_audit_list_and_detail_are_project_org_scoped_in_real_sqlite():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection,
                tables=[
                    Organization.__table__,
                    Project.__table__,
                    AuditLog.__table__,
                ],
            )
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_a = Organization(
        id=uuid.uuid4(),
        slug=f"audit-a-{uuid.uuid4().hex[:8]}",
        display_name="Audit A",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"audit-b-{uuid.uuid4().hex[:8]}",
        display_name="Audit B",
    )
    project_a = Project(
        id=uuid.uuid4(),
        name="A",
        gitlab_project_id=2001,
        gitlab_url="https://gitlab.example/a",
        default_branch="main",
        languages=["python"],
        organization_id=org_a.id,
    )
    project_b = Project(
        id=uuid.uuid4(),
        name="B",
        gitlab_project_id=2002,
        gitlab_url="https://gitlab.example/b",
        default_branch="main",
        languages=["python"],
        organization_id=org_b.id,
    )
    same_actor = "mcp:same-org"
    foreign_actor = "mcp:foreign-org"
    null_actor = "mcp:null-project"
    same_log = AuditLog(
        project_id=project_a.id,
        actor=same_actor,
        action="mcp.tool.search_symbols",
        target="visible-target",
        details={"query": "visible"},
    )
    foreign_log = AuditLog(
        project_id=project_b.id,
        actor=foreign_actor,
        action="mcp.tool.get_contract",
        target="foreign-target",
        details={"secret_context": "must-not-leak"},
    )
    null_log = AuditLog(
        project_id=None,
        actor=null_actor,
        action="mcp.tool.platform_event",
        target="platform-target",
        details={"secret_metadata": "must-not-leak"},
    )

    try:
        async with factory() as session:
            session.add_all(
                [
                    org_a,
                    org_b,
                    project_a,
                    project_b,
                    same_log,
                    foreign_log,
                    null_log,
                ]
            )
            await session.commit()

            user_a = SimpleNamespace(organization_id=org_a.id)
            sessions = await list_mcp_sessions(user_a, 50, session)
            assert sessions == [
                {
                    "actor": same_actor,
                    "first_seen": same_log.occurred_at.isoformat(),
                    "last_seen": same_log.occurred_at.isoformat(),
                    "call_count": 1,
                }
            ]

            visible = await mcp_session_requests(same_actor, user_a, 100, session)
            assert len(visible) == 1
            assert visible[0]["target"] == "visible-target"
            assert visible[0]["details"] == {"query": "visible"}

            # Foreign, missing, and project-less actor ids are all the same
            # empty response and disclose no target/details/project UUID.
            assert await mcp_session_requests(
                foreign_actor, user_a, 100, session
            ) == []
            assert await mcp_session_requests(
                "mcp:missing", user_a, 100, session
            ) == []
            assert await mcp_session_requests(
                null_actor, user_a, 100, session
            ) == []

            general = await list_audit(user_a, 100, None, None, session)
            assert [entry.id for entry in general] == [same_log.id]

            orgless = SimpleNamespace(organization_id=None)
            assert await list_mcp_sessions(orgless, 50, session) == []
            assert await mcp_session_requests(
                same_actor, orgless, 100, session
            ) == []
            assert await list_audit(orgless, 100, None, None, session) == []
    finally:
        await engine.dispose()
