"""PR-38 — user management API + GUI scaffolding.

The 13th-round audit ("팀 운영 시스템") named user CRUD, profile
self-service, role-change, and soft-delete as the four Critical
gaps from the RBAC area (A1, A4, A5, A7). This PR ships all four.

These tests pin the static surface (model + migration + router +
template). The async DB-touching behaviour lives in the
integration suite under ``test_integration_users.py`` (CI only).
"""

from __future__ import annotations

from pathlib import Path



_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_TPL = _APP / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# User model — profile + lifecycle columns
# ---------------------------------------------------------------------------


def test_user_model_has_profile_columns():
    body = _read(_APP / "models" / "auth.py")
    for col in (
        "email",
        "display_name",
        "avatar_url",
        "timezone",
        "disabled_at",
        "last_login_at",
        "updated_at",
    ):
        assert col in body, f"User model missing {col!r} column"


def test_user_model_disabled_at_is_nullable():
    """Disabled is the soft-delete flag; an active user has it NULL.
    Making the column NOT NULL would force every user row to look
    "disabled-at-some-time" which is the wrong semantics.
    """
    from app.models.auth import User

    col = User.__table__.c.disabled_at
    assert col.nullable is True


def test_user_model_email_index_unique():
    """Email uniqueness is enforced by the DB index, not by the
    application. Without the unique index two ``POST /users`` calls
    with the same email could race past the pre-flight check."""
    body = _read(
        _SERVER / "alembic" / "versions" / "0017_user_profile_columns.py"
    )
    assert "ix_users_email" in body
    assert "unique=True" in body


# ---------------------------------------------------------------------------
# users.py — endpoint surface
# ---------------------------------------------------------------------------


def test_users_router_exposes_required_endpoints():
    body = _read(_APP / "api" / "users.py")
    for marker in (
        '@router.get("/api/v1/auth/me")',
        '@router.patch("/api/v1/auth/me")',
        '@router.post("/api/v1/auth/me/password"',
        '@router.get("/api/v1/users")',
        '@router.post("/api/v1/users"',
        '@router.patch("/api/v1/users/{user_id}/role")',
        '@router.delete("/api/v1/users/{user_id}"',
        '@router.post("/api/v1/users/{user_id}/enable")',
    ):
        assert marker in body, f"users router missing {marker!r}"


def test_admin_endpoints_require_admin():
    """The admin-only paths must depend on ``require_admin``, not on
    the generic ``CurrentUser`` (which would let an operator promote
    themselves). Inspect the source rather than the signature so
    Pydantic's deferred-validation chain doesn't trigger."""
    body = _read(_APP / "api" / "users.py")
    for fn in ("list_users", "create_user", "change_role", "disable_user", "enable_user"):
        # Find the function definition and ensure ``require_admin``
        # appears inside its parameter list.
        idx = body.find(f"async def {fn}(")
        assert idx > 0, f"{fn} not found"
        # Slice from the def line to the closing ``):``
        end = body.find(") ->", idx)
        slab = body[idx:end] if end > idx else body[idx : idx + 400]
        assert "require_admin" in slab, (
            f"{fn} must depend on require_admin (found in slab: {slab!r})"
        )


def test_self_service_endpoints_use_current_user():
    """The profile / password endpoints must use the generic
    authenticated dependency — any role can update their own
    profile, not just admins."""
    body = _read(_APP / "api" / "users.py")
    for fn in ("me", "update_me", "change_my_password"):
        idx = body.find(f"async def {fn}(")
        assert idx > 0, f"{fn} not found"
        end = body.find(") ->", idx)
        if end < idx:
            end = idx + 400
        slab = body[idx:end]
        assert "CurrentUser" in slab, (
            f"{fn} must depend on CurrentUser (slab: {slab!r})"
        )


def test_role_change_refuses_self_demotion():
    """Without this guard, the last admin in an org could demote
    themselves and lock the org out of admin actions."""
    body = _read(_APP / "api" / "users.py")
    assert "cannot_demote_self" in body
    assert 'target.id == actor.id and body.role != "admin"' in body


def test_disable_refuses_self():
    body = _read(_APP / "api" / "users.py")
    assert "cannot_disable_self" in body


def test_create_user_returns_409_on_duplicate():
    body = _read(_APP / "api" / "users.py")
    # Both the username and email collision paths return 409 with a
    # clear ``detail`` so the GUI can show a translated toast.
    assert "username_taken" in body
    assert "email_taken" in body


