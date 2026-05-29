"""PR-81 — close the Plan IDOR holes flagged by the critical review.

A platform & UX review found ``decide_plan`` and ``get_plan`` had no
org check and no role gate at all — any authenticated user could
approve, reject, or regenerate any plan in any org with only the
plan UUID, completely bypassing Gate A. ``plan_from_finding`` had
the org check but lacked an operator gate, so a viewer could create
plans (worktree disk + cost).

This PR mirrors the pattern PR-60 used on findings: a shared
``_plan_in_user_org`` helper resolves the plan and 404s on a
cross-org id (so existence doesn't leak across tenants); the two
mutating routes carry ``require_operator``.
"""

from __future__ import annotations

from pathlib import Path

_API = Path(__file__).resolve().parents[1] / "app" / "api"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_plan_endpoints_use_org_helper():
    body = _read(_API / "plans.py")
    assert "_plan_in_user_org" in body
    # The 404 path uses the same helper for both reads and mutations.
    idx_get = body.find("async def get_plan(")
    slab = body[idx_get:idx_get + 400]
    assert "_plan_in_user_org(db, user, plan_id)" in slab


def test_decide_plan_requires_operator_and_org():
    body = _read(_API / "plans.py")
    idx = body.find("async def decide_plan(")
    # decorator + role gate immediately above.
    decorator = body[max(0, idx - 280):idx]
    assert "require_operator" in decorator
    slab = body[idx:idx + 700]
    assert "_plan_in_user_org(db, user, plan_id)" in slab


def test_plan_from_finding_requires_operator():
    body = _read(_API / "plans.py")
    idx = body.find("async def plan_from_finding(")
    decorator = body[max(0, idx - 260):idx]
    assert "require_operator" in decorator


def test_plans_imports_required_helpers():
    body = _read(_API / "plans.py")
    assert "resolve_project_org" in body
    assert "same_org" in body
    assert "from app.auth.rbac import require_operator" in body
