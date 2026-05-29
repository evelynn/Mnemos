"""PR-71 — web dashboard fixes from the frontend critical review.

The review found one critical and several i18n / a11y defects, most
introduced by the recent graph PRs:

* the Canvas graph backend (>200 nodes) had no click handler, so
  PR-67's click-a-node-to-confirm silently failed on large graphs;
* report stat-card labels, the L3 "Open questions:" label, the SSE-
  disconnect toast and the findings pagination strings were built in
  JS as raw English, bypassing the i18n phrase book;
* SVG graph nodes were mouse-only — no keyboard affordance;
* a ?project= deep link to /findings pre-filled the form but never
  loaded; profile.html called async showError without await.
"""

from __future__ import annotations

from pathlib import Path

_DASH = Path(__file__).resolve().parents[1] / "app" / "dashboard"


def _read(name: str) -> str:
    return (_DASH / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Critical — Canvas graph nodes are clickable
# ---------------------------------------------------------------------------


def test_canvas_node_click_wired():
    body = _read("templates/graph.html")
    assert "function _canvasClick(" in body
    assert '.addEventListener("click", _canvasClick)' in body
    # Both backends share one hit-test.
    assert "function _canvasNodeAt(" in body


def test_svg_nodes_keyboard_accessible():
    body = _read("templates/graph.html")
    assert "function _nodeKey(" in body
    # The circle is focusable and announces itself.
    assert 'tabindex="0" role="button"' in body
    assert "onkeydown=" in body


# ---------------------------------------------------------------------------
# i18n — strings that were English-only now route through the phrase book
# ---------------------------------------------------------------------------


def test_report_stat_cards_translatable():
    body = _read("templates/report.html")
    for key in ("Risk index", "Mean TTR", "7-day delta"):
        assert f'data-i18n="{key}"' in body


def test_report_open_questions_translatable():
    body = _read("templates/report.html")
    assert 'MnemosUI.t("Open questions:")' in body


def test_sse_disconnect_toast_translatable():
    body = _read("templates/analysis.html")
    idx = body.find("Live analysis stream disconnected")
    slab = body[max(0, idx - 120):idx]
    assert "MnemosUI.t(" in slab


def test_findings_pagination_translatable():
    body = _read("templates/findings.html")
    assert 'MnemosUI.t("Show next $1"' in body
    assert 'MnemosUI.t(' in body
    assert '"Showing $1 of $2 findings."' in body


def test_new_phrases_in_book():
    body = _read("static/ui.js")
    for key in (
        "Mean TTR",
        "7-day delta",
        "Open questions:",
        "Show next $1",
        "Showing $1 of $2 findings.",
    ):
        assert f'"{key}"' in body, f"phrase book missing {key!r}"


# ---------------------------------------------------------------------------
# Misc — deep link + awaited error handler
# ---------------------------------------------------------------------------


def test_findings_deep_link_auto_loads():
    body = _read("templates/findings.html")
    idx = body.find("function fromQuery(")
    slab = body[idx:idx + 650]
    assert 'params.get("project")' in slab
    assert "loadFindings(" in slab


def test_profile_awaits_show_error():
    body = _read("templates/profile.html")
    assert "await MnemosUI.showError(\"Profile load failed\"" in body
