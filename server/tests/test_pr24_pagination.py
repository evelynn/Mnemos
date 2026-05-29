"""PR-24 — P2-6 large-result client-side pagination."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# data.html — query result chunked render
# ---------------------------------------------------------------------------


def test_data_html_uses_chunked_renderer():
    body = _read(_TPL / "data.html")
    assert "_renderRowsPaginated" in body
    assert "_showNextDataPage" in body
    # Default page size — the audit recommended 100 as a balance
    # between perceived "instant" rendering and showing enough context.
    assert "_DATA_PAGE_SIZE = 100" in body


def test_data_html_no_longer_dumps_all_rows_at_once():
    """Regression guard — the single-write ``for (const row of rows)``
    loop the audit flagged must be gone."""
    body = _read(_TPL / "data.html")
    # The single-write builder used ``html += '<tr>' + row.map(...)``
    # inline in the runQuery flow. The new path delegates to
    # _renderRowsPaginated. Confirm the inline write moved.
    assert "_renderRowsPaginated(out, banner, cols, rows, payload)" in body


def test_data_html_uses_template_fragment_for_perf():
    body = _read(_TPL / "data.html")
    # DocumentFragment via <template> is the canonical way to
    # batch-insert N rows without N reflows.
    assert 'document.createElement("template")' in body


def test_data_html_shows_progress_status():
    body = _read(_TPL / "data.html")
    # The status line is part of the affordance — the operator should
    # see "Showing 100 of 5000 rows" not just an unmarked button.
    assert "Showing" in body
    assert "of" in body


# ---------------------------------------------------------------------------
# findings.html — same treatment
# ---------------------------------------------------------------------------


def test_findings_uses_chunked_renderer():
    body = _read(_TPL / "findings.html")
    assert "_renderFindingsPaginated" in body
    assert "_showNextFindingsPage" in body
    assert "_FINDINGS_PAGE_SIZE = 100" in body


def test_findings_escapes_json_detail():
    """The previous ``${JSON.stringify(f.detail)}`` interpolation was
    a latent XSS path if a finding's detail ever contained
    ``</pre>``. The chunked renderer now goes through escapeHtml."""
    body = _read(_TPL / "findings.html")
    assert "escapeHtml(JSON.stringify(f.detail))" in body


def test_findings_severity_uses_badge():
    """The colour-blind glyphs PR-20 added live on ``.badge.<sev>``.
    Findings render now uses the badge wrapper so the glyphs apply."""
    body = _read(_TPL / "findings.html")
    assert '<span class="badge ${sev}">' in body
    assert '<span class="badge ${status}">' in body


def test_findings_renderer_uses_template_fragment():
    body = _read(_TPL / "findings.html")
    assert 'document.createElement("template")' in body


def test_paginated_renderers_stash_dataset_on_dom():
    """``tbody._rows = rows`` is the trick that lets the Show-next
    handler find the dataset without a closure. Both renderers
    follow the same pattern."""
    data_body = _read(_TPL / "data.html")
    findings_body = _read(_TPL / "findings.html")
    assert "tbody._rows = rows" in data_body
    assert "tbody._rows = rows" in findings_body
