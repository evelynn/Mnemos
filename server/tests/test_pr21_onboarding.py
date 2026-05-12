"""PR-21 — Phase 2 onboarding auto-progression (P2-9)."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# projects.html — post-create CTA + step-1 mark
# ---------------------------------------------------------------------------


def test_projects_marks_step1_done_on_success():
    body = _read(_TPL / "projects.html")
    assert "mnemos_onboarding_step1_done" in body


def test_projects_shows_post_create_cta():
    body = _read(_TPL / "projects.html")
    assert 'id="post-create-cta"' in body
    # The href is patched at runtime to include the new project id +
    # ``?onboard=1`` so analysis.html can pre-fill its form.
    assert "onboard=1" in body


def test_projects_toast_on_success():
    body = _read(_TPL / "projects.html")
    assert "MnemosUI.showToast" in body


# ---------------------------------------------------------------------------
# analysis.html — auto-prefill + step-2 mark + onboarding banner
# ---------------------------------------------------------------------------


def test_analysis_handles_onboard_query_param():
    body = _read(_TPL / "analysis.html")
    # The bootstrap reads ``?project=…&onboard=1`` and pre-fills the
    # forms + reveals the step-2 banner.
    assert 'params.get("project")' in body
    assert 'params.get("onboard")' in body
    assert 'id="onboard-banner"' in body


def test_analysis_marks_step2_on_successful_trigger():
    body = _read(_TPL / "analysis.html")
    assert "mnemos_onboarding_step2_done" in body


def test_analysis_prefills_run_id_from_response():
    """A small UX win: after Start analysis succeeds the response's
    run id auto-populates the Monitor input so the operator doesn't
    have to copy-paste."""
    body = _read(_TPL / "analysis.html")
    assert 'document.getElementById("run-id").value = j.id' in body


# ---------------------------------------------------------------------------
# findings.html — step-3 mark
# ---------------------------------------------------------------------------


def test_findings_marks_step3_when_results_shown():
    body = _read(_TPL / "findings.html")
    assert "mnemos_onboarding_step3_done" in body


# ---------------------------------------------------------------------------
# dashboard.html — step-state-aware onboarding card
# ---------------------------------------------------------------------------


def test_dashboard_renders_step_aware_onboarding():
    body = _read(_TPL / "dashboard.html")
    assert "updateOnboarding" in body
    for sid in ("onb-step-1", "onb-step-2", "onb-step-3"):
        assert f'id="{sid}"' in body


def test_dashboard_hides_onboarding_when_all_steps_done():
    body = _read(_TPL / "dashboard.html")
    # Single guard: ``hidden = step1Done && step2Done && step3Done``.
    assert "step1Done && step2Done && step3Done" in body


def test_dashboard_marks_current_step():
    body = _read(_TPL / "dashboard.html")
    # CSS classes for done vs current are applied via classList.toggle.
    assert '"done"' in body
    assert '"current"' in body


# ---------------------------------------------------------------------------
# CSS — onboarding step-cta + done/current classes
# ---------------------------------------------------------------------------


def test_css_has_step_state_classes():
    css = _read(_STATIC / "app.css")
    assert ".onboarding li.done" in css
    assert ".onboarding li.current" in css


def test_css_has_post_action_cta_with_animation():
    css = _read(_STATIC / "app.css")
    assert ".onboarding-step-cta" in css
    # Pulse animation makes the CTA visually pop without being a
    # full-screen modal — the audit recommended manual progression
    # over auto-redirect.
    assert "@keyframes pulse-cta" in css
