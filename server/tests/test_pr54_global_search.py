"""PR-54 — command-palette global search (core-value D-area).

The value audit's D3 finding: the command palette only knew nav
shortcuts + the project list. A search for "payment" couldn't
surface a graph symbol or a finding. PR-54 wires the palette's
debounced remote search into the graph-search + findings APIs.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Remote search wiring
# ---------------------------------------------------------------------------


def test_remote_search_function_present():
    body = _read(_STATIC / "ui.js")
    assert "function _remoteSearch(" in body


def test_remote_search_queries_graph_and_findings():
    body = _read(_STATIC / "ui.js")
    rs_idx = body.find("function _remoteSearch(")
    end = body.find("\n  function _onPaletteInput", rs_idx)
    slab = body[rs_idx:end] if end > rs_idx else body[rs_idx:rs_idx + 2000]
    assert "/graph/search" in slab
    assert "/findings" in slab


def test_remote_search_needs_3_chars_and_project_context():
    """A 1-2 char query, or no project context, must not fire a
    fetch — otherwise every keystroke hammers the API."""
    body = _read(_STATIC / "ui.js")
    rs_idx = body.find("function _remoteSearch(")
    slab = body[rs_idx:rs_idx + 400]
    assert "query.length < 3" in slab
    assert "!pid" in slab


def test_palette_input_debounces_remote_search():
    """250ms debounce — the timer is cleared on every keystroke."""
    body = _read(_STATIC / "ui.js")
    assert "_paletteSearchTimer" in body
    assert "clearTimeout(_paletteSearchTimer)" in body
    assert "250" in body


def test_palette_guards_against_stale_results():
    """If the operator typed more while the fetch was in flight,
    the late result must NOT overwrite the current filtered set."""
    body = _read(_STATIC / "ui.js")
    assert "input.value !== query" in body


def test_project_context_from_url_or_localstorage():
    body = _read(_STATIC / "ui.js")
    assert "function _currentProjectContext(" in body
    assert 'get("project")' in body
    assert 'localStorage.getItem("mnemos_last_project")' in body


def test_remember_project_helper_exposed():
    body = _read(_STATIC / "ui.js")
    assert "rememberProject: rememberProject" in body
    assert 'localStorage.setItem("mnemos_last_project"' in body


def test_graph_and_findings_call_remember_project():
    """The palette's project context is only useful if pages
    actually record which project the operator last looked at."""
    graph = _read(_TPL / "graph.html")
    findings = _read(_TPL / "findings.html")
    assert "MnemosUI.rememberProject(pid)" in graph
    assert "MnemosUI.rememberProject(pid)" in findings


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def test_palette_renders_node_and_finding_kind_badges():
    body = _read(_STATIC / "ui.js")
    # The kind badge covers node + finding hits, not just project.
    assert '"node"' in body
    assert '"finding"' in body
    assert "cmdk-kind-" in body


def test_kind_badges_colour_coded():
    body = _read(_STATIC / "app.css")
    assert ".cmdk-kind-node" in body
    assert ".cmdk-kind-finding" in body
    assert ".cmdk-kind-project" in body


def test_node_hit_links_to_graph_tab():
    """Clicking a node search result must open the graph tab for
    that project — the operator wants to *see* the node."""
    body = _read(_STATIC / "ui.js")
    rs_idx = body.find("function _remoteSearch(")
    slab = body[rs_idx:rs_idx + 2000]
    assert '"/graph?project=' in slab


def test_finding_hit_links_to_findings_tab():
    body = _read(_STATIC / "ui.js")
    rs_idx = body.find("function _remoteSearch(")
    slab = body[rs_idx:rs_idx + 2000]
    assert '"/findings?project=' in slab