def test_password_change_validates_current_password():
    """Defence against session hijack — a stolen cookie can't
    rotate the password without knowing the current one."""
    body = _read(_APP / "api" / "users.py")
    assert "current_password_incorrect" in body


def test_pydantic_schemas_enforce_minimum_password_length():
    """12-char minimum on the password fields. Source-level check
    so we don't import the schema module (which would force
    EmailStr's optional dependency)."""
    body = _read(_APP / "api" / "users.py")
    # Both schemas declare min_length=12.
    pwd_count = body.count("min_length=12")
    assert pwd_count >= 2, (
        f"expected ≥ 2 ``min_length=12`` (UserCreate.password and "
        f"PasswordChange.new_password), got {pwd_count}"
    )


def test_role_whitelist_is_explicit():
    """The role validator must reject anything outside
    {viewer, operator, admin} — otherwise a typo in a future
    analyzer could grant ``superadmin`` access."""
    body = _read(_APP / "api" / "users.py")
    assert 'VALID_ROLES = {"viewer", "operator", "admin"}' in body
    # The validator must consult the whitelist; either ``not in
    # VALID_ROLES`` or an explicit comparison is acceptable.
    assert "VALID_ROLES" in body
    assert "_check_role" in body


# ---------------------------------------------------------------------------
# Audit log — every state-changing user action is recorded
# ---------------------------------------------------------------------------


def test_user_state_changes_are_audited():
    """The action strings the audit-tab filter depends on."""
    body = _read(_APP / "api" / "users.py")
    for action in (
        "user.profile_updated",
        "user.password_changed_self",
        "user.created",
        "user.role_changed",
        "user.disabled",
        "user.enabled",
    ):
        assert action in body, f"audit action {action!r} missing"


# ---------------------------------------------------------------------------
# Login flow — disabled accounts refused + last_login_at bumped
# ---------------------------------------------------------------------------


def test_login_refuses_disabled_account():
    body = _read(_APP / "api" / "auth.py")
    assert "disabled_at is not None" in body
    # The audit log distinguishes "wrong password" from "blocked
    # because disabled" so a reviewer can see ex-employees still
    # poking at the door — but the response is the same so an
    # outside attacker can't enumerate disabled accounts.
    assert "auth.login_blocked_disabled" in body


def test_login_records_last_login_at():
    body = _read(_APP / "api" / "auth.py")
    assert "user.last_login_at = datetime" in body


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def test_users_router_included_in_main():
    body = _read(_APP / "main.py")
    assert "users_api" in body
    assert "app.include_router(users_api.router)" in body


def test_dashboard_router_accepts_users_and_profile_tabs():
    body = _read(_APP / "dashboard" / "router.py")
    assert '"users"' in body
    assert '"profile"' in body


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def test_profile_template_exists_and_is_translatable():
    body = _read(_TPL / "profile.html")
    assert 'data-i18n="Profile"' in body
    assert 'data-i18n="Account"' in body
    # The change-password form must post to the canonical endpoint.
    assert "/api/v1/auth/me/password" in body


def test_users_template_exists_and_is_admin_gated():
    body = _read(_TPL / "users.html")
    # Non-admins see only the gate, not the form.
    assert "{% if user.role != 'admin' %}" in body
    # Admins see add-user form + members table.
    assert 'data-i18n="Add user"' in body
    assert 'data-i18n="Members"' in body
    # Role selector + disable button reference the right endpoints.
    assert "/api/v1/users/${userId}/role" in body
    assert "/api/v1/users/${userId}/enable" in body


def test_sidebar_nav_links_profile_and_users():
    body = _read(_TPL / "_layout.html")
    assert 'href="/profile"' in body
    assert 'href="/users"' in body
    # Users tab lives in the admin-only section.
    admin_idx = body.find("{% if user.role == 'admin' %}")
    users_idx = body.find('href="/users"')
    assert 0 < admin_idx < users_idx


def test_phrase_book_has_korean_for_user_management_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in (
        "프로필",
        "유저",
        "표시 이름",
        "임시 비밀번호",
        "유저 추가",
        "구성원",
        "비활성 포함",
        "비활성화",
        "다시 활성화",
        "마지막 로그인",
    ):
        assert kr in body, f"phrase book missing Korean translation {kr!r}"
