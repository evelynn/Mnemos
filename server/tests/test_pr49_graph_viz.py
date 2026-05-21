"""PR-49 — knowledge graph visualisation (closes value-audit gap #3).

The post-product critical review found that Mnemos stored a
100K-node knowledge graph but never *showed* it to a human. The
new ``/graph`` tab is the operator's window into the graph: a
component-level force-directed map with certainty + exercised
colouring.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_TPL = _APP / "dashboard" / "templates"
_STATIC = _APP / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# API endpoints — component_map + certainty_breakdown
# ---------------------------------------------------------------------------


def test_component_map_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/graph/component_map"' in body
    assert "async def graph_component_map(" in body


def test_component_map_only_returns_intra_set_edges():
    """An edge whose endpoint lies outside the truncated node set
    must NOT be returned — otherwise the visualizer draws arrows
    pointing into the void."""
    body = _read(_APP / "api" / "analysis.py")
    assert "Edge.source_id.in_(node_ids)" in body
    assert "Edge.target_id.in_(node_ids)" in body


def test_component_map_surfaces_exercised_flag():
    """PR-25's OTLP Tier 2 marks exercised edges; the visualizer
    needs that flag to render them solid vs dashed."""
    body = _read(_APP / "api" / "analysis.py")
    assert '"exercised":' in body
    assert '_exercised(' in body


def test_component_map_returns_truncated_flag():
    """A "Graph truncated — show more" UI hint depends on this."""
    body = _read(_APP / "api" / "analysis.py")
    assert '"truncated":' in body
    assert "len(nodes) >= limit" in body


def test_certainty_breakdown_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/graph/certainty_breakdown"' in body
    assert "async def graph_certainty_breakdown(" in body


def test_certainty_breakdown_groups_by_certainty():
    """The breakdown groups nodes + edges by the certainty column
    so a "12% inferred" badge actually has the data behind it."""
    body = _read(_APP / "api" / "analysis.py")
    assert "group_by(Node.certainty)" in body
    assert "group_by(Edge.certainty)" in body


def test_component_map_limit_is_clamped():
    """Operator can ask for up to 1000 nodes; below that floors
    at 10 so a typo doesn't break the visualizer."""
    body = _read(_APP / "api" / "analysis.py")
    assert "max(10, min(limit, 1000))" in body


# ---------------------------------------------------------------------------
# /graph dashboard tab
# ---------------------------------------------------------------------------


def test_graph_tab_template_present():
    p = _TPL / "graph.html"
    assert p.exists()
    body = _read(p)
    assert 'data-i18n="Knowledge graph"' in body


def test_graph_tab_registered_in_router():
    body = _read(_APP / "dashboard" / "router.py")
    assert '"graph"' in body


def test_sidebar_links_to_graph():
    body = _read(_TPL / "_layout.html")
    assert 'href="/graph"' in body


def test_graph_renders_svg_canvas_not_canvas():
    """SVG keeps zero JS bundle weight and lets the operator save
    the diagram with a right-click. The audit suggested Canvas/WebGL
    only when >1000 nodes — that's a Phase-3 swap."""
    body = _read(_TPL / "graph.html")
    assert '<svg id="gcanvas"' in body
    assert "<marker id=\"arrow\"" in body


def test_graph_uses_force_directed_layout():
    body = _read(_TPL / "graph.html")
    # Repulsion + spring + damping — the three primitives of a
    # basic force-directed layout.
    assert "Repulsion" in body
    assert "spring" in body or "Edge spring" in body
    assert "damp" in body


def test_graph_layout_caps_iterations_for_responsiveness():
    """A naïve force loop on 1000 nodes is 1M operations per
    iteration. The 120-iteration cap keeps the worst case under
    a second on a modern laptop."""
    body = _read(_TPL / "graph.html")
    assert "ITER = 120" in body


def test_graph_node_classes_reflect_certainty():
    body = _read(_TPL / "graph.html")
    # CSS classes for the three certainty values.
    for cls in (".node-circle.verified", ".node-circle.asserted",
                ".node-circle.inferred"):
        assert cls in body


def test_graph_exercised_edges_styled_distinctly():
    body = _read(_TPL / "graph.html")
    assert "edge.exercised" in body
    # Solid + brighter for exercised; dashed for inferred.
    assert "stroke-dasharray" in body


def test_graph_tooltip_shows_node_context():
    """Hovering a node must surface the id + kind + certainty +
    outgoing-edge count — the operator's first question when they
    see an unfamiliar circle."""
    body = _read(_TPL / "graph.html")
    assert "_showGraphTip" in body
    assert "outgoing edge" in body


def test_graph_honours_project_query_param():
    """The dashboard's stat-card "Open Graph →" link points at
    ``/graph?project=…``. The page must accept the param and
    auto-render."""
    body = _read(_TPL / "graph.html")
    assert 'URLSearchParams(window.location.search).get("project")' in body


def test_graph_stats_panel_shows_coverage_metric():
    """Coverage metric was the audit's C3 gap — "how trustworthy
    is our graph?". The four stat cards (Nodes / Edges /
    Verified% / Exercised%) answer that at a glance."""
    body = _read(_TPL / "graph.html")
    assert 'id="g-node-count"' in body
    assert 'id="g-edge-count"' in body
    assert 'id="g-verified-pct"' in body
    assert 'id="g-exercised-pct"' in body


def test_graph_form_uses_localised_labels():
    body = _read(_TPL / "graph.html")
    for tag in ("Project ID", "Node kind", "Max nodes", "Render", "Re-layout"):
        assert f'data-i18n="{tag}"' in body


def test_phrase_book_translates_graph_strings():
    body = _read(_STATIC / "ui.js")
    for kr in (
        "그래프",
        "지식 그래프",
        "최대 노드 수",
        "검증됨 %",
        "실행됨 %",
    ):
        assert kr in body, f"phrase book missing {kr!r}"
