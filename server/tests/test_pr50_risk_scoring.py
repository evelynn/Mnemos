"""PR-50 — finding risk scoring + remediation hints.

Closes the value-audit's B-area gap: findings were observations
with no priority ordering and no fix guidance (scored 35/100).
This PR adds a deterministic 0-100 risk score, a fix hint per
finding kind, CWE cross-references, and the acknowledged →
resolved lifecycle timestamps.
"""

from __future__ import annotations

from pathlib import Path

from app.merge.risk import (
    priority_label,
    remediation_for,
    score_finding,
)

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# score_finding — the 0-100 risk number
# ---------------------------------------------------------------------------


def test_score_scales_with_severity():
    """info < warning < error — the base ordering must hold."""
    info = score_finding(severity="info")
    warning = score_finding(severity="warning")
    error = score_finding(severity="error")
    assert info < warning < error


def test_exercised_finding_scores_higher():
    """A finding on a code path confirmed live by OTLP traces is
    more urgent than the same finding on apparently-dead code."""
    static = score_finding(severity="warning", exercised=False)
    live = score_finding(severity="warning", exercised=True)
    assert live > static


def test_blast_radius_raises_score():
    """A finding on a heavily-connected node ranks above a leaf."""
    leaf = score_finding(severity="warning", blast_radius=0)
    hub = score_finding(severity="warning", blast_radius=30)
    assert hub > leaf


def test_blast_radius_bonus_is_capped():
    """The bonus tops out at +20 so a 1000-edge node doesn't
    swamp the severity signal."""
    huge = score_finding(severity="info", blast_radius=100_000)
    # info base 10 + at most 20 = 30; never above.
    assert huge <= 30


def test_score_is_clamped_0_100():
    # Worst case — error + exercised + huge blast radius.
    worst = score_finding(severity="error", exercised=True, blast_radius=100_000)
    assert 0 <= worst <= 100
    # Unknown severity falls back to warning weight, not 0.
    unknown = score_finding(severity="bogus")
    assert unknown > 0


def test_unknown_severity_uses_warning_default():
    assert score_finding(severity="bogus") == score_finding(severity="warning")


# ---------------------------------------------------------------------------
# priority_label — numeric score → P1..P4 bucket
# ---------------------------------------------------------------------------


def test_priority_buckets():
    assert priority_label(90) == "P1"
    assert priority_label(75) == "P1"
    assert priority_label(60) == "P2"
    assert priority_label(50) == "P2"
    assert priority_label(30) == "P3"
    assert priority_label(25) == "P3"
    assert priority_label(10) == "P4"
    assert priority_label(0) == "P4"


# ---------------------------------------------------------------------------
# remediation_for — fix hint + CWE per finding kind
# ---------------------------------------------------------------------------


def test_remediation_covers_all_phase1_finding_kinds():
    """Every finding kind the merge layer can produce must have a
    bespoke remediation hint — not the generic fallback."""
    for kind in (
        "duplicate_endpoint",
        "unverified_claim",
        "dynamic_call_detected",
        "dead_path_suspected",
        "schema_mismatch",
        "opaque_component_failing",
    ):
        hint, cwe = remediation_for(kind)
        assert hint and len(hint) > 30, f"{kind}: hint too thin"
        # The hint must be actionable — mention a concrete next step.
        assert any(
            verb in hint.lower()
            for verb in ("open a plan", "add", "replace", "delete",
                         "reconcile", "route", "annotate", "wrap")
        ), f"{kind}: hint not actionable"


def test_remediation_attaches_cwe_where_applicable():
    """Security-relevant kinds carry a CWE id for compliance
    cross-referencing."""
    _, cwe_dyn = remediation_for("dynamic_call_detected")
    _, cwe_dead = remediation_for("dead_path_suspected")
    assert cwe_dyn == "CWE-470"
    assert cwe_dead == "CWE-561"


def test_remediation_unknown_kind_gets_actionable_default():
    hint, cwe = remediation_for("some_future_kind")
    assert "Plan" in hint
    assert cwe is None


# ---------------------------------------------------------------------------
# Model + migration wiring
# ---------------------------------------------------------------------------


def test_finding_model_has_risk_columns():
    body = _read(_APP / "models" / "findings.py")
    for col in ("risk_score", "remediation", "cwe_id", "acknowledged_at"):
        assert col in body, f"Finding model missing {col!r}"


def test_migration_present():
    body = _read(
        _SERVER / "alembic" / "versions" / "0020_finding_risk_columns.py"
    )
    assert "risk_score" in body
    assert "ix_findings_project_risk" in body
    assert 'down_revision: Union[str, None] = "0019_user_invites_and_resets"' in body


# ---------------------------------------------------------------------------
# merge/findings.py — score computed at upsert
# ---------------------------------------------------------------------------


def test_merge_computes_risk_at_upsert():
    body = _read(_APP / "merge" / "findings.py")
    assert "score_finding(" in body
    assert "remediation_for(" in body
    # Blast radius + exercised both feed the score.
    assert "_blast_radius(" in body
    assert "_subject_is_exercised(" in body


def test_merge_rescores_existing_findings():
    """A re-scan must re-score — the blast radius / exercised flag
    can change as the graph evolves between runs."""
    body = _read(_APP / "merge" / "findings.py")
    assert "existing.risk_score = risk" in body


# ---------------------------------------------------------------------------
# findings API — risk-first ordering + lifecycle
# ---------------------------------------------------------------------------


def test_findings_api_default_sort_is_risk():
    body = _read(_APP / "api" / "findings.py")
    # The default ``sort`` param is "risk".
    assert 'sort: str = "risk"' in body
    assert "Finding.risk_score.desc()" in body
    # The old recency sort stays available.
    assert 'sort == "recent"' in body


def test_findings_out_exposes_risk_fields():
    body = _read(_APP / "api" / "findings.py")
    for field in ("risk_score", "priority", "remediation", "cwe_id"):
        assert field in body


def test_patch_finding_records_acknowledged_at():
    body = _read(_APP / "api" / "findings.py")
    assert "acknowledged_at" in body
    assert 'body.status == "acknowledged"' in body


# ---------------------------------------------------------------------------
# findings.html — priority pill + risk bar + remediation
# ---------------------------------------------------------------------------


def test_findings_template_renders_priority_and_risk():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "priority-pill" in body
    assert "risk-bar" in body
    # The suggested-fix column shows the remediation hint.
    assert "Suggested fix" in body
    assert "f.remediation" in body


def test_findings_template_links_cwe():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "cwe.mitre.org" in body


def test_priority_pill_styled_by_level():
    body = _read(_APP / "dashboard" / "static" / "app.css")
    for level in ("P1", "P2", "P3", "P4"):
        assert f".priority-pill.{level}" in body


def test_phrase_book_translates_risk_columns():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("우선순위", "위험도", "권장 조치", "대상"):
        assert kr in body
