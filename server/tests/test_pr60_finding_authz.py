"""PR-60 — close authorization gaps on finding + comment mutation.

A critical-defect reassessment found ``patch_finding`` shipped with
no authorization at all: keyed off ``finding_id`` (not ``project_id``)
it never received the ``require_project_org`` retrofit, so any
authenticated user — including a viewer in another org — could
resolve / false-positive any finding by UUID. ``edit_comment`` /
``delete_comment`` likewise skipped the org check on the admin path.
"""

from __future__ import annotations

from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "app" / "api"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# patch_finding — role + org gating
# ---------------------------------------------------------------------------


def test_patch_finding_requires_operator_role():
    """A viewer must not be able to mutate finding status."""
    body = _read(_API / "findings.py")
    idx = body.find("def patch_finding(")
    decorator = body[max(0, idx - 220):idx]
    assert "require_operator" in decorator


def test_patch_finding_checks_org():
    """The route keys off finding_id, so it resolves the finding's
    org by hand and 404s on a cross-org id."""
    body = _read(_API / "findings.py")
    idx = body.find("async def patch_finding(")
    slab = body[idx:idx + 1100]
    assert "resolve_project_org(db, f.project_id)" in slab
    assert "same_org(user, org_id)" in slab
    # 404 (not 403) so existence doesn't leak across tenants.
    assert 'status_code=404, detail="not_found"' in slab


def test_findings_imports_org_helpers():
    body = _read(_API / "findings.py")
    assert "resolve_project_org" in body
    assert "same_org" in body
    assert "from app.auth.rbac import require_operator" in body


# ---------------------------------------------------------------------------
# comment edit / delete — org check on the admin path
# ---------------------------------------------------------------------------


def test_edit_comment_checks_target_org():
    body = _read(_API / "comments.py")
    idx = body.find("async def edit_comment(")
    slab = body[idx:idx + 700]
    assert "_check_target_in_user_org(db, c.target_kind, c.target_id, user)" in slab


def test_delete_comment_checks_target_org():
    body = _read(_API / "comments.py")
    idx = body.find("async def delete_comment(")
    slab = body[idx:idx + 700]
    assert "_check_target_in_user_org(db, c.target_kind, c.target_id, user)" in slab
