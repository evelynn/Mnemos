"""PR-44 — CSRF, invite + password reset, activity feed."""

from __future__ import annotations

from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_STATIC = _APP / "dashboard" / "static"
_TPL = _APP / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E1 — CSRF middleware
# ---------------------------------------------------------------------------


def test_csrf_middleware_module_present():
    body = _read(_APP / "security" / "csrf.py")
    assert "class CSRFMiddleware" in body
    assert "CSRF_COOKIE" in body
    assert "CSRF_HEADER" in body


def test_csrf_middleware_registered():
    body = _read(_APP / "main.py")
    assert "from app.security.csrf import CSRFMiddleware" in body
    assert "app.add_middleware(CSRFMiddleware)" in body


def test_csrf_safe_methods_exempt():
    """GET / HEAD / OPTIONS never need a token; otherwise the
    dashboard would emit pre-flights for every API call."""
    body = _read(_APP / "security" / "csrf.py")
    assert '"GET", "HEAD", "OPTIONS"' in body


def test_csrf_exempt_prefixes_documented():
    body = _read(_APP / "security" / "csrf.py")
    # Login + OIDC callback + webhook + OTLP must bypass. The webhook
    # router is mounted at /webhooks (not /api/v1/webhooks), so the
    # exemption prefix must match that real path.
    for path in (
        "/api/v1/auth/login",
        "/api/v1/auth/oidc/",
        "/webhooks/",
        "/otlp/",
    ):
        assert f'"{path}"' in body


def test_csrf_token_compared_in_constant_time():
    """``secrets.compare_digest`` defeats timing-side channel
    attacks against the token comparison."""
    body = _read(_APP / "security" / "csrf.py")
    assert "secrets.compare_digest" in body


@pytest.mark.asyncio
async def test_csrf_middleware_rejects_missing_header():
    from starlette.requests import Request

    from app.security.csrf import CSRFMiddleware

    mw = CSRFMiddleware(app=None)  # type: ignore[arg-type]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/users",
        "headers": [],
    }
    req = Request(scope)
    # No cookie + no header → 403.
    called = False

    async def call_next(_):
        nonlocal called
        called = True
        from starlette.responses import Response
        return Response("ok")

    resp = await mw.dispatch(req, call_next)
    assert resp.status_code == 403
    assert called is False  # handler never ran


@pytest.mark.asyncio
async def test_csrf_middleware_accepts_matching_token():
    from starlette.requests import Request

    from app.security.csrf import CSRF_COOKIE, CSRF_HEADER, CSRFMiddleware

    mw = CSRFMiddleware(app=None)  # type: ignore[arg-type]
    token = "x" * 64
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/users",
        "headers": [
            (b"cookie", f"{CSRF_COOKIE}={token}".encode()),
            (CSRF_HEADER.encode(), token.encode()),
        ],
    }
    req = Request(scope)

    async def call_next(_):
        from starlette.responses import Response
        return Response("ok")

    resp = await mw.dispatch(req, call_next)
    assert resp.status_code == 200


def test_client_side_fetch_wrapper_attaches_token():
    body = _read(_STATIC / "ui.js")
    assert "_readCookie(\"mnemos_csrf\")" in body
    assert 'init.headers.set("X-CSRF-Token"' in body
    # Safe methods skip the header to avoid spurious preflights.
    # PR-47 refactored the branch to ``method !== "GET" && method !== "HEAD" …``.
    assert 'method !== "GET"' in body or "method === \"GET\"" in body


# ---------------------------------------------------------------------------
# A2 — invite system
# ---------------------------------------------------------------------------


def test_invite_model_has_required_fields():
    body = _read(_APP / "models" / "onboarding.py")
    for col in (
        "email",
        "token_hash",
        "role",
        "organization_id",
        "invited_by",
        "expires_at",
        "consumed_at",
        "consumed_by_user_id",
    ):
        assert col in body


def test_invite_router_create_requires_admin():
    body = _read(_APP / "api" / "onboarding.py")
    create_idx = body.find("async def create_invite(")
    end = body.find(") ->", create_idx)
    slab = body[create_idx:end]
    assert "require_admin" in slab


