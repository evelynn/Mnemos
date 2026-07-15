"""PR-63 — two-sided ROI rollup + compliance coverage (core-value C).

A critical reassessment scored 비즈니스 분석 (business analysis) short
of target on two counts:

* ROI was one-directional — ``project_llm_cost`` tracked *spend* with
  no *benefit* side, so C6 "ROI tracking" was only half-met.
* ``opaque_component_failing`` carried no CWE, so it never produced a
  compliance pill despite being a real supply-chain trust weakness.

This PR adds a ``findings/roi`` endpoint pairing risk eliminated
against spend, maps ``opaque_component_failing`` to CWE-1357, and
surfaces ROI on the executive report.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ROI endpoint
# ---------------------------------------------------------------------------


def test_roi_endpoint_defined():
    body = _read(_APP / "api" / "findings.py")
    assert '"/api/v1/projects/{project_id}/findings/roi"' in body
    assert "async def findings_roi(" in body


def test_roi_is_two_sided():
    """Risk eliminated (benefit) paired with LLM spend (cost)."""
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert "risk_eliminated" in slab
    assert "estimated_usd" in slab
    assert "risk_per_usd" in slab
    # Precision — the analyzer-quality signal.
    assert '"precision"' in slab


def test_roi_org_scoped():
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    decorator = body[max(0, idx - 200):idx]
    assert "require_project_org" in decorator


def test_roi_only_counts_resolved_as_benefit():
    """false_positive findings eliminated no real risk — they must
    not inflate risk_eliminated, only feed the precision figure."""
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert 'status_val == "resolved"' in slab
    assert 'status_val == "false_positive"' in slab


# ---------------------------------------------------------------------------
# compliance — opaque_component_failing now carries a CWE
# ---------------------------------------------------------------------------


def test_opaque_component_has_cwe():
    from app.merge.risk import remediation_for

    _, cwe = remediation_for("opaque_component_failing")
    assert cwe == "CWE-1357"


def test_cwe_1357_maps_to_frameworks():
    from app.merge.compliance import compliance_for, compliance_tags

    mapping = compliance_for("CWE-1357")
    assert "owasp" in mapping
    assert "nist_800_53" in mapping
    tags = compliance_tags("CWE-1357")
    assert any("OWASP" in t for t in tags)
    assert any("Supply Chain" in t for t in tags)


# ---------------------------------------------------------------------------
# Executive report — ROI panel
# ---------------------------------------------------------------------------


def test_report_fetches_and_renders_roi():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "/findings/roi" in body
    assert 'id="rp-roi"' in body
    assert 'data-i18n="Return on investment"' in body


def test_phrase_book_translates_roi_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("투자 수익", "위험 점수 제거됨", "달러당 제거된 위험", "발견 정밀도"):
        assert kr in body, f"phrase book missing {kr!r}"
