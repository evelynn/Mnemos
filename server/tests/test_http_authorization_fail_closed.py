"""HTTP authentication/authorization boundaries fail closed.

These regressions use a real SQLite schema for ownership and persisted side
effects; only external services (Redis, Git worktrees, STT/OIDC providers) are
replaced at their documented seams.
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Request, Response, UploadFile
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import comments as comments_api
from app.api import analysis as analysis_api
from app.api import data as data_api
from app.api import diffs as diffs_api
from app.api import plans as plans_api
from app.api import projects as projects_api
from app.api import voice as voice_api
from app.api.graph_guard import require_readable_current_graph
from app.auth import deps as auth_deps
from app.auth import oidc
from app.auth.deps import current_user
from app.auth.passwords import hash_password
from app.dashboard import router as dashboard
from app.db import get_session
from app.models import all_models as _all_models  # noqa: F401
from app.models.auth import User
from app.models.base import Base
from app.models.comments import Comment
from app.models.findings import Finding
from app.models.graph import AnalysisRun, Node
from app.models.organization import Organization
from app.models.plans import DiffSubmission, Plan
from app.models.projects import Project
from app.models.samples import DataSample
from app.plan_provenance import PlanSourceRevision
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


@pytest.fixture
async def sqlite_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


def _project(name: str, org_id: uuid.UUID | None, number: int) -> Project:
    return Project(
        id=uuid.uuid4(),
        name=name,
        gitlab_project_id=number,
        gitlab_url=f"https://example.invalid/{name}",
        default_branch="main",
        languages=["python"],
        organization_id=org_id,
    )


def _user(
    username: str,
    org_id: uuid.UUID | None,
    *,
    role: str = "operator",
    disabled: bool = False,
) -> User:
    return User(
        id=uuid.uuid4(),
        username=username,
        password_hash=hash_password("ValidPassword123!"),
        role=role,
        organization_id=org_id,
        disabled_at=datetime.now(timezone.utc) if disabled else None,
    )


@pytest.mark.asyncio
async def test_orphan_plan_diff_comment_and_finding_reads_are_hidden(
    sqlite_factory,
) -> None:
    org = Organization(
        id=uuid.uuid4(),
        slug=f"org-{uuid.uuid4().hex}",
        display_name="Concrete org",
    )
    foreign_org = Organization(
        id=uuid.uuid4(),
        slug=f"foreign-{uuid.uuid4().hex}",
        display_name="Foreign org",
    )
    own_project = _project("own", org.id, 4101)
    orphan_project = _project("orphan", None, 4102)
    foreign_project = _project("foreign", foreign_org.id, 4103)
    own_plan = Plan(
        id=uuid.uuid4(),
        project_id=own_project.id,
        spec={"title": "own"},
        tasks=[],
        impact_report={},
        requester="test",
    )
    orphan_finding = Finding(
        id=uuid.uuid4(),
        project_id=orphan_project.id,
        kind="risk",
        identity_key="a" * 64,
        severity="high",
        detail={},
        risk_score=90,
    )
    orphan_plan = Plan(
        id=uuid.uuid4(),
        project_id=orphan_project.id,
        spec={"title": "orphan"},
        tasks=[],
        impact_report={"source_finding_id": str(orphan_finding.id)},
        requester="test",
    )
    foreign_finding = Finding(
        id=uuid.uuid4(),
        project_id=foreign_project.id,
        kind="risk",
        identity_key="b" * 64,
        severity="high",
        detail={},
        risk_score=90,
    )
    foreign_plan = Plan(
        id=uuid.uuid4(),
        project_id=foreign_project.id,
        spec={"title": "foreign"},
        tasks=[],
        impact_report={"source_finding_id": str(foreign_finding.id)},
        requester="test",
    )
    orphan_diff = DiffSubmission(
        id=uuid.uuid4(),
        plan_id=orphan_plan.id,
        task_id="orphan-task",
        diff="ORPHAN_SECRET_DIFF",
    )
    orphan_comment = Comment(
        id=uuid.uuid4(),
        target_kind="plan",
        target_id=orphan_plan.id,
        body="ORPHAN_SECRET_COMMENT",
    )
    foreign_diff = DiffSubmission(
        id=uuid.uuid4(),
        plan_id=foreign_plan.id,
        task_id="foreign-task",
        diff="FOREIGN_SECRET_DIFF",
    )
    async with sqlite_factory() as session:
        session.add_all([org, foreign_org])
        await session.flush()
        session.add_all([own_project, orphan_project, foreign_project])
        await session.flush()
        session.add_all(
            [
                own_plan,
                orphan_finding,
                orphan_plan,
                foreign_finding,
                foreign_plan,
            ]
        )
        await session.flush()
        session.add_all([orphan_diff, foreign_diff, orphan_comment])
        await session.commit()

        concrete_user = SimpleNamespace(organization_id=org.id)
        orgless_user = SimpleNamespace(organization_id=None)
        await comments_api._check_target_in_user_org(session, "plan", own_plan.id, concrete_user)

        for kind, target_id in (
            ("plan", foreign_plan.id),
            ("diff_submission", foreign_diff.id),
        ):
            with pytest.raises(HTTPException) as raised:
                await comments_api._check_target_in_user_org(
                    session, kind, target_id, concrete_user
                )
            assert raised.value.status_code == 404

        for kind, target_id in (
            ("plan", orphan_plan.id),
            ("diff_submission", orphan_diff.id),
        ):
            with pytest.raises(HTTPException) as raised:
                await comments_api._check_target_in_user_org(session, kind, target_id, orgless_user)
            assert raised.value.status_code == 404

        with pytest.raises(HTTPException) as raised:
            await plans_api.plans_for_finding(
                orphan_finding.id,
                user=orgless_user,
                db=session,
            )
        assert (raised.value.status_code, raised.value.detail) == (
            404,
            "finding_not_found",
        )
        with pytest.raises(HTTPException) as raised:
            await plans_api.plans_for_finding(
                foreign_finding.id,
                user=concrete_user,
                db=session,
            )
        assert (raised.value.status_code, raised.value.detail) == (
            404,
            "finding_not_found",
        )
        assert (
            await diffs_api.list_submissions_filtered(
                user=orgless_user,
                db=session,
            )
            == []
        )


@pytest.mark.asyncio
async def test_disabled_cookie_dashboard_and_oidc_are_all_rejected(
    sqlite_factory,
    monkeypatch,
) -> None:
    disabled = _user("disabled-user", None, disabled=True)
    disabled_username = disabled.username
    async with sqlite_factory() as session:
        session.add(disabled)
        await session.commit()

        revoked: list[str] = []

        async def _read_session(_token: str):
            return disabled.id

        async def _delete_session(token: str):
            revoked.append(token)

        monkeypatch.setattr(auth_deps, "read_session", _read_session)
        monkeypatch.setattr(auth_deps, "delete_session", _delete_session)

        with pytest.raises(HTTPException) as raised:
            await auth_deps.current_user("stale-api-cookie", session)
        assert (raised.value.status_code, raised.value.detail) == (
            401,
            "invalid_session",
        )
        assert revoked == ["stale-api-cookie"]

        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [],
                "query_string": b"",
            }
        )
        page = await dashboard.home(
            request,
            session_token="stale-dashboard-cookie",
            db=session,
        )
        assert page.status_code == 303
        assert page.headers["location"] == "/login"
        assert revoked == ["stale-api-cookie", "stale-dashboard-cookie"]

        monkeypatch.setattr(analysis_api.app_db, "SessionLocal", sqlite_factory)
        with pytest.raises(HTTPException) as raised:
            await analysis_api._require_sse_run_access(
                uuid.uuid4(),
                session_token="stale-sse-cookie",
            )
        assert (raised.value.status_code, raised.value.detail) == (
            401,
            "invalid_session",
        )
        assert revoked == [
            "stale-api-cookie",
            "stale-dashboard-cookie",
            "stale-sse-cookie",
        ]

        login_request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/login",
                "headers": [],
                "query_string": b"",
            }
        )
        created_sessions: list[uuid.UUID] = []
        audit_actions: list[str] = []

        async def _not_locked(_username: str) -> bool:
            return False

        async def _record_failure(_username: str) -> int:
            return 1

        async def _clear(_username: str) -> None:
            return None

        async def _create_session(user_id: uuid.UUID) -> str:
            created_sessions.append(user_id)
            return "must-not-be-created"

        async def _audit(**kwargs) -> None:
            audit_actions.append(kwargs["action"])

        monkeypatch.setattr(dashboard.brute_force, "is_locked", _not_locked)
        monkeypatch.setattr(dashboard.brute_force, "record_failure", _record_failure)
        monkeypatch.setattr(dashboard.brute_force, "clear", _clear)
        monkeypatch.setattr(dashboard, "create_session", _create_session)
        monkeypatch.setattr(dashboard, "audit_record", _audit)
        login_response = await dashboard.login_submit(
            login_request,
            Response(),
            username=disabled_username,
            password="ValidPassword123!",
            db=session,
        )
        assert login_response.status_code == 401
        assert created_sessions == []
        assert audit_actions == ["auth.login_blocked_disabled"]

        class _Redis:
            async def get(self, _key):
                return json.dumps({"verifier": "verifier"})

            async def delete(self, _key):
                return None

        class _TokenResponse:
            status_code = 200

            @staticmethod
            def json():
                return {"id_token": "verified-by-test-seam"}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, *_args, **_kwargs):
                return _TokenResponse()

        async def _redis():
            return _Redis()

        async def _discovery():
            return {"token_endpoint": "https://idp.invalid/token"}

        async def _claims(*_args):
            return {"preferred_username": disabled_username, "sub": "disabled"}

        settings = SimpleNamespace(
            oidc_redirect_uri="https://mnemos.invalid/callback",
            oidc_client_id="client",
            oidc_client_secret="secret",
            oidc_issuer="https://idp.invalid",
            session_cookie_name="mnemos_session",
            session_max_age_sec=60,
            session_cookie_secure=True,
        )
        oidc_sessions: list[uuid.UUID] = []

        async def _oidc_create_session(user_id: uuid.UUID):
            oidc_sessions.append(user_id)
            return "must-not-be-created"

        monkeypatch.setattr(oidc, "_enabled", lambda: True)
        monkeypatch.setattr(oidc, "_settings", lambda: settings)
        monkeypatch.setattr(oidc, "get_redis", _redis)
        monkeypatch.setattr(oidc, "_discovery", _discovery)
        monkeypatch.setattr(oidc, "_verify_id_token", _claims)
        monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda **_kwargs: _Client())
        monkeypatch.setattr(oidc, "create_session", _oidc_create_session)
        monkeypatch.setattr(oidc, "audit_record", _audit)

        with pytest.raises(HTTPException) as raised:
            await oidc.callback(
                code="code",
                state="state",
                response=Response(),
                db=session,
            )
        assert (raised.value.status_code, raised.value.detail) == (
            401,
            "invalid_credentials",
        )
        assert oidc_sessions == []
        assert audit_actions[-1] == "auth.oidc_login_blocked_disabled"


@pytest.mark.asyncio
async def test_dashboard_login_honours_brute_force_lockout(
    sqlite_factory,
    monkeypatch,
) -> None:
    async with sqlite_factory() as session:

        async def _locked(_username: str) -> bool:
            return True

        async def _must_not_create(_user_id):
            raise AssertionError("locked dashboard login created a session")

        async def _audit(**_kwargs):
            return None

        monkeypatch.setattr(dashboard.brute_force, "is_locked", _locked)
        monkeypatch.setattr(dashboard, "create_session", _must_not_create)
        monkeypatch.setattr(dashboard, "audit_record", _audit)
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/login",
                "headers": [],
                "query_string": b"",
            }
        )
        response = await dashboard.login_submit(
            request,
            Response(),
            username="locked-user",
            password="irrelevant",
            db=session,
        )
        assert response.status_code == 429


@pytest.mark.asyncio
async def test_viewer_mutations_have_no_side_effect_and_operator_paths_work(
    sqlite_factory,
    monkeypatch,
    tmp_path,
) -> None:
    org = Organization(
        id=uuid.uuid4(),
        slug=f"rbac-{uuid.uuid4().hex}",
        display_name="RBAC org",
    )
    project = _project("rbac-project", org.id, 4201)
    run = AnalysisRun(
        id=uuid.uuid4(),
        project_id=project.id,
        status="completed",
        triggered_by="test",
        git_sha="a" * 40,
        scope="full",
    )
    entity = Node(
        id="data:customers",
        project_id=project.id,
        kind="DataEntity",
        data={"name": "customers", "is_sensitive": False},
        certainty="verified",
        created_by=["test"],
    )
    viewer = _user("viewer", org.id, role="viewer")
    operator = _user("operator", org.id, role="operator")
    actor = {"user": viewer}
    calls = {"bootstrap": 0, "plan_lock": 0, "worktree": 0, "sample_lock": 0}

    async with sqlite_factory() as session:
        session.add(org)
        await session.flush()
        session.add_all([project, viewer, operator])
        await session.flush()
        session.add_all([run, entity])
        await session.commit()

        async def _session_override():
            yield session

        async def _user_override():
            return actor["user"]

        async def _graph_guard_override():
            return None

        async def _bootstrap(_db, *, project_id):
            assert project_id is not None
            calls["bootstrap"] += 1

        revision = PlanSourceRevision(
            run_id=run.id,
            git_sha=run.git_sha,
            source_generation=1,
            overlay_generation=0,
        )

        async def _plan_lock(*_args, **_kwargs):
            calls["plan_lock"] += 1
            return revision

        async def _impact(*_args, **_kwargs):
            return {
                "directly_affected": [],
                "transitively_affected": [],
                "opaque_components_touched": [],
            }

        async def _worktree(plan_id, project_id, *, base_sha):
            calls["worktree"] += 1
            path = tmp_path / str(project_id) / str(plan_id)
            path.mkdir(parents=True)
            assert base_sha == run.git_sha
            return path

        async def _sample_lock(*_args, **_kwargs):
            calls["sample_lock"] += 1
            return (
                SimpleNamespace(current_run_id=run.id, overlay_generation=0),
                SimpleNamespace(generation=1),
            )

        async def _no_audit(**_kwargs):
            return None

        async def _no_rate_limit(*_args, **_kwargs):
            return None

        monkeypatch.setattr(projects_api, "bootstrap_graph_head", _bootstrap)
        monkeypatch.setattr(projects_api, "audit_record", _no_audit)
        monkeypatch.setattr(plans_api, "lock_current_plan_source_revision", _plan_lock)
        monkeypatch.setattr(plans_api, "impact_analysis", _impact)
        monkeypatch.setattr(plans_api, "create_worktree", _worktree)
        monkeypatch.setattr(plans_api, "audit_record", _no_audit)
        monkeypatch.setattr(data_api, "lock_validated_graph_head", _sample_lock)
        monkeypatch.setattr(data_api, "audit_record", _no_audit)
        monkeypatch.setattr(data_api, "rl_enforce", _no_rate_limit)

        app = FastAPI()
        app.include_router(projects_api.router)
        app.include_router(plans_api.router)
        app.include_router(data_api.router)
        app.dependency_overrides[get_session] = _session_override
        app.dependency_overrides[current_user] = _user_override
        app.dependency_overrides[require_readable_current_graph] = _graph_guard_override

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            project_body = {
                "name": "new-project",
                "gitlab_project_id": 4202,
                "gitlab_url": "https://example.invalid/new-project",
                "languages": ["python"],
            }
            plan_body = {
                "spec": {
                    "title": "Plan",
                    "motivation": "Test operator boundary",
                    "non_goals": [],
                    "success_criteria": [],
                },
                "tasks": [],
                "target_component_id": "symbol:test",
                "requester": "test",
            }
            sample_body = {
                "columns": ["email"],
                "rows": [["person@example.com"]],
                "row_count_estimate": 1,
            }

            assert (await client.post("/api/v1/projects", json=project_body)).status_code == 403
            assert (
                await client.patch(
                    f"/api/v1/projects/{project.id}",
                    json={"name": "viewer-must-not-write"},
                )
            ).status_code == 403
            assert (
                await client.post(f"/api/v1/projects/{project.id}/plans", json=plan_body)
            ).status_code == 403
            assert (
                await client.post(
                    f"/api/v1/projects/{project.id}/data_entities/{entity.id}/refresh_sample",
                    json=sample_body,
                )
            ).status_code == 403
            assert calls == {
                "bootstrap": 0,
                "plan_lock": 0,
                "worktree": 0,
                "sample_lock": 0,
            }
            assert int((await session.execute(select(func.count(Plan.id)))).scalar_one()) == 0
            assert int((await session.execute(select(func.count(DataSample.id)))).scalar_one()) == 0
            await session.refresh(project)
            assert project.name == "rbac-project"

            actor["user"] = operator
            assert (await client.post("/api/v1/projects", json=project_body)).status_code == 201
            updated = await client.patch(
                f"/api/v1/projects/{project.id}",
                json={"name": "operator-updated"},
            )
            assert updated.status_code == 200
            assert updated.json()["name"] == "operator-updated"
            assert (
                await client.post(f"/api/v1/projects/{project.id}/plans", json=plan_body)
            ).status_code == 200
            assert (
                await client.post(
                    f"/api/v1/projects/{project.id}/data_entities/{entity.id}/refresh_sample",
                    json=sample_body,
                )
            ).status_code == 200
            assert calls == {
                "bootstrap": 1,
                "plan_lock": 1,
                "worktree": 1,
                "sample_lock": 1,
            }


@pytest.mark.asyncio
async def test_voice_audit_project_is_exact_org_and_never_writes_on_mismatch(
    sqlite_factory,
    monkeypatch,
) -> None:
    org_a = Organization(id=uuid.uuid4(), slug=f"voice-a-{uuid.uuid4().hex}", display_name="A")
    org_b = Organization(id=uuid.uuid4(), slug=f"voice-b-{uuid.uuid4().hex}", display_name="B")
    own = _project("voice-own", org_a.id, 4301)
    foreign = _project("voice-foreign", org_b.id, 4302)
    orphan = _project("voice-orphan", None, 4303)
    user = SimpleNamespace(id=uuid.uuid4(), organization_id=org_a.id, role="operator")
    audit_projects: list[uuid.UUID | None] = []

    async def _audit(**kwargs):
        audit_projects.append(kwargs.get("project_id"))

    monkeypatch.setattr(voice_api, "audit_record", _audit)
    monkeypatch.setattr(voice_api, "is_stt_available", lambda: False)

    async with sqlite_factory() as session:
        session.add_all([org_a, org_b])
        await session.flush()
        session.add_all([own, foreign, orphan])
        await session.commit()

        for hidden in (foreign.id, orphan.id, uuid.uuid4()):
            with pytest.raises(HTTPException) as raised:
                await voice_api.transcribe(
                    user=user,
                    audio=UploadFile(filename="voice.wav", file=io.BytesIO(b"x")),
                    language=None,
                    project_id=str(hidden),
                    db=session,
                )
            assert (raised.value.status_code, raised.value.detail) == (
                404,
                "not_found",
            )
        with pytest.raises(HTTPException) as raised:
            await voice_api.transcribe(
                user=user,
                audio=UploadFile(filename="voice.wav", file=io.BytesIO(b"x")),
                language=None,
                project_id="not-a-uuid",
                db=session,
            )
        assert raised.value.status_code == 404
        assert audit_projects == []

        # Same-org resolution succeeds; the next independent availability
        # gate proves ownership was accepted without writing an AuditLog.
        with pytest.raises(HTTPException) as raised:
            await voice_api.transcribe(
                user=user,
                audio=UploadFile(filename="voice.wav", file=io.BytesIO(b"x")),
                language=None,
                project_id=str(own.id),
                db=session,
            )
        assert (raised.value.status_code, raised.value.detail) == (
            503,
            "stt_unavailable",
        )
        assert audit_projects == []

        class _Engine:
            @staticmethod
            def transcribe(_raw: bytes, *, language=None):
                return SimpleNamespace(
                    text="same org",
                    language=language,
                    duration_s=0.1,
                    engine="test",
                    model="test",
                )

        monkeypatch.setattr(voice_api, "is_stt_available", lambda: True)
        monkeypatch.setattr(voice_api, "get_engine", lambda: _Engine())
        result = await voice_api.transcribe(
            user=user,
            audio=UploadFile(filename="voice.wav", file=io.BytesIO(b"x")),
            language="ko",
            project_id=str(own.id),
            db=session,
        )
        assert result["text"] == "same org"
        assert audit_projects == [own.id]
