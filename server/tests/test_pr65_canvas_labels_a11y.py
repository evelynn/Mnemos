"""PR-65 — Canvas backend visual parity + graph a11y (core-value D).

The post-PR-63 reassessment flagged the Canvas backend (PR-59) as
visually degraded vs SVG: it drew no node labels and no edge
arrowheads, so above 200 nodes an operator saw unlabelled discs with
no edge direction. Neither backend had a text alternative for screen
readers. This PR adds canvas labels + arrowheads and an aria-live
text summary of the graph.
"""

from __future__ import annotations

from pathlib import Path

_DASH = Path(__file__).resolve().parents[1] / "app" / "dashboard"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Canvas — arrowheads + labels
# ---------------------------------------------------------------------------


def test_canvas_draws_edge_arrowheads():
    body = _read(_DASH / "templates" / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 3600]
    # A filled triangle at the target end conveys edge direction.
    assert "Arrowhead" in slab
    assert "closePath()" in slab
    assert "ctx.fill()" in slab


def test_canvas_draws_node_labels():
    body = _read(_DASH / "templates" / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 3600]
    assert "fillText(" in slab


def test_canvas_labels_thinned_on_dense_graphs():
    """All-node labelling only on a sparse graph — otherwise just the
    Component nodes, so the text doesn't turn to mush."""
    body = _read(_DASH / "templates" / "graph.html")
    idx = body.find("function renderGraphCanvas(")
    slab = body[idx:idx + 3600]
    assert "labelAll" in slab
    assert 'n.kind !== "Component"' in slab


# ---------------------------------------------------------------------------
# Accessibility — text alternative
# ---------------------------------------------------------------------------


def test_graph_has_aria_live_text_alternative():
    body = _read(_DASH / "templates" / "graph.html")
    assert 'id="g-a11y"' in body
    assert 'aria-live="polite"' in body
    assert "function _updateGraphA11y(" in body
    # The canvas is focusable.
    assert 'id="gcanvas-2d" hidden tabindex="0"' in body


def test_a11y_summary_reports_counts_and_components():
    body = _read(_DASH / "templates" / "graph.html")
    idx = body.find("function _updateGraphA11y(")
    slab = body[idx:idx + 700]
    assert "_gState.nodes.length" in slab
    assert "_gState.edges.length" in slab
    assert 'n.kind === "Component"' in slab


def test_sr_only_class_styled():
    body = _read(_DASH / "static" / "app.css")
    assert ".sr-only {" in body
    assert "clip: rect(0 0 0 0)" in body


def test_phrase_book_has_components_label():
    body = _read(_DASH / "static" / "ui.js")
    assert '"Components": "컴포넌트"' in body
