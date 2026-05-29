"""PR-45 — 14th-round audit hot-fix bundle.

Four findings, one PR:

* **A7 / C1 Critical**: logout form was form-encoded POST without
  the CSRF token; ``/api/v1/auth/logout`` plus the dashboard's
  ``/logout`` were both missing from the CSRF exempt list, so
  every user had been unable to sign out since PR-44 landed.
* **A2 / C5 Critical**: ``brute_force.is_locked`` /
  ``record_failure`` / ``clear`` raised straight through a Redis
  outage, which would 500 every login attempt during a Redis
  blip. They now catch and fail-open.
* **A6 / C4 Critical**: ``mountCommentThread`` was exposed on
  ``MnemosUI`` (PR-43) but no template called it. diffs.html
  and plans.html now mount a thread under every submission /
  plan card.
* **A8 Major**: invite + password-reset endpoints (PR-44) had no
  GUI. Three new templates (``forgot.html`` / ``reset.html`` /
  ``invite.html``), a "Forgot password?" link on login, and an
  invite-by-token section on the Users admin tab.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_TPL = _APP / "dashboard" / "templates"
_STATIC = _APP / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Logout CSRF — the 14th-round audit's most user-visible critical
# ---------------------------------------------------------------------------


def test_csrf_exempts_logout_paths():
    body = _read(_APP / "security" / "csrf.py")
    # Both the JSON endpoint and the dashboard form post.
    assert '"/api/v1/auth/logout"' in body
    assert '"/logout"' in body


def test_csrf_exempts_anonymous_token_flows():
    """Invite-accept + reset-request / reset-consume are anonymous
    by design — the token IS the auth. Requiring a CSRF token
    would defeat the whole flow."""
    body = _read(_APP / "security" / "csrf.py")
    assert '"/api/v1/auth/reset/"' in body
    assert '"/api/v1/invites/accept"' in body
    # The dashboard pages that drive those flows are also exempt
    # because they're GET-rendered and then POST anonymously.
    assert '"/login"' in body


# ---------------------------------------------------------------------------
# Brute-force Redis fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_locked_fails_open_when_redis_is_down():
    from app.auth import brute_force

    async def _broken():
        raise RuntimeError("redis pool exhausted")

    with patch("app.auth.brute_force.get_redis", new=_broken):
        # A Redis outage must NOT lock everyone out of the platform.
        assert await brute_force.is_locked("alice") is False


@pytest.mark.asyncio
async def test_record_failure_returns_zero_on_redis_error():
    """The login handler reads ``record_failure``'s return value
    to decide between ``login_failed`` and ``login_blocked_lockout``
    audit actions. A Redis failure must return a value that lets
    the regular failure path proceed (i.e. not >= the cap)."""
    from app.auth import brute_force

    async def _broken():
        raise RuntimeError("network unreachable")

    with patch("app.auth.brute_force.get_redis", new=_broken):
        count = await brute_force.record_failure("alice")
    assert count == 0


@pytest.mark.asyncio
async def test_clear_silent_on_redis_error():
    from app.auth import brute_force

    async def _broken():
        raise RuntimeError("connection reset")

    with patch("app.auth.brute_force.get_redis", new=_broken):
        # Must not raise.
        await brute_force.clear("alice")


@pytest.mark.asyncio
async def test_happy_path_still_uses_redis():
    """Sanity check: with a working Redis the counter logic is
    unchanged — the fail-open is *only* an except branch."""
    from app.auth import brute_force

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(return_value=1)
    fake_redis.expire = AsyncMock()
    fake_redis.get = AsyncMock(return_value=b"3")
    fake_redis.delete = AsyncMock()

    async def _ok():
        return fake_redis

    with patch("app.auth.brute_force.get_redis", new=_ok):
        assert await brute_force.is_locked("alice") is False
        assert await brute_force.record_failure("alice") == 1
        await brute_force.clear("alice")

    fake_redis.delete.assert_awaited_once_with("mnemos:login_fail:alice")


# ---------------------------------------------------------------------------
# Comments UI mount — diffs.html + plans.html
# ---------------------------------------------------------------------------


def test_diffs_template_mounts_comment_thread():
    body = _read(_TPL / "diffs.html")
    # The submission card carries the marker the helper looks for.
    assert "data-comment-thread" in body
    assert 'data-mount="diff_submission:' in body
    # The submit handler triggers the mount pass.
    assert "mountSubmissionComments" in body
    assert "MnemosUI.mountCommentThread" in body


def test_plans_template_mounts_comment_thread():
    body = _read(_TPL / "plans.html")
    assert "data-comment-thread" in body
    assert 'data-mount="plan:' in body
    assert "MnemosUI.mountCommentThread" in body


def test_mount_walks_only_unmounted_nodes():
    """A re-render must not mount the same thread twice — the
    helper keys off ``_mnemosMounted``."""
    body = _read(_TPL / "diffs.html")
    assert "el._mnemosMounted" in body


# ---------------------------------------------------------------------------
# Invite / reset UI templates
# ---------------------------------------------------------------------------


def test_forgot_template_present():
    body = _read(_TPL / "forgot.html")
    assert 'data-i18n="Forgot password"' in body
    assert '/api/v1/auth/reset/request' in body
    # The token is surfaced inline if the username matched so an
    # admin without SMTP can still hand-deliver it.
    assert "token" in body


def test_reset_template_reads_token_from_fragment():
    """The reset URL carries the token in the URL *fragment* so
    it never reaches the server access log."""
    body = _read(_TPL / "reset.html")
    assert "window.location.hash" in body
    assert "/api/v1/auth/reset/consume" in body
    # Password confirmation field present.
    assert "r-confirm" in body


def test_invite_template_reads_token_from_fragment():
    body = _read(_TPL / "invite.html")
    assert "window.location.hash" in body
    assert "/api/v1/invites/accept" in body
    # Username regex matches the API constraint.
    assert "[a-zA-Z0-9_.\\-]{2,64}" in body


def test_login_template_has_forgot_link():
    body = _read(_TPL / "login.html")
    assert 'href="/forgot"' in body


def test_users_template_has_invite_by_token_section():
    body = _read(_TPL / "users.html")
    assert 'data-i18n="Or invite by token"' in body
    assert "/api/v1/invites" in body
    assert "createInvite" in body


def test_users_invite_link_uses_fragment():
    """The generated invite URL must use ``#token=…`` not
    ``?token=…`` — same threat model as the reset link."""
    body = _read(_TPL / "users.html")
    assert "/invite#token=" in body


def test_dashboard_router_serves_new_pages():
    body = _read(_APP / "dashboard" / "router.py")
    for route in ("/forgot", "/reset", "/invite"):
        assert f'@router.get("{route}"' in body


def test_phrase_book_covers_new_flow():
    body = _read(_STATIC / "ui.js")
    for kr in (
        "비밀번호 잊으셨나요?",
        "비밀번호 재설정",
        "재설정 요청",
        "초대 수락",
        "계정 만들기",
        "또는 토큰으로 초대",
        "초대 링크 생성",
        "링크 복사",
    ):
        assert kr in body, f"phrase book missing {kr!r}"
