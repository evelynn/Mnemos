"""Integration tests — auth + RBAC boundary.

Confirms that the middleware chain, session cookie flow, and role
checks all line up end-to-end.
"""

import uuid

import pytest

pytestmark = pytest.mark.integration


async def _login(client, username: str, password: str):
    return await client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )


async def test_me_requires_session(http_client):
    r = await http_client.get("/api/v1/auth/me")
    assert r.status_code == 401
    body = r.json()
    assert body["status"] == "error"
    assert "request_id" in body


async def test_admin_can_list_secrets(http_client, admin_user, db_session):
    # Commit the savepoint-nested user so the running app's session sees it.
    await db_session.commit()
    r = await _login(http_client, admin_user.username, "pw-test-123456")
    assert r.status_code == 200
    r = await http_client.get("/api/v1/secrets")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


async def test_viewer_blocked_from_admin_endpoint(
    http_client, db_session
):
    from app.auth.passwords import hash_password
    from app.models.auth import User

    username = f"viewer-{uuid.uuid4().hex[:8]}"
    db_session.add(
        User(username=username, password_hash=hash_password("pw-viewer-abcd12"), role="viewer")
    )
    await db_session.commit()

    r = await _login(http_client, username, "pw-viewer-abcd12")
    assert r.status_code == 200
    # list secrets is open to any authed user, creating is admin-only.
    r = await http_client.post(
        "/api/v1/secrets",
        json={"label": "x", "kind": "other", "value": "v"},
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "insufficient_role"
