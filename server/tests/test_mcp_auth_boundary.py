from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from mcp.types import CallToolRequest, CallToolRequestParams, ListToolsRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.mcp import dev_tools  # noqa: E402
from app.mcp import server as mcp_server  # noqa: E402
from app.mcp.auth import (  # noqa: E402
    MCPAuthenticationError,
    resolve_mcp_auth_context,
)
from app.models.all_models import ApiKey, Organization, Project, User  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.plans import Plan  # noqa: E402
from app.safety.tokens import hash_token  # noqa: E402


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


async def _seed_auth_matrix(session_factory) -> dict[str, object]:
    org_a_id = uuid.uuid4()
    org_b_id = uuid.uuid4()
    project_a_id = uuid.uuid4()
    project_b_id = uuid.uuid4()
    project_same_org_id = uuid.uuid4()
    project_orphan_id = uuid.uuid4()
    user_a_id = uuid.uuid4()
    viewer_a_id = uuid.uuid4()
    disabled_a_id = uuid.uuid4()
    user_b_id = uuid.uuid4()
    orgless_id = uuid.uuid4()

    now = datetime.now(timezone.utc)
    credentials = {
        "valid": "mcp-valid-operator",
        "viewer": "mcp-valid-viewer",
        "revoked": "mcp-revoked",
        "orphan": "mcp-orphan-key",
        "unscoped": "mcp-unscoped-key",
        "disabled": "mcp-disabled-user",
        "orgless": "mcp-orgless-user",
        "cross_org": "mcp-cross-org",
        "cross_project": "mcp-other-project",
        "orphan_project": "mcp-orphan-project",
    }

    async with session_factory() as session:
        session.add_all(
            [
                Organization(id=org_a_id, slug="mcp-a", display_name="MCP A"),
                Organization(id=org_b_id, slug="mcp-b", display_name="MCP B"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                Project(
                    id=project_a_id,
                    name="project-a",
                    gitlab_project_id=8101,
                    gitlab_url="https://example.invalid/a",
                    default_branch="main",
                    languages=["python"],
                    organization_id=org_a_id,
                ),
                Project(
                    id=project_b_id,
                    name="project-b",
                    gitlab_project_id=8102,
                    gitlab_url="https://example.invalid/b",
                    default_branch="main",
                    languages=["python"],
                    organization_id=org_b_id,
                ),
                Project(
                    id=project_same_org_id,
                    name="project-a-two",
                    gitlab_project_id=8104,
                    gitlab_url="https://example.invalid/a-two",
                    default_branch="main",
                    languages=["python"],
                    organization_id=org_a_id,
                ),
                Project(
                    id=project_orphan_id,
                    name="project-orphan",
                    gitlab_project_id=8103,
                    gitlab_url="https://example.invalid/orphan",
                    default_branch="main",
                    languages=["python"],
                    organization_id=None,
                ),
                User(
                    id=user_a_id,
                    username="mcp-operator-a",
                    password_hash="unused",
                    role="operator",
                    organization_id=org_a_id,
                ),
                User(
                    id=viewer_a_id,
                    username="mcp-viewer-a",
                    password_hash="unused",
                    role="viewer",
                    organization_id=org_a_id,
                ),
                User(
                    id=disabled_a_id,
                    username="mcp-disabled-a",
                    password_hash="unused",
                    role="operator",
                    organization_id=org_a_id,
                    disabled_at=now,
                ),
                User(
                    id=user_b_id,
                    username="mcp-operator-b",
                    password_hash="unused",
                    role="operator",
                    organization_id=org_b_id,
                ),
                User(
                    id=orgless_id,
                    username="mcp-orgless",
                    password_hash="unused",
                    role="operator",
                    organization_id=None,
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_a_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["valid"]),
                    label="valid",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=viewer_a_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["viewer"]),
                    label="viewer",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_a_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["revoked"]),
                    label="revoked",
                    revoked_at=now,
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=None,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["orphan"]),
                    label="orphan",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_a_id,
                    project_id=None,
                    token_hash=hash_token(credentials["unscoped"]),
                    label="unscoped",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=disabled_a_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["disabled"]),
                    label="disabled",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=orgless_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["orgless"]),
                    label="orgless",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_b_id,
                    project_id=project_a_id,
                    token_hash=hash_token(credentials["cross_org"]),
                    label="cross-org",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_a_id,
                    project_id=project_same_org_id,
                    token_hash=hash_token(credentials["cross_project"]),
                    label="cross-project",
                ),
                ApiKey(
                    id=uuid.uuid4(),
                    user_id=user_a_id,
                    project_id=project_orphan_id,
                    token_hash=hash_token(credentials["orphan_project"]),
                    label="orphan-project",
                ),
            ]
        )
        await session.commit()

    return {
        "org_a_id": org_a_id,
        "project_a_id": project_a_id,
        "project_b_id": project_b_id,
        "project_orphan_id": project_orphan_id,
        "user_a_id": user_a_id,
        "credentials": credentials,
    }


