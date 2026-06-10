"""Cross-org boundary checks (integration).

Verifies that the Phase C-1b retrofit actually keeps organisations
isolated end-to-end. Marked ``integration`` so it only runs in CI where
Postgres + Redis are live.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration


async def _make_user(db_session, role: str, org_id):
    from app.auth.passwords import hash_password
    from app.models.auth import User

    user = User(
        username=f"{role}-{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("pw-test-bound-1234"),
        role=role,
        organization_id=org_id,
    )
    db_session.add(user)
    await db_session.flush()
    return user


async def _make_org(db_session, slug: str):
    from app.models.organization import Organization

    org = Organization(slug=slug, display_name=slug.title())
    db_session.add(org)
    await db_session.flush()
    return org


async def _login(client, username: str, password: str):
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def test_cross_org_project_returns_404(http_client, db_session):
    org_a = await _make_org(db_session, f"orga-{uuid.uuid4().hex[:6]}")
    org_b = await _make_org(db_session, f"orgb-{uuid.uuid4().hex[:6]}")

    # A project lives in org B.
    from app.models.projects import Project

    project_b = Project(
        name="proj-b",
        gitlab_project_id=42,
        gitlab_url="https://gitlab.example.com/proj-b",
        languages=["csharp"],
        organization_id=org_b.id,
    )
    db_session.add(project_b)

    # An operator in org A.
    user_a = await _make_user(db_session, "operator", org_a.id)
    await db_session.commit()

    r = await _login(http_client, user_a.username, "pw-test-bound-1234")
    assert r.status_code == 200

    # Reading the cross-org project must look identical to a missing
    # project so the boundary doesn't leak existence.
    r = await http_client.get(f"/api/v1/projects/{project_b.id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "not_found"

    # The list endpoint must also exclude the cross-org row.
    r = await http_client.get("/api/v1/projects")
    assert r.status_code == 200
    project_ids = {p["id"] for p in r.json()}
    assert str(project_b.id) not in project_ids


async def test_cross_org_audit_entries_excluded(http_client, db_session):
    org_a = await _make_org(db_session, f"orga-{uuid.uuid4().hex[:6]}")
    org_b = await _make_org(db_session, f"orgb-{uuid.uuid4().hex[:6]}")

    from app.models.audit import AuditLog
    from app.models.projects import Project

    project_b = Project(
        name="proj-b",
        gitlab_project_id=43,
        gitlab_url="https://gitlab.example.com/proj-b2",
        languages=["csharp"],
        organization_id=org_b.id,
    )
    db_session.add(project_b)
    await db_session.flush()

    db_session.add(
        AuditLog(
            actor="user:other-org-user",
            action="diff.approve",
            target="some-id",
            project_id=project_b.id,
        )
    )

    user_a = await _make_user(db_session, "admin", org_a.id)
    await db_session.commit()

    r = await _login(http_client, user_a.username, "pw-test-bound-1234")
    assert r.status_code == 200

    # The audit list must not include the entry tied to org B's project.
    r = await http_client.get(
        "/api/v1/audit", params={"actor": "user:other-org-user"}
    )
    assert r.status_code == 200
    assert r.json() == []


async def test_cross_org_analysis_run_is_404_and_uncancellable(http_client, db_session):
    """An analysis run addressed by run_id must stay org-scoped.

    Regression: the run read/stages/cancel endpoints key off run_id (not
    project_id) so the require_project_org dependency couldn't gate them;
    until require_run_org was added, an operator in org A could read AND
    cancel org B's running analyses just by knowing the run UUID.
    """
    org_a = await _make_org(db_session, f"orga-{uuid.uuid4().hex[:6]}")
    org_b = await _make_org(db_session, f"orgb-{uuid.uuid4().hex[:6]}")

    from app.models.graph import AnalysisRun
    from app.models.projects import Project

    project_b = Project(
        name="proj-b",
        gitlab_project_id=44,
        gitlab_url="https://gitlab.example.com/proj-b3",
        languages=["csharp"],
        organization_id=org_b.id,
    )
    db_session.add(project_b)
    await db_session.flush()

    run_b = AnalysisRun(
        project_id=project_b.id,
        status="running",
        triggered_by="user:owner-b",
        git_sha="HEAD",
        scope="full",
    )
    db_session.add(run_b)
    await db_session.flush()

    user_a = await _make_user(db_session, "operator", org_a.id)
    await db_session.commit()

    r = await _login(http_client, user_a.username, "pw-test-bound-1234")
    assert r.status_code == 200

    # Read, stages, and SSE all 404 across the boundary (never leak the run).
    assert (await http_client.get(f"/api/v1/analysis_runs/{run_b.id}")).status_code == 404
    assert (
        await http_client.get(f"/api/v1/analysis_runs/{run_b.id}/stages")
    ).status_code == 404

    # Cancel must not mutate org B's run.
    r = await http_client.post(f"/api/v1/analysis_runs/{run_b.id}/cancel")
    assert r.status_code == 404
    await db_session.refresh(run_b)
    assert run_b.status == "running"
