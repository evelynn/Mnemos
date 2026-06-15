"""PR-40 — design tokens + dark mode + theme switcher.

Closes the 13th-round audit's UI/UX B1 (no CSS-variable token
system) and B3 (no dark mode). The user-facing change is a
3-button sidebar switcher (Light / Auto / Dark); the structural
change is that every colour now flows from a CSS variable so a
future theme swap edits one block.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B1 — design tokens declared
# ---------------------------------------------------------------------------


def test_design_tokens_block_declared_at_top():
    body = _read(_STATIC / "app.css")
    assert ":root {" in body
    # The core colour tokens the audit named.
    for token in (
        "--bg:",
        "--surface:",
        "--surface-alt:",
        "--border:",
        "--text:",
        "--text-muted:",
        "--accent:",
        "--success:",
        "--warn:",
        "--danger:",
        "--sidebar-bg:",
    ):
        assert token in body, f"missing token {token!r}"


def test_spacing_scale_defined():
    body = _read(_STATIC / "app.css")
    for sp in ("--sp-1:", "--sp-2:", "--sp-3:", "--sp-4:", "--sp-6:"):
        assert sp in body


def test_radius_and_shadow_tokens_defined():
    body = _read(_STATIC / "app.css")
    for tok in ("--radius-sm:", "--radius-md:", "--radius-lg:",
                "--shadow-card:", "--shadow-modal:", "--shadow-toast:"):
        assert tok in body


def test_font_tokens_defined():
    body = _read(_STATIC / "app.css")
    assert "--font-body:" in body
    assert "--font-mono:" in body


# ---------------------------------------------------------------------------
# B3 — dark theme block + prefers-color-scheme fallback
# ---------------------------------------------------------------------------


def test_dark_theme_block_present():
    body = _read(_STATIC / "app.css")
    # Operator-chosen dark.
    assert ':root[data-theme="dark"]' in body


def test_dark_theme_overrides_all_core_tokens():
    """Every token declared in the light :root must have a paired
    override in the dark block — otherwise the dark theme would
    inherit a light-themed border on a dark surface and look broken.
    """
    body = _read(_STATIC / "app.css")
    light_start = body.find(":root {")
    light_end = body.find("}", light_start)
    light_block = body[light_start:light_end]
    dark_start = body.find(':root[data-theme="dark"]')
    dark_end = body.find("}", dark_start)
    dark_block = body[dark_start:dark_end]

    # Every ``--color-name:`` defined in the light block has a
    # matching declaration in the dark block. Spacing / font / radius
    # tokens are theme-agnostic and don't need overrides.
    import re

    light_colours = set(re.findall(r"(--[a-z][\w-]*):\s*#", light_block))
    missing = [c for c in light_colours if c not in dark_block]
    assert not missing, f"dark theme missing overrides for {missing!r}"


def test_prefers_color_scheme_fallback_for_no_pref():
    """If the operator hasn't picked a theme yet, respect the OS.
    Implemented as ``:root:not([data-theme])`` inside the media
    query so the JS toggle still wins when an explicit preference
    is set."""
    body = _read(_STATIC / "app.css")
    assert "@media (prefers-color-scheme: dark)" in body
    assert ":root:not([data-theme])" in body


# ---------------------------------------------------------------------------
# Hex-literal cleanup — the most-used colours are now variables
# ---------------------------------------------------------------------------


def test_main_palette_hex_literals_are_gone_below_token_block():
    """Below the design-token block, the bulk hex literals
    ``#1f6feb`` / ``#d0d7de`` / ``#f6f8fa`` / ``#1f2328`` etc.
    should be replaced by ``var(--*)``. The token block itself
    intentionally keeps the hex defs."""
    body = _read(_STATIC / "app.css")
    # Slice off the token + dark-block prelude.
    marker = "* { box-sizing: border-box; }"
    idx = body.find(marker)
    assert idx > 0
    after_tokens = body[idx:]
    # The most-recycled colours from the legacy stylesheet — none
    # should appear below the token block any more.
    for hex_literal in ("#1f6feb", "#d0d7de", "#f6f8fa", "#1f2328", "#6b7178"):
        assert hex_literal not in after_tokens, (
            f"{hex_literal!r} still appears below the token block — "
            "rewrite that selector to use a var(--*) instead"
        )


# ---------------------------------------------------------------------------
# Theme switcher UI
# ---------------------------------------------------------------------------


def test_theme_switcher_has_three_buttons_in_layout():
    body = _read(_TPL / "_layout.html")
    assert 'class="theme-switcher"' in body
    for pref in ("light", "auto", "dark"):
        assert f'data-theme-pref="{pref}"' in body


def test_theme_switcher_buttons_have_aria_labels():
    body = _read(_TPL / "_layout.html")
    for label in ("Light theme", "System default theme", "Dark theme"):
        assert f'aria-label="{label}"' in body


def test_theme_switcher_persists_to_localstorage():
    body = _read(_TPL / "_layout.html")
    assert 'localStorage.setItem("mnemos_theme"' in body
    # ``auto`` should *remove* the explicit preference so the OS
    # setting takes over via the prefers-color-scheme media query.
    assert 'localStorage.removeItem("mnemos_theme")' in body


def test_theme_switcher_toggles_html_data_attribute():
    body = _read(_TPL / "_layout.html")
    assert 'document.documentElement.setAttribute("data-theme"' in body
    assert 'document.documentElement.removeAttribute("data-theme")' in body


# ---------------------------------------------------------------------------
# FOUC defence — the saved theme is applied BEFORE the stylesheet
# loads, so the first paint isn't in the wrong colour scheme.
# ---------------------------------------------------------------------------


def test_pre_paint_theme_application_in_base_template():
    body = _read(_TPL / "base.html")
    # The inline script must appear before the <link rel="stylesheet">
    # so the variable override is in place when the cascade applies.
    script_idx = body.find('localStorage.getItem("mnemos_theme")')
    # The href is cache-busted via asset_url() (PR-165), so anchor on the
    # stable rel="stylesheet" marker rather than the literal path.
    stylesheet_idx = body.find('rel="stylesheet"')
    assert 0 < script_idx < stylesheet_idx


# ---------------------------------------------------------------------------
# CSS-side wiring sanity — the .sidebar uses the sidebar-* tokens,
# not the generic --text token (which would have made dark-mode
# sidebar invisible against the dark page background).
# ---------------------------------------------------------------------------


def test_sidebar_uses_dedicated_tokens():
    body = _read(_STATIC / "app.css")
    sidebar_idx = body.find(".sidebar {")
    end = body.find("}", sidebar_idx)
    slab = body[sidebar_idx:end]
    assert "var(--sidebar-bg)" in slab, (
        ".sidebar must use --sidebar-bg, not --text — otherwise dark "
        "mode would render the sidebar identical to the page bg"
    )
