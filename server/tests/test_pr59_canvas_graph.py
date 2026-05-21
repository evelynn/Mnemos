"""PR-59 — Canvas backend for large knowledge graphs (core-value D2).

PR-49 shipped an SVG force-directed graph that the audit signed off
for the ~200-node component map. The reassessment kept D2 short of
target: a 1000-node project would emit thousands of <g>/<circle>/
<text> DOM elements and stall layout. This PR keeps the SVG backend
for small graphs and adds a Canvas 2D backend for large ones, picked
automatically by node count.
"""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# graph.html — canvas element + backend selection
# ---------------------------------------------------------------------------


def test_graph_has_canvas_element():
    body = _read(_TPL / "graph.html")
    assert '<canvas id="gcanvas-2d"' in body
    # Hidden until a large graph selects the canvas backend.
    assert 'id="gcanvas-2d" hidden' in body


def test_graph_keeps_svg_backend():
    """The SVG element must survive — small graphs still use it."""
    body = _read(_TPL / "graph.html")
    assert '<svg id="gcanvas"' in body
    assert "function renderGraphSvg(" in body


def test_render_branches_on_node_count():
    """renderGraph() picks SVG vs Canvas by node count against the
    CANVAS_THRESHOLD constant."""
    body = _read(_TPL / "graph.html")
    assert "CANVAS_THRESHOLD" in body
    idx = body.find("function renderGraph(")
    slab = body[idx:idx + 600]
    assert "_gState.nodes.length > CANVAS_THRESHOLD" in slab
    assert "renderGraphCanvas()" in slab
    assert "renderGraphSvg()" in slab


def test_render_toggles_visibility_of_backends():
    """Only one backend may be visible / hit-tested at a time."""
    body = _read(_TPL / "graph.html")
    idx = body.find("function renderGraph(")
    slab = body[idx:idx + 600]
    assert 'getElementById("gcanvas").hidden = useCanvas' in slab
    assert 'getElementById("gcanvas-2d").hidden = !useCanvas' in slab


# ---------------------------------------------------------------------------
# Canvas 2D backend
# ---------------------------------------------------------------------------


def test_canvas_backend_defined():
    body = _read(_TPL / "graph.html")
    assert "function renderGraphCanvas(" in body
    assert 'getContext("2d")' in body


def test_canvas_honours_device_pixel_ratio():
    """Crisp rendering on HiDPI — internal resolution scaled by dpr."""
    body = _read(_TPL / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 900]
    assert "devicePixelRatio" in slab
    assert "setTransform(dpr" in slab


def test_canvas_draws_edges_then_nodes():
    """Edges first so node discs draw on top — same z-order as SVG."""
    body = _read(_TPL / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 3600]
    edge_at = slab.find("for (const e of _gState.edges)")
    node_at = slab.find("for (const n of _gState.nodes)")
    assert edge_at != -1 and node_at != -1
    assert edge_at < node_at
    assert "ctx.arc(" in slab


def test_canvas_reads_theme_colours():
    """Colours come from CSS custom properties so dark mode renders."""
    body = _read(_TPL / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 3600]
    assert "getComputedStyle(document.documentElement)" in slab
    assert "--accent" in slab


def test_canvas_hit_test_for_tooltip():
    """The canvas has no DOM nodes, so hover uses a pointer hit-test
    that maps client coords back into the 800×600 layout space."""
    body = _read(_TPL / "graph.html")
    assert "function _canvasHover(" in body
    idx = body.find("function _canvasHover(")
    slab = body[idx:idx + 900]
    assert "getBoundingClientRect()" in slab
    assert "_showGraphTip(" in slab
    # Listener wired to mousemove on the canvas.
    assert '.addEventListener("mousemove", _canvasHover)' in body


# ---------------------------------------------------------------------------
# Layout — iteration trimming for large graphs
# ---------------------------------------------------------------------------


def test_layout_trims_iterations_for_large_graphs():
    """The O(n²) refinement step count is reduced past 400 nodes so a
    1000-node layout still settles quickly."""
    body = _read(_TPL / "graph.html")
    assert "nodes.length > 400 ? 60 : 120" in body


# ---------------------------------------------------------------------------
# CSS + i18n
# ---------------------------------------------------------------------------


def test_canvas_styled():
    body = _read(_TPL / "graph.html")
    assert "#gcanvas-2d {" in body


def test_canvas_note_translatable():
    body = _read(_TPL / "graph.html")
    assert 'id="g-canvas-note"' in body
    assert 'data-i18n="Large graph' in body


def test_phrase_book_translates_canvas_note():
    body = _read(_STATIC / "ui.js")
    assert "대규모 그래프" in body
    assert "캔버스" in body