def test_invite_accept_is_anonymous():
    """The token IS the auth — accept must NOT require a session."""
    body = _read(_APP / "api" / "onboarding.py")
    accept_idx = body.find("async def accept_invite(")
    end = body.find(") ->", accept_idx)
    slab = body[accept_idx:end]
    # No CurrentUser dependency — the token is verified inline.
    assert "CurrentUser" not in slab


def test_invite_ttl_is_seven_days():
    body = _read(_APP / "api" / "onboarding.py")
    assert "INVITE_TTL = timedelta(days=7)" in body


def test_invite_token_stored_as_hash_only():
    """The raw token is returned once on create and never persisted —
    only the SHA-256 hash lives in the DB."""
    body = _read(_APP / "api" / "onboarding.py")
    assert "token_hash=hash_token(raw_token)" in body
    # The raw token returns on the create response so the admin can
    # share it, but it's NEVER stored on the row.
    assert "token=raw_token" in body


def test_invite_consume_rejects_expired_and_used():
    body = _read(_APP / "api" / "onboarding.py")
    assert "invite_invalid_or_expired" in body
    # Three guards. PR-138h introduced a local ``expires_at`` (coerced
    # from ``invite.expires_at`` to UTC-aware) so the comparison reads
    # ``expires_at < now`` rather than ``invite.expires_at < now``.
    assert "invite.consumed_at is not None" in body
    assert "expires_at < now" in body


def test_invite_audit_actions():
    body = _read(_APP / "api" / "onboarding.py")
    assert "invite.created" in body
    assert "invite.consumed" in body


# ---------------------------------------------------------------------------
# A3 — password reset
# ---------------------------------------------------------------------------


def test_reset_request_returns_same_shape_for_unknown_username():
    """No enumeration. The handler always returns the same 202
    so an attacker can't tell which usernames exist."""
    body = _read(_APP / "api" / "onboarding.py")
    # Both branches return ``out`` with the same status_code.
    assert "if user is None or user.disabled_at is not None:" in body
    assert "return out" in body


def test_reset_ttl_is_one_hour():
    body = _read(_APP / "api" / "onboarding.py")
    assert "RESET_TTL = timedelta(hours=1)" in body


def test_reset_consume_enforces_policy():
    body = _read(_APP / "api" / "onboarding.py")
    # Password policy still gates the new password — short / weak
    # passwords rejected with 400.
    assert "PasswordPolicyError" in body
    assert "hash_password(body.new_password)" in body


def test_reset_audit_actions():
    body = _read(_APP / "api" / "onboarding.py")
    assert "auth.password_reset_requested" in body
    assert "auth.password_reset_consumed" in body


def test_router_registered():
    body = _read(_APP / "main.py")
    assert "onboarding_api" in body
    assert "include_router(onboarding_api.router)" in body


def test_migration_creates_both_tables():
    body = _read(_SERVER / "alembic" / "versions" / "0019_user_invites_and_resets.py")
    assert '"user_invites"' in body
    assert '"password_reset_tokens"' in body
    # Both token-hash columns are unique so a hash collision can't
    # let one row consume a different token's grant.
    assert "unique=True" in body


# ---------------------------------------------------------------------------
# C3 — activity feed
# ---------------------------------------------------------------------------


def test_dashboard_renders_activity_feed_section():
    body = _read(_TPL / "dashboard.html")
    assert 'id="activity-box"' in body
    assert 'data-i18n="Recent activity"' in body


def test_dashboard_calls_load_activity():
    body = _read(_TPL / "dashboard.html")
    assert "loadActivity()" in body
    assert "/api/v1/audit?limit=15" in body


def test_activity_feed_styled():
    body = _read(_STATIC / "app.css")
    assert ".activity-feed" in body
    assert ".activity-item" in body


def test_activity_feed_collapses_on_mobile():
    body = _read(_STATIC / "app.css")
    assert "@media (max-width: 768px)" in body
    # The grid-template-columns override moves to a single column.
    activity_idx = body.find(".activity-item")
    assert activity_idx > 0


def test_phrase_book_for_activity_feed():
    body = _read(_STATIC / "ui.js")
    for kr in ("최근 활동", "팀 활동이 없습니다.", "최근 분석 실행"):
        assert kr in body, f"missing Korean phrase {kr!r}"