@pytest.mark.parametrize(
    "credential_name",
    [
        "revoked",
        "orphan",
        "unscoped",
        "disabled",
        "orgless",
        "cross_org",
        "cross_project",
        "orphan_project",
    ],
)
async def test_resolver_rejects_every_inactive_or_wrong_scope_binding(
    database,
    credential_name,
):
    seeded = await _seed_auth_matrix(database)
    credentials = seeded["credentials"]
    expected_project_id = (
        seeded["project_orphan_id"]
        if credential_name == "orphan_project"
        else seeded["project_a_id"]
    )
    async with database() as session:
        with pytest.raises(MCPAuthenticationError, match="invalid_mcp_credentials"):
            await resolve_mcp_auth_context(
                session,
                token_hash=hash_token(credentials[credential_name]),
                expected_project_id=expected_project_id,
            )


async def test_resolver_rejects_random_token_and_cli_project_mismatch(database):
    seeded = await _seed_auth_matrix(database)
    credentials = seeded["credentials"]
    async with database() as session:
        for token_hash, project_id in (
            (hash_token("random-token"), seeded["project_a_id"]),
            (hash_token(credentials["valid"]), seeded["project_b_id"]),
        ):
            with pytest.raises(MCPAuthenticationError):
                await resolve_mcp_auth_context(
                    session,
                    token_hash=token_hash,
                    expected_project_id=project_id,
                )


async def test_resolver_returns_immutable_exact_operator_context(database):
    seeded = await _seed_auth_matrix(database)
    credentials = seeded["credentials"]
    async with database() as session:
        context = await resolve_mcp_auth_context(
            session,
            token_hash=hash_token(credentials["valid"]),
            expected_project_id=seeded["project_a_id"],
        )

    assert context.project_id == seeded["project_a_id"]
    assert context.organization_id == seeded["org_a_id"]
    assert context.user_id == seeded["user_a_id"]
    assert context.role == "operator"
    assert credentials["valid"] not in repr(context)
    with pytest.raises((AttributeError, TypeError)):
        context.role = "admin"


def test_stdio_environment_token_is_hashed_before_database_resolution(monkeypatch):
    raw_token = "raw-mcp-token-must-never-be-logged"
    monkeypatch.setenv("MNEMOS_MCP_TOKEN", raw_token)

    token_hash = mcp_server._require_mcp_token()

    assert token_hash == hash_token(raw_token)
    assert raw_token not in token_hash
    assert "MNEMOS_MCP_TOKEN" not in os.environ


async def test_random_startup_credential_fails_before_stdio_opens(
    database,
    monkeypatch,
    capsys,
):
    seeded = await _seed_auth_matrix(database)
    monkeypatch.setattr(mcp_server, "SessionLocal", database)

    def must_not_open_stdio():
        raise AssertionError("invalid credential opened MCP stdio")

    monkeypatch.setattr(mcp_server, "stdio_server", must_not_open_stdio)
    raw_token = "random-startup-credential"
    with pytest.raises(SystemExit) as exc:
        await mcp_server._run(
            seeded["project_a_id"],
            token_hash=hash_token(raw_token),
        )

    assert exc.value.code == 2
    stderr = capsys.readouterr().err
    assert "MNEMOS_MCP_AUTH_invalid" in stderr
    assert raw_token not in stderr
    assert hash_token(raw_token) not in stderr


async def _call(server, name: str, arguments: dict) -> dict:
    handler = server.request_handlers[CallToolRequest]
    response = await handler(
        CallToolRequest(
            params=CallToolRequestParams(name=name, arguments=arguments)
        )
    )
    return json.loads(response.root.content[0].text)


async def _listed_tool_names(server) -> set[str]:
    handler = server.request_handlers[ListToolsRequest]
    response = await handler(ListToolsRequest())
    return {tool.name for tool in response.root.tools}


async def _context(database, seeded, name: str):
    async with database() as session:
        return await resolve_mcp_auth_context(
            session,
            token_hash=hash_token(seeded["credentials"][name]),
            expected_project_id=seeded["project_a_id"],
        )


