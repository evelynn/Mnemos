"""PR-17 verification — share-URL guest, onboarding, SSE recovery, metrics_summary."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_API = Path(__file__).resolve().parents[1] / "app" / "api"
_CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# share-URL guest flow (Critical C-Code-2 + Team B must-fix #3)
# ---------------------------------------------------------------------------


def test_login_html_stashes_hash():
    body = _read(_TPL / "login.html")
    assert "mnemos_post_login_hash" in body
    assert "sessionStorage" in body


def test_base_html_restores_hash():
    body = _read(_TPL / "base.html")
    assert "mnemos_post_login_hash" in body
    assert "sessionStorage.getItem" in body
    # PR-18 refined the restore: bare ``approve=`` hashes route to
    # /diffs, everything else lands on whatever page the operator hit.
    # Either restore path counts as proof the mechanism is wired up.
    assert (
        "window.location.hash = hash" in body
        or "window.location.replace" in body
    )


# ---------------------------------------------------------------------------
# Onboarding card (UX-Crit-1)
# ---------------------------------------------------------------------------


def test_dashboard_has_onboarding_card():
    body = _read(_TPL / "dashboard.html")
    assert 'id="onboarding-card"' in body
    assert "Welcome to Mnemos" in body
    assert "Register a GitLab project" in body


def test_dashboard_reveals_onboarding_when_no_projects():
    body = _read(_TPL / "dashboard.html")
    # PR-21 promoted the simple ``hidden = rows.length !== 0`` check
    # into a multi-step state machine. The onboarding card now reveals
    # whenever any step is incomplete, not just on a literally empty
    # project list.
    # PR-108 — the call site grew a second argument carrying
    # observed-from-API completion flags, replacing the old
    # localStorage source-of-truth.
    assert "updateOnboarding(rows.length, { hasCompletedRun, hasFinding })" in body
    assert "step1Done = projectCount > 0" in body


def test_app_css_has_onboarding_style():
    css = _read(_STATIC / "app.css")
    assert ".onboarding" in css


# ---------------------------------------------------------------------------
# SSE recovery (UX-Crit-2 + Team B must-fix #6)
# ---------------------------------------------------------------------------


def test_analysis_has_sse_status_badge():
    body = _read(_TPL / "analysis.html")
    assert 'id="sse-status"' in body


def test_analysis_has_onerror_handler():
    body = _read(_TPL / "analysis.html")
    assert "source.onerror" in body


def test_analysis_retry_lifecycle_is_generation_scoped():
    body = _read(_TPL / "analysis.html")
    assert "let _retryTimer" in body
    assert "let _monitorGeneration" in body
    assert "clearTimeout(_retryTimer)" in body
    assert "generation !== _monitorGeneration" in body
    assert "_activeRunId !== id" in body
    assert "_SSE_BACKOFF_MS" in body


def test_analysis_has_jitter_in_backoff():
    """Thundering-herd defence — Team B's 5th-round critique."""
    body = _read(_TPL / "analysis.html")
    # Either ``0.5 + Math.random()`` or ``Math.random() * 0.5`` etc.
    assert "Math.random()" in body


def test_analysis_handles_visibility_change():
    body = _read(_TPL / "analysis.html")
    assert "visibilitychange" in body
    assert "visibilityState" in body


def test_analysis_cleans_up_on_unload():
    body = _read(_TPL / "analysis.html")
    assert "beforeunload" in body


def test_analysis_closes_prior_sse_before_reopen():
    body = _read(_TPL / "analysis.html")
    # No-leak guarantee on re-monitor — the previous EventSource must
    # be closed and nulled before a new one is opened.
    assert "_sse.close()" in body


# ---------------------------------------------------------------------------
# Role hint (UX-Crit-3 + Team B must-fix #7)
# ---------------------------------------------------------------------------


def test_layout_exposes_role_hint():
    body = _read(_TPL / "_layout.html")
    assert "window.MNEMOS_USER_ROLE_HINT" in body


def test_break_glass_button_is_admin_gated():
    body = _read(_TPL / "diffs.html")
    # The button must read the role hint, not blindly render for every
    # operator (4th-round audit, UX-Crit-3).
    assert "MNEMOS_USER_ROLE_HINT" in body
    assert "admin" in body


# ---------------------------------------------------------------------------
# DB-aggregate metrics summary (Team B must-fix #8)
# ---------------------------------------------------------------------------


def test_metrics_summary_endpoint_defined():
    body = _read(_API / "health.py")
    assert "/api/v1/health/metrics_summary" in body
    assert "metrics_summary" in body


def test_metrics_summary_queries_postgres_not_prometheus():
    """The endpoint must aggregate from Postgres so the multi-worker
    Prometheus registry split (Team B critique #8) doesn't cause
    silent under-reporting."""
    body = _read(_API / "health.py")
    assert "FROM project_dbs" in body
    assert "FROM diff_break_glass_grants" in body
    assert "FROM analysis_runs" in body
    # table is audit_log (singular) — see migration 0003. The endpoint
    # previously referenced a nonexistent audit_logs table and 503'd.
    assert "FROM audit_log " in body
    # Must not be pulling from prometheus_client globals.
    assert "prometheus_client" not in body


def test_dashboard_renders_four_new_cards():
    body = _read(_TPL / "dashboard.html")
    for stat_id in (
        "stat-bg-active",
        "stat-runs-failed",
        "stat-dbs-disabled",
        "stat-webhooks",
    ):
        assert f'id="{stat_id}"' in body


# ---------------------------------------------------------------------------
# CI hardening (M-1, M-3)
# ---------------------------------------------------------------------------


def test_ci_builds_dotnet_analyzers():
    body = _read(_CI)
    assert "actions/setup-dotnet@v4" in body
    assert "dotnet build analyzers/ggoss-sql-mssql" in body


def test_ci_verifies_integration_collection():
    body = _read(_CI)
    # The grep must pick out actual test functions, not fixtures.
    assert "pytest --co -q -m integration" in body
    assert "::test_" in body
