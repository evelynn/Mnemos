"""PR-23 — P2-8 SSE cross-tab status broadcast."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_layout_contains_cross_tab_strip():
    body = _read(_TPL / "_layout.html")
    assert 'id="sse-cross-tab-strip"' in body
    # ``role`` + ``aria-live`` so screen readers announce the
    # disconnect, not silently.
    assert 'role="status"' in body
    assert 'aria-live="polite"' in body
    # The strip starts hidden — only the broadcast reveals it.
    assert "hidden" in body


def test_ui_js_exposes_publish_helper():
    body = _read(_STATIC / "ui.js")
    assert "publishSseState" in body
    # BroadcastChannel is the right primitive (vs. localStorage event
    # polling) — same-origin tabs only, no network round-trip.
    assert 'BroadcastChannel("mnemos-sse")' in body


def test_ui_js_handles_missing_broadcast_channel():
    body = _read(_STATIC / "ui.js")
    # Some embedded browsers (older WebViews) lack BroadcastChannel.
    # The publish path must no-op gracefully.
    assert 'typeof BroadcastChannel === "undefined"' in body


def test_ui_js_strip_stays_hidden_on_analysis():
    """Showing both the in-tab badge *and* the cross-tab strip on
    /analysis would just be visual noise."""
    body = _read(_STATIC / "ui.js")
    assert 'window.location.pathname === "/analysis"' in body
    # The same handler hides the strip on a ``live`` broadcast — a
    # persistent green strip on every page would be clutter.
    assert '"live"' in body


def test_analysis_publishes_on_state_change():
    body = _read(_TPL / "analysis.html")
    # Every call to ``_markSseStatus`` also broadcasts. The function
    # itself is the single source of truth.
    assert "publishSseState" in body


def test_analysis_stop_publishes_idle():
    body = _read(_TPL / "analysis.html")
    # Manual Stop → ``idle`` so other tabs clear the strip even when
    # nothing actually disconnected.
    assert '"idle"' in body


def test_css_styles_cross_tab_strip():
    css = _read(_STATIC / "app.css")
    assert "#sse-cross-tab-strip" in css
    # Sticky-top placement so it stays visible while the operator
    # scrolls a long table.
    assert "position: sticky" in css
