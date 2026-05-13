"""PR-46 — Phase 3 productisation: idle session timeout +
revocation-on-disable + global rate-limit middleware.

The 15th-round audit named three Phase-3 blockers (E4, A6
revocation, E2 global rate-limit) as the only things between the
current state and "production-ready". All three close here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E4 — idle timeout (sliding TTL on every authenticated request)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_session_slides_the_ttl_forward():
    """Every authenticated request must refresh the session's TTL
    so a long-active operator doesn't get logged out at the
    8-hour absolute mark while they're working. The slide uses
    ``expire`` (not ``set``), so the cookie value stays stable."""
    from app.auth import sessions

    fake = MagicMock()
    fake.get = AsyncMock(return_value="11111111-1111-1111-1111-111111111111|2026-05-13T00:00:00+00:00")
    fake.expire = AsyncMock(return_value=True)
    with patch.object(sessions, "_client", return_value=fake):
        result = await sessions.read_session("tok-abc")
    assert result is not None
    fake.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_session_returns_none_when_no_key():
    """Idle past the TTL → Redis returned None → we return None
    instead of raising. The slide must NOT happen on a missing key."""
    from app.auth import sessions

    fake = MagicMock()
    fake.get = AsyncMock(return_value=None)
    fake.expire = AsyncMock(return_value=True)
    with patch.object(sessions, "_client", return_value=fake):
        result = await sessions.read_session("expired")
    assert result is None
    fake.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_slide_silently_swallows_redis_error():
    """A Redis blip during the slide mustn't fail an
    already-authenticated request. The cookie stays usable; the
    next request retries the slide."""
    from app.auth import sessions

    fake = MagicMock()
    fake.get = AsyncMock(return_value="11111111-1111-1111-1111-111111111111|2026-05-13")
    fake.expire = AsyncMock(side_effect=RuntimeError("redis down"))
    with patch.object(sessions, "_client", return_value=fake):
        result = await sessions.read_session("tok-abc")
    # Auth still works.
    assert str(result) == "11111111-1111-1111-1111-111111111111"


# ---------------------------------------------------------------------------
# A6 — session revocation on user disable
# ---------------------------------------------------------------------------


def test_revoke_all_for_user_exists():
    body = _read(_APP / "auth" / "sessions.py")
    assert "async def revoke_all_for_user" in body
    # The audit's specific complaint: cookie outlives the disable
    # action. The implementation must scan the keyspace and
    # delete every matching key.
    assert "scan_iter" in body


def test_disable_user_calls_revoke():
    body = _read(_APP / "api" / "users.py")
    assert "revoke_all_for_user(target.id)" in body
    # The audit-log entry records how many sessions were killed.
    assert "sessions_revoked" in body


@pytest.mark.asyncio
async def test_revoke_all_walks_only_matching_keys():
    """Two sessions for two different users: only the target's
    session is deleted."""
    from app.auth import sessions
    import uuid as _uuid

    target_id = _uuid.UUID("11111111-1111-1111-1111-111111111111")
    other_id = _uuid.UUID("22222222-2222-2222-2222-222222222222")

    fake = MagicMock()
    fake.delete = AsyncMock(return_value=1)

    async def fake_scan_iter(match=None, count=None):
        for k in (
            "mnemos:session:tok-target-1",
            "mnemos:session:tok-other-1",
            "mnemos:session:tok-target-2",
        ):
            yield k

    fake.scan_iter = fake_scan_iter
    payloads = {
        "mnemos:session:tok-target-1": f"{target_id}|...",
        "mnemos:session:tok-other-1": f"{other_id}|...",
        "mnemos:session:tok-target-2": f"{target_id}|...",
    }
    fake.get = AsyncMock(side_effect=lambda k: payloads.get(k))

    with patch.object(sessions, "_client", return_value=fake):
        n = await sessions.revoke_all_for_user(target_id)

    assert n == 2
    assert fake.delete.await_count == 2


# ---------------------------------------------------------------------------
# E2 — global rate-limit middleware
# ---------------------------------------------------------------------------


def test_rate_limit_middleware_module_present():
    body = _read(_APP / "security" / "rate_limit.py")
    assert "class RateLimitMiddleware" in body
    # Group resolution + default cap.
    assert "_GROUPS" in body
    assert "DEFAULT_LIMIT" in body


def test_rate_limit_middleware_registered_in_main():
    body = _read(_APP / "main.py")
    assert "from app.security.rate_limit import RateLimitMiddleware" in body
    assert "app.add_middleware(RateLimitMiddleware)" in body


def test_rate_limit_exempts_login_and_health():
    """Login already has the brute-force counter; health probes
    are scraped at high cadence on purpose."""
    body = _read(_APP / "security" / "rate_limit.py")
    assert '"/api/v1/auth/login"' in body
    assert '"/api/v1/health"' in body
    assert '"/otlp/"' in body
    assert '"/api/v1/webhooks/"' in body


def test_rate_limit_safe_methods_pass_through():
    body = _read(_APP / "security" / "rate_limit.py")
    assert '"GET", "HEAD", "OPTIONS"' in body


def test_rate_limit_per_group_keys():
    """Comments / invites / users / diffs / break_glass each get
    their own counter so a flood of one doesn't starve the others."""
    body = _read(_APP / "security" / "rate_limit.py")
    for group in ("invites", "comments", "users", "diffs", "break_glass"):
        assert f'"{group}"' in body


