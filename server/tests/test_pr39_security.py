"""PR-39 — security hardening: brute-force lockout, password
policy, security response headers.

Closes the 13th-round audit's Critical E3 (brute-force) and the
Major E5/E6 (password policy + HSTS/CSP headers). Each test pins
the contract the operator's threat model depends on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.passwords import (
    MIN_LENGTH,
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
)


_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# E5 — password policy
# ---------------------------------------------------------------------------


def test_min_length_is_twelve():
    """Sub-12-char passwords are the easiest to brute-force; the
    13th-round audit picked 12 as the platform-wide floor.
    """
    assert MIN_LENGTH == 12


def test_policy_rejects_short_passwords():
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password_policy("short1")
    assert exc.value.code == "password_too_short"


def test_policy_rejects_letter_only():
    """A 12-char alphabetic string is a passphrase, but the policy
    asks for one digit too. Stops 'passwordpassw' from sneaking
    through."""
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password_policy("abcdefghijkl")
    assert exc.value.code == "password_missing_letter_or_digit"


def test_policy_rejects_digit_only():
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password_policy("123456789012")
    assert exc.value.code == "password_missing_letter_or_digit"


def test_policy_rejects_known_weak_passwords():
    """The 13th-round audit's E5 said the most-common 'pass1234'
    style strings should be refused even when they satisfy the
    length + letter + digit gate."""
    with pytest.raises(PasswordPolicyError) as exc:
        validate_password_policy("password1234")
    assert exc.value.code == "password_too_common"


def test_policy_accepts_strong_password():
    # Strong = 12+ chars, mixed letters + digits, not in the weak list.
    validate_password_policy("StrongPass12")


def test_hash_password_runs_policy_first():
    """Every entry point (CLI, REST, change-password) goes through
    ``hash_password`` so the policy is enforced uniformly. The
    function raises on a weak input *before* even touching bcrypt.
    Source-level check guards the call order; the unit-test bcrypt
    backend is brittle to env (version detection), so we don't
    actually hash a password here.
    """
    with pytest.raises(PasswordPolicyError):
        hash_password("short")
    body = _read(_APP / "auth" / "passwords.py")
    policy_idx = body.find("validate_password_policy(plain)")
    # PR-135 — hashing now calls bcrypt.hashpw directly (passlib's
    # CryptContext was dropped; its bcrypt backend self-test breaks
    # on bcrypt ≥ 4.1). Policy must still run before the hash call.
    bcrypt_idx = body.find("bcrypt.hashpw(")
    assert 0 < policy_idx < bcrypt_idx


def test_users_api_translates_policy_error_to_400():
    body = _read(_APP / "api" / "users.py")
    # The handler must catch PasswordPolicyError and turn it into a
    # clear 400 with the policy's ``code`` as the detail.
    assert "except PasswordPolicyError as exc" in body
    assert "HTTPException" in body
    assert "exc.code" in body


# ---------------------------------------------------------------------------
# E3 — brute-force lockout
# ---------------------------------------------------------------------------


def test_brute_force_constants_match_audit_spec():
    """5 failures inside 15 minutes — the 13th-round audit pinned
    these numbers as the team-operations sweet spot."""
    from app.auth.brute_force import (
        LOCKOUT_WINDOW_SEC,
        MAX_FAILURES_PER_WINDOW,
    )

    assert MAX_FAILURES_PER_WINDOW == 5
    assert LOCKOUT_WINDOW_SEC == 15 * 60


def test_brute_force_key_is_per_username_lowercase():
    """A script trying a hundred usernames mustn't collide on a
    single key (which would lock everyone out at once). And the
    key is case-normalised so 'Admin' and 'admin' aren't separate
    counters."""
    from app.auth.brute_force import _key

    assert _key("Alice") == "mnemos:login_fail:alice"
    assert _key("  ALICE  ") == "mnemos:login_fail:alice"


@pytest.mark.asyncio
async def test_record_failure_sets_ttl_on_first_failure_only():
    """The TTL must NOT be refreshed on every failed attempt —
    otherwise an attacker who guesses one password every 14 minutes
    keeps the counter alive forever without ever hitting the cap.
    """
    from app.auth import brute_force

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(side_effect=[1, 2, 3])
    fake_redis.expire = AsyncMock()
    fake_redis.delete = AsyncMock()
    fake_redis.get = AsyncMock(return_value=None)

    async def _get():
        return fake_redis

    with patch("app.auth.brute_force.get_redis", new=_get):
        await brute_force.record_failure("alice")
        await brute_force.record_failure("alice")
        await brute_force.record_failure("alice")

    # ``expire`` called exactly once — on the first INCR.
    assert fake_redis.expire.call_count == 1


@pytest.mark.asyncio
async def test_is_locked_returns_true_at_cap():
    """5 failures = locked. 4 = not yet."""
    from app.auth import brute_force

    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(side_effect=[b"4", b"5"])

    async def _get():
        return fake_redis

    with patch("app.auth.brute_force.get_redis", new=_get):
        not_locked = await brute_force.is_locked("alice")
        locked = await brute_force.is_locked("alice")

    assert not_locked is False
    assert locked is True


@pytest.mark.asyncio
async def test_clear_deletes_the_counter():
    from app.auth import brute_force

    fake_redis = MagicMock()
    fake_redis.delete = AsyncMock()

    async def _get():
        return fake_redis

    with patch("app.auth.brute_force.get_redis", new=_get):
        await brute_force.clear("alice")

    fake_redis.delete.assert_awaited_once_with("mnemos:login_fail:alice")


def test_login_handler_checks_lockout_before_password_verify():
    """Two reasons the lockout check goes FIRST:

    1. Bcrypt verify is intentionally slow (~100ms); checking it
       under lockout would let an attacker time-side-channel valid
       usernames.
    2. A locked-out attacker shouldn't get a chance to learn that
       their guesses are still being processed.
    """
    body = _read(_APP / "api" / "auth.py")
    lock_check = body.find("brute_force.is_locked(")
    verify_call = body.find("verify_password(")
    assert 0 < lock_check < verify_call


def test_login_handler_records_failure_and_clears_on_success():
    body = _read(_APP / "api" / "auth.py")
    assert "brute_force.record_failure(body.username)" in body
    assert "brute_force.clear(body.username)" in body


def test_login_blocked_lockout_audited():
    """Audit log must distinguish lockout from a plain wrong
    password so an admin can see attack patterns vs typos."""
    body = _read(_APP / "api" / "auth.py")
    assert "auth.login_blocked_lockout" in body
    # 429 — the canonical "too many" status.
    assert "HTTP_429_TOO_MANY_REQUESTS" in body


# ---------------------------------------------------------------------------
# E6 — security response headers
# ---------------------------------------------------------------------------


def test_security_headers_middleware_exists():
    body = _read(_APP / "security" / "headers.py")
    assert "class SecurityHeadersMiddleware" in body


def test_security_headers_always_set():
    """X-Content-Type-Options / X-Frame-Options / Referrer-Policy /
    Permissions-Policy must land on every response, dev or prod."""
    body = _read(_APP / "security" / "headers.py")
    for header in (
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
        "Content-Security-Policy",
    ):
        assert header in body, f"middleware missing {header!r}"


def test_csp_default_blocks_third_party_iframes_and_forms():
    body = _read(_APP / "security" / "headers.py")
    assert "frame-ancestors 'none'" in body
    assert "form-action 'self'" in body
    # No third-party script origins: ui.js, mermaid, and exceljs are all
    # self-hosted under /static, so the default CSP stays 'self'-only and
    # the platform runs fully air-gapped (the unused htmx CDN was dropped).
    assert "https://unpkg.com" not in body
    assert "script-src 'self' 'unsafe-inline';" in body


def test_hsts_is_opt_in_via_env():
    """A local-dev HTTP deployment must not get a sticky HSTS flag.
    HSTS only fires when MNEMOS_HSTS_ENABLE=true."""
    body = _read(_APP / "security" / "headers.py")
    assert "MNEMOS_HSTS_ENABLE" in body
    assert "Strict-Transport-Security" in body


def test_middleware_registered_in_main():
    body = _read(_APP / "main.py")
    assert "from app.security.headers import SecurityHeadersMiddleware" in body
    assert "app.add_middleware(SecurityHeadersMiddleware)" in body


@pytest.mark.asyncio
async def test_middleware_adds_default_headers_to_response():
    """End-to-end: call the middleware with a fake response and
    confirm the always-on headers land."""
    from starlette.responses import Response

    from app.security.headers import SecurityHeadersMiddleware

    async def fake_call_next(request):
        return Response("ok", status_code=200)

    mw = SecurityHeadersMiddleware(app=None)  # type: ignore[arg-type]
    request = MagicMock()
    resp = await mw.dispatch(request, fake_call_next)
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["X-Frame-Options"] == "DENY"
    assert resp.headers["Referrer-Policy"] == "same-origin"
    assert "camera=()" in resp.headers["Permissions-Policy"]
    # PR-155 — microphone is self-allowed for the Ask tab voice feature,
    # but camera/geolocation/etc stay hard-denied.
    assert "microphone=(self)" in resp.headers["Permissions-Policy"]
    assert "geolocation=()" in resp.headers["Permissions-Policy"]
    # Default CSP applied.
    assert "default-src 'self'" in resp.headers["Content-Security-Policy"]
    # HSTS NOT set in default-dev mode.
    assert "Strict-Transport-Security" not in resp.headers


@pytest.mark.asyncio
async def test_middleware_emits_hsts_when_opted_in(monkeypatch):
    from starlette.responses import Response

    from app.security.headers import SecurityHeadersMiddleware

    monkeypatch.setenv("MNEMOS_HSTS_ENABLE", "true")

    async def fake_call_next(request):
        return Response("ok", status_code=200)

    mw = SecurityHeadersMiddleware(app=None)  # type: ignore[arg-type]
    resp = await mw.dispatch(MagicMock(), fake_call_next)
    assert "Strict-Transport-Security" in resp.headers
    assert "max-age=" in resp.headers["Strict-Transport-Security"]
