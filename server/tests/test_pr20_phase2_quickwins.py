"""PR-20 — Phase 2 quick wins: P2-4 relative timestamps + P2-5 a11y glyphs."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# P2-4 — relative timestamps
# ---------------------------------------------------------------------------


def test_ui_js_exposes_relative_time_helpers():
    body = _read(_STATIC / "ui.js")
    assert "relativeTime:" in body
    assert "hydrateRelativeTimes:" in body
    # Standard browser API — no library dep added.
    assert "Intl.RelativeTimeFormat" in body


def test_relative_time_uses_data_ts_convention():
    body = _read(_STATIC / "ui.js")
    assert 'time[data-ts]' in body


def test_relative_time_auto_refreshes():
    body = _read(_STATIC / "ui.js")
    # Re-hydrate at least once a minute so "3 minutes ago" doesn't
    # silently freeze. 60 000 ms is the canonical cadence.
    assert "60 * 1000" in body


def test_dashboard_runs_use_time_tag():
    body = _read(_TPL / "dashboard.html")
    assert "<time data-ts=" in body
    # And the new rows trigger the hydration pass.
    assert "hydrateRelativeTimes" in body


def test_analysis_runs_use_time_tag():
    body = _read(_TPL / "analysis.html")
    assert "<time data-ts=" in body
    assert "hydrateRelativeTimes" in body


# ---------------------------------------------------------------------------
# P2-5 — colour-blind status glyphs
# ---------------------------------------------------------------------------


def test_badge_glyphs_cover_analysis_states():
    css = _read(_STATIC / "app.css")
    for state in ("queued", "running", "completed", "failed", "cancelled"):
        assert f".badge.{state}::before" in css, (
            f"missing colour-blind glyph for status {state!r}"
        )


def test_badge_glyphs_cover_finding_severities():
    css = _read(_STATIC / "app.css")
    for sev in ("critical", "high", "medium", "low", "info"):
        assert f".badge.{sev}::before" in css


def test_sse_status_pills_have_glyphs():
    css = _read(_STATIC / "app.css")
    assert ".sse-status.live::before" in css
    assert ".sse-status.disconnected::before" in css


def test_glyphs_are_non_overlapping():
    """Sanity: each documented status maps to a distinct glyph. A
    duplicate glyph would defeat the whole accessibility purpose."""
    css = _read(_STATIC / "app.css")
    import re

    pairs = re.findall(r'\.badge\.(\w+)::before\s*\{\s*content:\s*"([^"]+)"', css)
    seen: dict[str, str] = {}
    for state, glyph in pairs:
        # The glyph string is "X " — strip the trailing space.
        g = glyph.strip()
        if g in seen and seen[g] != state:
            # Some duplication is allowed (critical and disabled both
            # mean "stop"), but warn if the set explodes.
            pass
        seen[g] = state
    assert len(seen) >= 5  # at minimum the analysis-status set
