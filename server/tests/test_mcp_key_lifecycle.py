from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from fastapi import HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api import mcp_keys as api  # noqa: E402
from app.mcp.auth import (  # noqa: E402
    MCPAuthenticationError,
    resolve_mcp_auth_context,
)
from app.models.all_models import (  # noqa: E402
    ApiKey,
    AuditLog,
    Organization,
    Project,
    User,
)
from app.models.base import Base  # noqa: E402
from app.safety.tokens import hash_token  # noqa: E402


def test_project_mcp_key_router_is_registered() -> None:
    from app.main import create_app

    paths = create_app().openapi()["paths"]
    collection = "/api/v1/projects/{project_id}/mcp-keys"
    item = "/api/v1/projects/{project_id}/mcp-keys/{key_id}"
    assert {"get", "post"} <= set(paths[collection])
    assert "delete" in paths[item]


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    try:
        yield session_factory
    finally:
        await engine.dispose()


async def _seed(database) -> dict[str, object]:
    org_a = Organization(
        id=uuid.uuid4(),
        slug=f"mcp-key-a-{uuid.uuid4().hex}",
        display_name="MCP key A",
    )
    org_b = Organization(
        id=uuid.uuid4(),
        slug=f"mcp-key-b-{uuid.uuid4().hex}",
        display_name="MCP key B",
    )
    project_a = Project(
        id=uuid.uuid4(),
        name="MCP project A",
        gitlab_project_id=8201,
        gitlab_url="https://example.invalid/mcp-a",
        default_branch="main",
        languages=["python"],
        organization_id=org_a.id,
    )
    project_a2 = Project(
        id=uuid.uuid4(),
        name="MCP project A2",
        gitlab_project_id=8202,
        gitlab_url="https://example.invalid/mcp-a2",
        default_branch="main",
        languages=["python"],
        organization_id=org_a.id,
    )
    project_b = Project(
        id=uuid.uuid4(),
        name="MCP project B",
        gitlab_project_id=8203,
        gitlab_url="https://example.invalid/mcp-b",
        default_branch="main",
        languages=["python"],
        organization_id=org_b.id,
    )
    project_orphan = Project(
        id=uuid.uuid4(),
        name="MCP project orphan",
        gitlab_project_id=8204,
        gitlab_url="https://example.invalid/mcp-orphan",
        default_branch="main",
        languages=["python"],
        organization_id=None,
    )
    operator_a = User(
        id=uuid.uuid4(),
        username=f"mcp-key-operator-a-{uuid.uuid4().hex}",
        password_hash="unused",
        role="operator",
        organization_id=org_a.id,
    )
    admin_a = User(
        id=uuid.uuid4(),
        username=f"mcp-key-admin-a-{uuid.uuid4().hex}",
        password_hash="unused",
        role="admin",
        organization_id=org_a.id,
    )
    viewer_a = User(
        id=uuid.uuid4(),
        username=f"mcp-key-viewer-a-{uuid.uuid4().hex}",
        password_hash="unused",
        role="viewer",
        organization_id=org_a.id,
    )
    operator_b = User(
        id=uuid.uuid4(),
        username=f"mcp-key-operator-b-{uuid.uuid4().hex}",
        password_hash="unused",
        role="operator",
        organization_id=org_b.id,
    )
    operator_orphan = User(
        id=uuid.uuid4(),
        username=f"mcp-key-operator-orphan-{uuid.uuid4().hex}",
        password_hash="unused",
        role="operator",
        organization_id=None,
    )

    async with database() as session:
        session.add_all([org_a, org_b])
        await session.flush()
        session.add_all([project_a, project_a2, project_b, project_orphan])
        session.add_all(
            [operator_a, admin_a, viewer_a, operator_b, operator_orphan]
        )
        await session.commit()

    return {
        "org_a": org_a,
        "org_b": org_b,
        "project_a": project_a,
        "project_a2": project_a2,
        "project_b": project_b,
        "project_orphan": project_orphan,
        "operator_a": operator_a,
        "admin_a": admin_a,
        "viewer_a": viewer_a,
        "operator_b": operator_b,
        "operator_orphan": operator_orphan,
    }


async def test_create_returns_raw_once_and_persists_only_digest_metadata(
    database,
):
    seeded = await _seed(database)
    project = seeded["project_a"]
    operator = seeded["operator_a"]
    admin = seeded["admin_a"]
    response = Response()

    async with database() as session:
        created = await api.create_mcp_key(
            project.id,
            api.MCPKeyCreate(label="Codex source analysis"),
            response,
            operator,
            session,
        )
        stored = await session.get(ApiKey, created.id)
        assert stored is not None
        audit = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "mcp_key.create")
            )
        ).scalar_one()

        listed = await api.list_mcp_keys(project.id, operator, session)
        listed_by_admin = await api.list_mcp_keys(project.id, admin, session)

    assert created.token
    assert len(created.token) >= 40
    assert stored.token_hash == hash_token(created.token)
    assert stored.token_hash != created.token
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert len(listed) == 1
    assert [item.id for item in listed_by_admin] == [created.id]
    metadata = listed[0].model_dump()
    assert metadata["id"] == created.id
    assert "token" not in metadata
    assert "token_hash" not in metadata
    audited = json.dumps(audit.details, sort_keys=True)
    assert created.token not in audited
    assert stored.token_hash not in audited


