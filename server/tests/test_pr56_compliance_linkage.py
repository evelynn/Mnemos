"""PR-56 — compliance mapping + Finding↔Plan linkage (core-value B).

The 2nd value reassessment left two B-area items partial:
B7 (no compliance-framework mapping — CWE id alone) and B5
(Finding→Plan link was write-only). This PR closes both.
"""

from __future__ import annotations

from pathlib import Path

from app.merge.compliance import compliance_for, compliance_tags

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# compliance.py — CWE → framework mapping
# ---------------------------------------------------------------------------


def test_compliance_for_known_cwe():
    """CWE-470 (unsafe reflection) maps to an OWASP injection
    entry + a PCI secure-coding requirement + a NIST control."""
    m = compliance_for("CWE-470")
    assert "owasp" in m
    assert any("Injection" in r for r in m["owasp"])
    assert "pci_dss" in m
    assert "nist_800_53" in m


def test_compliance_for_unknown_cwe_is_empty():
    assert compliance_for("CWE-99999") == {}
    assert compliance_for(None) == {}


def test_compliance_for_drops_empty_frameworks():
    """CWE-1041 (redundant code) has no OWASP entry — the empty
    list must be dropped, not returned as ``"owasp": []``."""
    m = compliance_for("CWE-1041")
    assert "owasp" not in m
    # But it does carry a NIST control.
    assert "nist_800_53" in m


def test_compliance_tags_flat_format():
    """``compliance_tags`` returns ``"FRAMEWORK: requirement"``
    strings — the shape the findings table renders as pills."""
    tags = compliance_tags("CWE-470")
    assert tags
    assert all(": " in t for t in tags)
    assert any(t.startswith("OWASP Top 10:") for t in tags)


def test_compliance_tags_empty_for_no_cwe():
    assert compliance_tags(None) == []
    assert compliance_tags("CWE-00000") == []


def test_every_phase1_cwe_is_mapped():
    """The CWEs the PR-50 remediation table attaches must all have
    a compliance mapping — otherwise a finding shows a CWE id with
    no framework context."""
    from app.merge.risk import _REMEDIATION

    for kind, (_, cwe) in _REMEDIATION.items():
        if cwe is None:
            continue
        # A mapped CWE must produce at least one tag.
        assert compliance_tags(cwe), (
            f"{kind}'s CWE {cwe} has no compliance mapping"
        )


# ---------------------------------------------------------------------------
# findings API — compliance_tags surfaced
# ---------------------------------------------------------------------------


def test_finding_out_exposes_compliance_tags():
    body = _read(_APP / "api" / "findings.py")
    assert "compliance_tags: list[str]" in body
    assert "compliance_tags=compliance_tags(f.cwe_id)" in body


# ---------------------------------------------------------------------------
# plans API — plans_for_finding endpoint
# ---------------------------------------------------------------------------


def test_plans_for_finding_endpoint_defined():
    body = _read(_APP / "api" / "plans.py")
    assert '@router.get("/api/v1/findings/{finding_id}/plans")' in body
    assert "async def plans_for_finding(" in body


def test_plans_for_finding_filters_by_source_finding_id():
    """The link is ``impact_report.source_finding_id`` — PR-52
    wrote it; PR-56 reads it back."""
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plans_for_finding(")
    end = body.find("@router", fn_idx + 1)
    slab = body[fn_idx:end] if end > fn_idx else body[fn_idx:]
    assert 'source_finding_id' in slab


def test_plans_for_finding_org_isolated():
    body = _read(_APP / "api" / "plans.py")
    fn_idx = body.find("async def plans_for_finding(")
    end = body.find("@router", fn_idx + 1)
    slab = body[fn_idx:end] if end > fn_idx else body[fn_idx:]
    assert "organization_id" in slab
    assert "finding_not_found" in slab


# ---------------------------------------------------------------------------
# findings.html — compliance pills + linked-plan badge
# ---------------------------------------------------------------------------


def test_findings_template_renders_compliance_pills():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "compliance-tags" in body
    assert "compliance-pill" in body
    assert "f.compliance_tags" in body


def test_findings_template_lazy_loads_related_plans():
    """The related-plans fetch fires on <details> open, not up
    front — N findings shouldn't mean N fetches on render."""
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "related-plans" in body
    assert "/api/v1/findings/${fid}/plans" in body
    assert 'addEventListener("toggle"' in body


def test_compliance_and_plan_badge_styled():
    body = _read(_APP / "dashboard" / "static" / "app.css")
    assert ".compliance-pill" in body
    assert ".plan-badge" in body


def test_phrase_book_translates_linkage_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    assert "연결된 계획" in body
