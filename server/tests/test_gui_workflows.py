"""End-to-end GUI workflow checks (PR-13).

PR-13 turned three "curl-only" scenarios into proper dashboard forms:
ProjectDB registration, ad-hoc SQL query, and visible triggered_by
distinction on analysis runs. Static greps guard the regressions.
"""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(name: str) -> str:
    return (_TPL / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# settings.html — ProjectDB registration form (admin only)
# ---------------------------------------------------------------------------


def test_settings_has_pdb_create_form():
    body = _read("settings.html")
    assert 'id="pdb-create"' in body, "ProjectDB registration form missing"


def test_pdb_form_shows_kind_specific_hint():
    body = _read("settings.html")
    assert "_PDB_HINTS" in body
    assert "oracledb thin mode" in body
    assert "Encrypt=true" in body


def test_pdb_form_renders_412_grants_summary():
    """Probe failure must surface grants_summary in a dialog, not be
    truncated to 200 chars in a toast."""
    body = _read("settings.html")
    assert 'id="probe-fail-dialog"' in body
    assert "renderJsonFromScript" in body


def test_pdb_form_is_admin_only():
    body = _read("settings.html")
    assert "{% if user.role == 'admin' %}" in body


# ---------------------------------------------------------------------------
# data.html — read-only SQL runner
# ---------------------------------------------------------------------------


def test_data_has_query_runner():
    body = _read("data.html")
    assert 'id="query-runner"' in body
    assert 'id="q-sql"' in body
    assert 'id="q-purpose"' in body
    assert 'id="q-max-rows"' in body


def test_query_runner_reads_warning_header():
    """RFC 7234 Warning header (PR-8 clamp) must reach the user."""
    body = _read("data.html")
    assert 'r.headers.get("Warning")' in body


def test_query_runner_surfaces_limit_clamp():
    body = _read("data.html")
    assert 'limit_clamp' in body


# ---------------------------------------------------------------------------
# analysis.html — webhook vs user badge + failure toast
# ---------------------------------------------------------------------------


def test_analysis_distinguishes_webhook_from_user():
    body = _read("analysis.html")
    assert "🔗 webhook" in body
    assert 'startsWith("webhook:")' in body


def test_analysis_alerts_on_failed_run():
    body = _read("analysis.html")
    assert 'event === "run_failed"' in body
    assert "MnemosUI.showToast" in body


def test_app_css_has_badge_styles():
    css = (_STATIC / "app.css").read_text(encoding="utf-8")
    assert ".badge.webhook" in css
    assert ".badge.user" in css