async def test_viewer_mutation_is_denied_before_graph_db_or_filesystem_side_effect(
    database,
    monkeypatch,
):
    seeded = await _seed_auth_matrix(database)
    context = await _context(database, seeded, "viewer")
    monkeypatch.setattr(mcp_server, "SessionLocal", database)

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("mutation side effect reached")

    monkeypatch.setattr(mcp_server, "read_graph_stamp", must_not_run)
    monkeypatch.setattr(mcp_server, "submit_plan_tool", must_not_run)
    result = await _call(
        mcp_server.build_server(context),
        "submit_plan",
        {
            "spec": {"title": "forbidden"},
            "tasks": [],
            "target_component_id": "symbol:x",
        },
    )

    assert result["error"] == mcp_server.MCP_AUTHORIZATION_DENIED_CODE
    async with database() as session:
        assert (await session.execute(select(Plan))).scalars().all() == []


async def test_viewer_tool_discovery_exposes_only_explicit_read_only_allowlist(
    database,
    monkeypatch,
):
    seeded = await _seed_auth_matrix(database)
    context = await _context(database, seeded, "viewer")
    monkeypatch.setattr(mcp_server, "SessionLocal", database)

    names = await _listed_tool_names(mcp_server.build_server(context))

    assert names == mcp_server._READ_ONLY_TOOL_NAMES
    assert not (names & mcp_server._MUTATING_TOOL_NAMES)


async def test_revocation_after_start_blocks_next_call_before_side_effect(
    database,
    monkeypatch,
):
    seeded = await _seed_auth_matrix(database)
    context = await _context(database, seeded, "valid")
    monkeypatch.setattr(mcp_server, "SessionLocal", database)

    async with database() as session:
        key = (
            await session.execute(
                select(ApiKey).where(ApiKey.id == context.api_key_id)
            )
        ).scalar_one()
        key.revoked_at = datetime.now(timezone.utc)
        await session.commit()

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("revoked session reached a side effect")

    monkeypatch.setattr(mcp_server, "read_graph_stamp", must_not_run)
    monkeypatch.setattr(mcp_server, "submit_plan_tool", must_not_run)
    result = await _call(
        mcp_server.build_server(context),
        "submit_plan",
        {"spec": {}, "tasks": [], "target_component_id": "symbol:x"},
    )
    assert result["error"] == mcp_server.MCP_AUTHENTICATION_INVALID_CODE


async def test_valid_operator_dispatch_is_bound_to_context_project_and_actor(
    database,
    monkeypatch,
):
    seeded = await _seed_auth_matrix(database)
    context = await _context(database, seeded, "valid")
    monkeypatch.setattr(mcp_server, "SessionLocal", database)
    captured: dict[str, object] = {}

    async def read_stamp(_session, *, project_id):
        captured["stamped_project_id"] = project_id
        return object()

    async def submit(_session, **kwargs):
        captured.update(kwargs)
        return {"success": True}

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(mcp_server, "read_graph_stamp", read_stamp)
    monkeypatch.setattr(mcp_server, "submit_plan_tool", submit)
    monkeypatch.setattr(mcp_server, "audit_record", no_audit)
    result = await _call(
        mcp_server.build_server(context),
        "submit_plan",
        {"spec": {}, "tasks": [], "target_component_id": "symbol:x"},
    )

    assert result == {"success": True}
    assert captured["project_id"] == context.project_id
    assert captured["stamped_project_id"] == context.project_id
    assert captured["requester"] == context.actor


async def test_foreign_plan_id_is_rejected_before_file_process_or_review_effect(
    database,
    monkeypatch,
):
    seeded = await _seed_auth_matrix(database)
    context = await _context(database, seeded, "valid")
    foreign_plan_id = uuid.uuid4()
    async with database() as session:
        session.add(
            Plan(
                id=foreign_plan_id,
                project_id=seeded["project_b_id"],
                status="approved",
                spec={},
                tasks=[],
                requester="foreign",
            )
        )
        await session.commit()

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("foreign Plan reached an external side effect")

    monkeypatch.setattr(dev_tools, "resolve_in_worktree", must_not_run)
    monkeypatch.setattr(dev_tools, "run_in_sandbox", must_not_run)
    monkeypatch.setattr(dev_tools, "run_pipeline", must_not_run)
    monkeypatch.setattr(dev_tools, "compute_diff", must_not_run)

    async with database() as session:
        edit = await dev_tools.edit_file_in_worktree(
            session,
            auth_context=context,
            expected_project_id=seeded["project_a_id"],
            plan_id=foreign_plan_id,
            file_path="src/x.py",
            edits=[],
        )
        run = await dev_tools.run_in_sandbox_tool(
            session,
            auth_context=context,
            expected_project_id=seeded["project_a_id"],
            plan_id=foreign_plan_id,
            command="pytest",
        )
        diff = await dev_tools.submit_diff(
            session,
            auth_context=context,
            expected_project_id=seeded["project_a_id"],
            plan_id=foreign_plan_id,
            task_id="t1",
        )

    assert edit == run == diff == {"error": "plan_not_found"}