async def test_viewer_cannot_create_list_or_revoke_and_has_no_side_effect(database):
    seeded = await _seed(database)
    project = seeded["project_a"]
    operator = seeded["operator_a"]
    viewer = seeded["viewer_a"]

    async with database() as session:
        created = await api.create_mcp_key(
            project.id,
            api.MCPKeyCreate(label="owned"),
            Response(),
            operator,
            session,
        )
        initial_audit_count = await session.scalar(
            select(func.count()).select_from(AuditLog)
        )

        for operation in (
            api.create_mcp_key(
                project.id,
                api.MCPKeyCreate(label="forbidden"),
                Response(),
                viewer,
                session,
            ),
            api.list_mcp_keys(project.id, viewer, session),
            api.revoke_mcp_key(project.id, created.id, viewer, session),
        ):
            with pytest.raises(HTTPException) as caught:
                await operation
            assert caught.value.status_code == 403

        key = await session.get(ApiKey, created.id)
        assert key is not None and key.revoked_at is None
        assert await session.scalar(select(func.count()).select_from(ApiKey)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(AuditLog))
            == initial_audit_count
        )


async def test_revoke_is_atomic_idempotent_and_blocks_future_mcp_startup(database):
    seeded = await _seed(database)
    project = seeded["project_a"]
    operator = seeded["operator_a"]
    admin = seeded["admin_a"]

    async with database() as session:
        created = await api.create_mcp_key(
            project.id,
            api.MCPKeyCreate(label="rotate me"),
            Response(),
            operator,
            session,
        )
        context = await resolve_mcp_auth_context(
            session,
            token_hash=hash_token(created.token),
            expected_project_id=project.id,
        )
        assert context.user_id == operator.id

        await api.revoke_mcp_key(project.id, created.id, admin, session)
        key = await session.get(ApiKey, created.id)
        assert key is not None and key.revoked_at is not None
        first_revoked_at = key.revoked_at

        await api.revoke_mcp_key(project.id, created.id, admin, session)
        key = await session.get(ApiKey, created.id)
        assert key is not None and key.revoked_at == first_revoked_at

        with pytest.raises(MCPAuthenticationError):
            await resolve_mcp_auth_context(
                session,
                token_hash=hash_token(created.token),
                expected_project_id=project.id,
            )
        revoke_audits = (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "mcp_key.revoke")
            )
        ).scalars().all()

    assert len(revoke_audits) == 1
    audited = json.dumps(revoke_audits[0].details, sort_keys=True)
    assert created.token not in audited
    assert hash_token(created.token) not in audited


async def test_cross_scope_missing_and_orphan_keys_share_404_without_mutation(
    database,
):
    seeded = await _seed(database)
    project_a = seeded["project_a"]
    project_a2 = seeded["project_a2"]
    project_b = seeded["project_b"]
    project_orphan = seeded["project_orphan"]
    operator_a = seeded["operator_a"]
    operator_b = seeded["operator_b"]
    operator_orphan = seeded["operator_orphan"]
    raw = {
        "valid": "valid-key",
        "orphan": "orphan-key",
        "unscoped": "unscoped-key",
        "cross_org": "cross-org-key",
    }
    ids = {name: uuid.uuid4() for name in raw}

    async with database() as session:
        session.add_all(
            [
                ApiKey(
                    id=ids["valid"],
                    user_id=operator_a.id,
                    project_id=project_a.id,
                    token_hash=hash_token(raw["valid"]),
                    label="valid",
                ),
                ApiKey(
                    id=ids["orphan"],
                    user_id=None,
                    project_id=project_a.id,
                    token_hash=hash_token(raw["orphan"]),
                    label="orphan",
                ),
                ApiKey(
                    id=ids["unscoped"],
                    user_id=operator_a.id,
                    project_id=None,
                    token_hash=hash_token(raw["unscoped"]),
                    label="unscoped",
                ),
                ApiKey(
                    id=ids["cross_org"],
                    user_id=operator_b.id,
                    project_id=project_a.id,
                    token_hash=hash_token(raw["cross_org"]),
                    label="cross-org",
                ),
            ]
        )
        await session.commit()

        attempts = [
            (project_a2.id, ids["valid"], operator_a),
            (project_b.id, ids["valid"], operator_b),
            (project_a.id, uuid.uuid4(), operator_a),
            (project_a.id, ids["orphan"], operator_a),
            (project_a.id, ids["unscoped"], operator_a),
            (project_a.id, ids["cross_org"], operator_a),
            (uuid.uuid4(), ids["valid"], operator_a),
            (project_orphan.id, ids["valid"], operator_orphan),
        ]
        for project_id, key_id, user in attempts:
            with pytest.raises(HTTPException) as caught:
                await api.revoke_mcp_key(project_id, key_id, user, session)
            assert caught.value.status_code == 404
            assert caught.value.detail == "not_found"

        rows = (
            await session.execute(select(ApiKey).where(ApiKey.id.in_(ids.values())))
        ).scalars().all()

    assert len(rows) == len(ids)
    assert all(row.revoked_at is None for row in rows)
