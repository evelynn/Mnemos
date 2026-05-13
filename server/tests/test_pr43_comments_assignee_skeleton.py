"""PR-43 — comments system, assignee fields, loading skeletons."""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_STATIC = _APP / "dashboard" / "static"
_TPL = _APP / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C4 — Comment model + migration + API
# ---------------------------------------------------------------------------


def test_comment_model_polymorphic_target():
    body = _read(_APP / "models" / "comments.py")
    assert "target_kind" in body
    assert "target_id" in body
    assert "VALID_TARGET_KINDS" in body
    # Plan + diff_submission today; extends without a migration.
    assert '"plan"' in body
    assert '"diff_submission"' in body


def test_migration_creates_comments_table_and_index():
    body = _read(_SERVER / "alembic" / "versions" / "0018_comments_and_assignee.py")
    assert 'op.create_table(\n        "comments"' in body
    assert "ix_comments_target" in body
    # Index covers the (kind, id) lookup the list endpoint hits.
    assert '["target_kind", "target_id"]' in body


def test_comments_router_exposes_crud():
    body = _read(_APP / "api" / "comments.py")
    for marker in (
        '@router.get("/api/v1/comments")',
        '@router.post("/api/v1/comments"',
        '@router.patch("/api/v1/comments/{comment_id}")',
        '@router.delete("/api/v1/comments/{comment_id}"',
    ):
        assert marker in body, f"missing route {marker!r}"


def test_comment_endpoints_check_org_isolation():
    """A user from org A must not be able to comment on a plan
    that belongs to org B."""
    body = _read(_APP / "api" / "comments.py")
    assert "_check_target_in_user_org" in body
    assert "organization_id" in body


def test_comment_edit_delete_owner_or_admin_only():
    body = _read(_APP / "api" / "comments.py")
    assert "not_comment_author" in body
    # Admin override path also present.
    assert 'user.role != "admin"' in body


def test_comment_audit_actions_recorded():
    body = _read(_APP / "api" / "comments.py")
    for action in ("comment.created", "comment.edited", "comment.deleted"):
        assert action in body


def test_comments_router_registered():
    body = _read(_APP / "main.py")
    assert "comments_api" in body
    assert "include_router(comments_api.router)" in body


def test_invalid_target_kind_rejected_with_400():
    body = _read(_APP / "api" / "comments.py")
    assert "invalid_target_kind" in body


def test_body_length_capped():
    body = _read(_APP / "api" / "comments.py")
    # Pydantic max_length 4000 — well under any reasonable use,
    # well under a denial-of-service flood.
    assert "max_length=4000" in body


# ---------------------------------------------------------------------------
# C5 — assignee fields
# ---------------------------------------------------------------------------


def test_plan_model_has_assignee_id():
    body = _read(_APP / "models" / "plans.py")
    # Both Plan and DiffSubmission grow the column.
    assert body.count("assignee_id") >= 2


def test_assignee_fk_uses_set_null():
    """A deleted user must NOT cascade-delete plans / diffs they
    were assigned to. The history is more valuable than the FK."""
    body = _read(_SERVER / "alembic" / "versions" / "0018_comments_and_assignee.py")
    assert body.count('ondelete="SET NULL"') >= 2


# ---------------------------------------------------------------------------
# Comment thread mount helper
# ---------------------------------------------------------------------------


def test_ui_js_exposes_mount_comment_thread():
    body = _read(_STATIC / "ui.js")
    assert "mountCommentThread: mountCommentThread" in body


def test_mount_helper_handles_owner_actions():
    body = _read(_STATIC / "ui.js")
    # The author (and admin) sees Edit + Delete actions; everyone
    # else sees the comment without action buttons.
    assert "c.author_id === currentUserId || currentUserRole === \"admin\"" in body
    assert "_mnemosEditComment" in body
    assert "_mnemosDeleteComment" in body


def test_mount_helper_renders_skeleton_while_loading():
    body = _read(_STATIC / "ui.js")
    assert "skeleton skeleton-card" in body


def test_mount_helper_translates_strings():
    body = _read(_STATIC / "ui.js")
    # Comments header + empty state + placeholder + submit button
    # all go through ``MnemosUI.t``.
    assert 't("Comments")' in body
    assert 't("No comments yet.")' in body
    assert 't("Write a comment…")' in body
    assert 't("Post comment")' in body


def test_phrase_book_has_korean_for_comments():
    body = _read(_STATIC / "ui.js")
    for kr in ("댓글", "댓글이 없습니다.", "댓글 작성…", "댓글 게시"):
        assert kr in body, f"phrase book missing {kr!r}"


# ---------------------------------------------------------------------------
# B7 — loading skeletons
# ---------------------------------------------------------------------------


def test_skeleton_styles_defined():
    body = _read(_STATIC / "app.css")
    assert ".skeleton {" in body
    assert "skeleton-shimmer" in body
    # Variants for the common shapes.
    assert ".skeleton-text-lg" in body
    assert ".skeleton-text" in body
    assert ".skeleton-card" in body


def test_skeleton_respects_reduced_motion():
    """Operators who set the OS reduced-motion preference must not
    see the shimmer animation."""
    body = _read(_STATIC / "app.css")
    assert "@media (prefers-reduced-motion: reduce)" in body
    # The reduced-motion rule turns off the animation.
    assert "animation: none" in body


def test_comment_thread_styled():
    body = _read(_STATIC / "app.css")
    assert ".comment-thread" in body
    assert ".comment-meta" in body
    assert ".comment-form" in body
