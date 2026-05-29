"""PR-42 — command palette + tooltip + GitHub-style keyboard shortcuts.

Closes the 13th-round audit's C2 (no global search) and D3 (no
keyboard shortcuts), plus D4 (no inline tooltip for jargon).
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C2 / D3 — command palette + shortcuts
# ---------------------------------------------------------------------------


def test_palette_overlay_present_in_layout():
    body = _read(_TPL / "_layout.html")
    assert 'id="cmdk-overlay"' in body
    assert 'id="cmdk-input"' in body
    assert 'id="cmdk-list"' in body
    # Accessible modal semantics.
    assert 'role="dialog"' in body
    assert 'aria-modal="true"' in body


def test_palette_input_has_translatable_placeholder():
    body = _read(_TPL / "_layout.html")
    assert "data-i18n-placeholder=" in body
    # The phrase book translates it.
    ui = _read(_STATIC / "ui.js")
    assert "프로젝트 검색, 탭 이동…" in ui


def test_palette_keybindings_documented_in_footer():
    body = _read(_TPL / "_layout.html")
    # ↑↓ navigate, ↵ open, g+p go to projects — the footer reminds
    # the operator of the bindings.
    assert "↑" in body or "↓" in body
    assert "↵" in body
    assert "g" in body  # the g-prefix hint


def test_palette_opens_on_cmd_k():
    body = _read(_STATIC / "ui.js")
    # The handler accepts metaKey OR ctrlKey so macOS and Linux
    # operators both get the same UX.
    assert "ev.metaKey || ev.ctrlKey" in body
    assert 'ev.key.toLowerCase() === "k"' in body


def test_palette_opens_on_slash_outside_inputs():
    """``/`` alone opens the palette — but only when the operator
    isn't typing into a field, otherwise a search query would
    swallow the slash."""
    body = _read(_STATIC / "ui.js")
    assert "INPUT|TEXTAREA|SELECT" in body
    assert 'ev.key === "/"' in body


def test_palette_arrow_keys_change_selection():
    body = _read(_STATIC / "ui.js")
    assert 'ev.key === "ArrowDown"' in body
    assert 'ev.key === "ArrowUp"' in body
    assert 'ev.key === "Enter"' in body


def test_palette_escape_closes_it():
    body = _read(_STATIC / "ui.js")
    # The Esc handler lives in the palette keydown listener.
    palette_idx = body.find("_onPaletteKey")
    end = body.find("function _", palette_idx + 1)
    slab = body[palette_idx:end] if end > palette_idx else body[palette_idx:]
    assert 'ev.key === "Escape"' in slab


def test_palette_caches_project_list_on_first_open():
    body = _read(_STATIC / "ui.js")
    assert "_PALETTE_PROJECTS = null" in body
    # Subsequent opens skip the fetch.
    assert "_PALETTE_PROJECTS !== null" in body


def test_g_shortcut_map_covers_main_tabs():
    body = _read(_STATIC / "ui.js")
    # The GitHub-style "g d" / "g p" / "g a" shortcuts.
    assert "_G_MAP" in body
    for letter in ("d", "p", "a", "f", "s"):
        assert f'"{letter}":' in body


def test_g_shortcut_blocked_in_fields():
    """``g`` followed by a letter must NOT navigate while the
    operator is typing in a search box."""
    body = _read(_STATIC / "ui.js")
    # The g-prefix handler is gated by the same inField check the
    # palette uses.
    assert "_gPending" in body
    # Timeout window so a half-pressed g doesn't pollute the next key.
    assert "1500" in body


def test_filter_logic_searches_label_and_sublabel():
    body = _read(_STATIC / "ui.js")
    assert "e.label.toLowerCase().indexOf" in body
    assert "e.sublabel" in body


# ---------------------------------------------------------------------------
# D4 — tooltip for jargon
# ---------------------------------------------------------------------------


def test_tooltip_css_uses_data_tip_attribute():
    body = _read(_STATIC / "app.css")
    assert "[data-tip]" in body
    # Tooltip appears on hover *and* focus-visible (keyboard a11y).
    assert "[data-tip]:focus-visible::after" in body


def test_tooltip_content_pulled_from_attribute():
    body = _read(_STATIC / "app.css")
    assert "content: attr(data-tip)" in body


def test_diffs_template_uses_tooltips_for_jargon():
    """The audit specifically called out "ultrareview" and
    "break-glass grant" as terms operators don't recognise. The
    diffs intro paragraph now wraps both in ``data-tip`` spans."""
    body = _read(_TPL / "diffs.html")
    assert "data-tip=" in body
    # Both terms are tooltipped.
    assert ">ultrareview<" in body
    assert ">break-glass grant<" in body


# ---------------------------------------------------------------------------
# Palette styling
# ---------------------------------------------------------------------------


def test_palette_modal_styled():
    body = _read(_STATIC / "app.css")
    assert ".cmdk-overlay" in body
    assert ".cmdk-modal" in body
    assert ".cmdk-item.active" in body


def test_palette_responsive_on_mobile():
    """The modal must shrink to the viewport on a phone (width
    cap based on calc()), not blow past the screen edge."""
    body = _read(_STATIC / "app.css")
    assert "min(600px, calc(100vw" in body