def test_rate_limit_fails_open_on_redis_error():
    """Same posture as ``brute_force.is_locked`` — better to let
    throttle slip during a Redis outage than to lock everyone out."""
    body = _read(_APP / "security" / "rate_limit.py")
    assert "except Exception" in body
    # The except branch must call_next, not return 429.
    fail_open_idx = body.find("except Exception")
    end = body.find("if count > self.limit:", fail_open_idx)
    slab = body[fail_open_idx:end]
    assert "call_next(request)" in slab


def test_rate_limit_returns_429_with_retry_after():
    body = _read(_APP / "security" / "rate_limit.py")
    assert "status_code=429" in body
    assert "Retry-After" in body
    assert "rate_limit_exceeded" in body


@pytest.mark.asyncio
async def test_rate_limit_middleware_lets_first_request_through():
    """First mutation in a window: counter increments to 1, request
    flows through to the handler."""
    from starlette.responses import Response

    from app.security.rate_limit import RateLimitMiddleware

    mw = RateLimitMiddleware(app=None, limit=5, window_sec=60)  # type: ignore[arg-type]

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(return_value=1)
    fake_redis.expire = AsyncMock()

    async def _fake_get_redis():
        return fake_redis

    handler_called = False

    async def call_next(_):
        nonlocal handler_called
        handler_called = True
        return Response("ok")

    request = MagicMock()
    request.method = "POST"
    request.url.path = "/api/v1/comments"
    request.cookies = {}
    request.client = MagicMock(host="127.0.0.1")

    with patch("app.security.rate_limit.get_redis", new=_fake_get_redis):
        resp = await mw.dispatch(request, call_next)

    assert resp.status_code == 200
    assert handler_called is True
    fake_redis.expire.assert_awaited_once()


@pytest.mark.asyncio
async def test_rate_limit_middleware_returns_429_over_cap():
    from app.security.rate_limit import RateLimitMiddleware

    mw = RateLimitMiddleware(app=None, limit=5, window_sec=60)  # type: ignore[arg-type]

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(return_value=6)  # over the cap
    fake_redis.expire = AsyncMock()

    async def _fake_get_redis():
        return fake_redis

    handler_called = False

    async def call_next(_):
        nonlocal handler_called
        handler_called = True
        from starlette.responses import Response
        return Response("ok")

    request = MagicMock()
    request.method = "POST"
    request.url.path = "/api/v1/comments"
    request.cookies = {}
    request.client = MagicMock(host="127.0.0.1")

    with patch("app.security.rate_limit.get_redis", new=_fake_get_redis):
        resp = await mw.dispatch(request, call_next)

    assert resp.status_code == 429
    assert handler_called is False
    assert resp.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_rate_limit_get_requests_skip_counter():
    from app.security.rate_limit import RateLimitMiddleware

    mw = RateLimitMiddleware(app=None, limit=5, window_sec=60)  # type: ignore[arg-type]

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(return_value=99)
    fake_redis.expire = AsyncMock()

    async def _fake_get_redis():
        return fake_redis

    async def call_next(_):
        from starlette.responses import Response
        return Response("ok")

    request = MagicMock()
    request.method = "GET"
    request.url.path = "/api/v1/comments"
    request.cookies = {}
    request.client = MagicMock(host="127.0.0.1")

    with patch("app.security.rate_limit.get_redis", new=_fake_get_redis):
        resp = await mw.dispatch(request, call_next)

    assert resp.status_code == 200
    # The counter must NOT have been touched.
    fake_redis.incr.assert_not_awaited()
