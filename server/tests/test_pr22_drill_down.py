"""PR-22 — Phase 2 dashboard drill-down (P2-7)."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_API = Path(__file__).resolve().parents[1] / "app" / "api"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stat cards are <a> elements with the right hrefs
# ---------------------------------------------------------------------------


def test_stat_cards_are_clickable_links():
    body = _read(_TPL / "dashboard.html")
    # Every actionable card uses ``<a class="stat-link" href="…">``.
    assert 'class="stat-card stat-link"' in body
    # Readiness is the only non-link card (status display, no list to drill into).
    assert body.count('class="stat-card stat-link"') >= 7


def test_drill_down_hrefs_carry_filters():
    body = _read(_TPL / "dashboard.html")
    # Each link must arrive at its target with the matching query.
    expected = [
        'href="/findings?status=open"',
        'href="/diffs?filter=blocked"',
        'href="/analysis?status=failed&since=24h"',
        'href="/settings#project-dbs"',
        'href="/audit?action=webhook.received"',
    ]
    for h in expected:
        assert h in body, f"missing drill-down link {h}"


def test_stat_link_has_focus_visible_outline():
    body = _read(_TPL / "dashboard.html")
    assert "a.stat-link:focus-visible" in body


def test_stat_link_has_hover_state():
    body = _read(_TPL / "dashboard.html")
    assert "a.stat-link:hover" in body


# ---------------------------------------------------------------------------
# Target pages honour the query params
# ---------------------------------------------------------------------------


def test_findings_prefills_from_query():
    body = _read(_TPL / "findings.html")
    assert 'params.get("status")' in body
    assert 'params.get("severity")' in body


def test_analysis_filters_runs_from_query():
    body = _read(_TPL / "analysis.html")
    assert 'params.get("status")' in body
    assert 'params.get("since")' in body
    # 24h cut-off honoured client-side.
    assert "24 * 3600 * 1000" in body


def test_analysis_auto_dispatches_load_when_filter_present():
    body = _read(_TPL / "analysis.html")
    # When status/since arrives with a project id we auto-trigger
    # Load runs so the operator sees the filtered list immediately.
    assert 'params.has("status")' in body or 'params.has("since")' in body


def test_audit_prefills_and_auto_searches():
    body = _read(_TPL / "audit.html")
    assert 'params.get("action")' in body
    assert 'params.get("actor")' in body
    # Auto-submit when any filter is present.
    assert 'form.dispatchEvent(new Event("submit"))' in body


def test_diffs_loads_filtered_when_query_present():
    body = _read(_TPL / "diffs.html")
    assert "loadFiltered" in body
    assert '?verdict=' in body
    # The handler decides between plan-scoped and org-wide based on
    # the ``?filter=…`` param.
    assert '"blocked"' in body


# ---------------------------------------------------------------------------
# New org-scoped /api/v1/diff_submissions endpoint
# ---------------------------------------------------------------------------


def test_org_scoped_submissions_endpoint_exists():
    body = _read(_API / "diffs.py")
    assert '@router.get("/api/v1/diff_submissions")' in body
    assert "list_submissions_filtered" in body


def test_org_scoped_endpoint_filters_verdict():
    body = _read(_API / "diffs.py")
    # The handler must explicitly check for the verdict whitelist.
    assert '"blocked"' in body
    assert "verdict ==" in body or "verdict in {" in body


def test_org_scoped_endpoint_is_org_isolated():
    body = _read(_API / "diffs.py")
    # The org isolation comes from the join + the where clause on
    # ``organization_id``. Both must be present.
    assert "organization_id" in body
    assert ".join(Project" in body or "Project.organization_id" in body


# ---------------------------------------------------------------------------
# settings.html has the anchor target the dashboard links to
# ---------------------------------------------------------------------------


def test_settings_has_project_dbs_anchor():
    body = _read(_TPL / "settings.html")
    assert 'id="project-dbs"' in body
