"""PR-52 — one-click Finding → Plan (core-value B-area).

The value audit's B4 gap: the operator had to manually re-type a
Plan spec from a finding. This PR adds
``POST /api/v1/findings/{id}/plan`` which pre-fills a draft Plan
from the finding's kind / subject / severity / risk score / the
deterministic remediation hint PR-50 attached.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


def test_plan_from_finding_endpoint_defined():
    body = _read(_APP / "api" / "plans.py")
    assert '"/api/v1/findings/{finding_id}/plan"' in body
    assert "async def plan_from_finding(" in body


def test_plan_from_finding_is_org_isolated():
    """A user must not be able to spin a Plan off another tenant's
    finding."""
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plan_from_finding(")
    end = body.find("@router", fn_idx + 1)
    slab = body[fn_idx:end] if end > fn_idx else body[fn_idx:]
    assert "organization_id" in slab
    assert "finding_not_found" in slab


def test_plan_from_finding_prefills_spec_from_remediation():
    """The generated Plan's motivation must carry the finding's
    remediation hint — that's the whole point, no re-typing."""
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plan_from_finding(")
    slab = body[fn_idx:fn_idx + 4500]
    assert "finding.remediation" in slab
    assert "finding.kind" in slab
    assert "finding.risk_score" in slab


def test_plan_from_finding_lands_pending_approval():
    """The draft is NOT auto-approved — the operator still reviews
    it and the ultrareview pipeline still gates the diff. The Plan
    model defaults status to pending_approval; the handler must
    not override it."""
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plan_from_finding(")
    end = body.find("@router", fn_idx + 1)
    slab = body[fn_idx:end] if end > fn_idx else body[fn_idx:]
    # The handler must not set status= to anything approved.
    assert 'status="approved"' not in slab
    assert "status='approved'" not in slab


def test_plan_from_finding_records_source_finding():
    """The Plan's impact_report + worktree_meta both record which
    finding spawned it — provenance for the audit trail."""
    body = _read(_APP / "api" / "plans.py")
    assert '"source_finding_id"' in body
    assert '"from_finding"' in body


def test_plan_from_finding_audited():
    body = _read(_APP / "api" / "plans.py")
    assert "plan.from_finding" in body


def test_plan_from_finding_runs_impact_analysis():
    """The generated Plan carries an impact report just like a
    hand-written one — so the approver sees blast radius."""
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plan_from_finding(")
    slab = body[fn_idx:fn_idx + 4500]
    assert "impact_analysis(" in slab
    assert "directly_affected" in slab


def test_plan_from_finding_creates_worktree():
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plan_from_finding(")
    slab = body[fn_idx:fn_idx + 4500]
    assert "create_worktree(" in slab


# ---------------------------------------------------------------------------
# findings.html — Create plan button
# ---------------------------------------------------------------------------


def test_findings_template_has_create_plan_button():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "createPlanFromFinding" in body
    assert 'data-i18n="Create plan"' in body
    assert "/api/v1/findings/${findingId}/plan" in body


def test_create_plan_button_only_for_open_findings():
    """A resolved / false-positive finding shouldn't offer a
    'create plan' button — there's nothing to fix."""
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert 'f.status === "open" || f.status === "acknowledged"' in body


def test_create_plan_fires_notification():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "MnemosUI.notify(" in body


def test_plans_template_accepts_project_query():
    """The Finding→Plan flow deep-links to /plans?project=… so the
    operator lands on the new draft."""
    body = _read(_APP / "dashboard" / "templates" / "plans.html")
    assert 'URLSearchParams(window.location.search).get("project")' in body


def test_mini_btn_styled():
    body = _read(_APP / "dashboard" / "static" / "app.css")
    assert ".mini-btn" in body


def test_phrase_book_translates_plan_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("계획 생성", "초안 계획", "Plans 탭"):
        assert kr in body
