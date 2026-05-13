"""PR-28 — 8th-round audit closure: i18n broader rollout + onboarding /
SSE state sync fixes + audit timestamp hydration."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A2 — every page <h1> is now data-i18n-tagged
# ---------------------------------------------------------------------------

_PAGE_LABELS = {
    "audit.html":          "Audit log",
    "analysis.html":       "Analysis",
    "dashboard.html":      "Dashboard",
    "plans.html":          "Plans (Gate A)",
    "diffs.html":          "Diffs (Gate B)",
    "gdpr.html":           "GDPR tools",
    "sso.html":            "SSO / OIDC",
    "projects.html":       "Projects",
    "organizations.html":  "Organizations",
    "data.html":           "Data entities",
    "settings.html":       "Settings",
    "findings.html":       "Findings",
}


def test_every_page_h1_is_translatable():
    for fname, label in _PAGE_LABELS.items():
        body = _read(_TPL / fname)
        assert f'<h1 data-i18n="{label}">' in body, (
            f"{fname}: <h1> for {label!r} must carry data-i18n attribute"
        )


def test_phrase_book_covers_every_page_h1():
    body = _read(_STATIC / "ui.js")
    # Every label in the page set has a Korean translation. (Findings,
    # Projects, Analysis etc. were already there from PR-26/27 — the
    # new entries are the rest.)
    for label in (
        "Audit log",
        "Plans (Gate A)",
        "Diffs (Gate B)",
        "Data entities",
        "Organizations",
        "GDPR tools",
    ):
        assert f'"{label}"' in body, f"phrase book missing {label!r}"


# ---------------------------------------------------------------------------
# A9 — sidebar admin section + Sign-out button are i18n-tagged
# ---------------------------------------------------------------------------


def test_admin_sidebar_section_is_translatable():
    body = _read(_TPL / "_layout.html")
    assert 'data-i18n="Admin"' in body
    assert 'data-i18n="Organizations"' in body
    assert 'data-i18n="SSO / OIDC"' in body
    assert 'data-i18n="GDPR tools"' in body
    assert 'data-i18n="Sign out"' in body


# ---------------------------------------------------------------------------
# B3 — onboarding step 3 marks done on entry, not on non-empty rows
# ---------------------------------------------------------------------------


def test_step3_completes_on_findings_entry_regardless_of_rows():
    body = _read(_TPL / "findings.html")
    # The previous ``if (rows.length > 0)`` gate is gone — reaching
    # the findings view counts as "reviewed" even when an analysis
    # genuinely produced zero findings (8th-round audit B3).
    assert "if (rows.length > 0) sessionStorage" not in body
    assert "if (rows.length > 0) localStorage" not in body
    # The unconditional set is still present.
    assert 'localStorage.setItem("mnemos_onboarding_step3_done"' in body


# ---------------------------------------------------------------------------
# Critical UX-1 — onboarding state lives in localStorage (cross-tab)
# ---------------------------------------------------------------------------


def test_onboarding_state_uses_localstorage():
    """The 8th-round audit flagged sessionStorage as tab-isolated:
    completing step 2 in one tab left the dashboard tab showing
    "in progress". localStorage fixes the cross-tab story."""
    for fname in ("projects.html", "analysis.html", "findings.html"):
        body = _read(_TPL / fname)
        assert "sessionStorage" not in body, (
            f"{fname}: onboarding state must use localStorage, not "
            f"sessionStorage (8th-round audit Critical UX-1)"
        )
        assert "localStorage.setItem(\"mnemos_onboarding_step" in body


def test_dashboard_reads_onboarding_state_from_localstorage():
    body = _read(_TPL / "dashboard.html")
    assert 'localStorage.getItem("mnemos_onboarding_step2_done")' in body
    assert 'localStorage.getItem("mnemos_onboarding_step3_done")' in body


# ---------------------------------------------------------------------------
# Critical UX-2 — SSE state synced via localStorage, replayed on new tab
# ---------------------------------------------------------------------------


def test_sse_state_persists_to_localstorage():
    body = _read(_STATIC / "ui.js")
    # ``publishSseState`` writes the latest state so a tab opened
    # mid-disconnect can find it.
    assert 'localStorage.setItem(\n        "mnemos_sse_last_state"' in body or \
           'localStorage.setItem("mnemos_sse_last_state"' in body


def test_sse_state_replayed_on_new_tab():
    body = _read(_STATIC / "ui.js")
    # The DOMContentLoaded handler reads the persisted state and
    # applies the strip immediately — no waiting for the next publish.
    assert "_readPersistedSseState" in body


def test_sse_state_ignores_stale_reads():
    """A laptop closed in disconnected state shouldn't show the
    strip when it reopens days later. 30-minute freshness window."""
    body = _read(_STATIC / "ui.js")
    assert "30 * 60 * 1000" in body


# ---------------------------------------------------------------------------
# B1 — audit timestamps use <time data-ts>
# ---------------------------------------------------------------------------


def test_audit_uses_time_data_ts():
    body = _read(_TPL / "audit.html")
    assert "<time data-ts=" in body
    assert "hydrateRelativeTimes" in body


def test_audit_escapes_json_detail():
    """While we were touching the audit row builder, fix the latent
    XSS path the findings table had (PR-24 closed the same on
    findings.html)."""
    body = _read(_TPL / "audit.html")
    assert "escapeHtml(JSON.stringify(e.details))" in body


# ---------------------------------------------------------------------------
# Dashboard runs-box: dynamic insert applies i18n too
# ---------------------------------------------------------------------------


def test_dashboard_dynamic_runs_apply_i18n():
    body = _read(_TPL / "dashboard.html")
    # After ``box.innerHTML = html`` the dashboard hydrates
    # relative times *and* applies the i18n pass on the new fragment
    # so any future ``data-i18n`` markers in the row HTML pick up
    # translations.
    assert "MnemosUI.applyI18n(box)" in body
