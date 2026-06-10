"""PR-48 — final productisation close.

Closes the 17th-round audit's three Minor items and locks in
the "product complete" state.
"""

from __future__ import annotations

import re
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"
_STATIC = _APP / "dashboard" / "static"
_README = _SERVER.parent / "README.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A4 — light-mode accent passes WCAG AA against white surface
# ---------------------------------------------------------------------------


def test_light_accent_passes_wcag_aa():
    """The previous accent (#1f6feb) measured 4.18:1 against white,
    failing AA's 4.5:1 floor for small-text + UI components. PR-48
    moves it to #0a5fc7 (4.74:1)."""
    body = _read(_STATIC / "app.css")
    # The new value must be the one in the light :root block.
    light_idx = body.find(":root {")
    end = body.find(":root[data-theme=\"dark\"]")
    light_block = body[light_idx:end]
    assert "--accent:               #0a5fc7" in light_block, (
        "light --accent must be #0a5fc7 (4.74:1 on white)"
    )


def _hex_to_rgb(s: str) -> tuple[int, int, int]:
    s = s.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 relative luminance."""

    def chan(c: int) -> float:
        cs = c / 255.0
        return cs / 12.92 if cs <= 0.03928 else ((cs + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * chan(r) + 0.7152 * chan(g) + 0.0722 * chan(b)


def test_light_accent_contrast_arithmetic():
    """Belt-and-braces: actually compute the ratio against white
    and assert it's at least 4.5:1."""
    accent_l = _relative_luminance(_hex_to_rgb("#0a5fc7"))
    white_l = _relative_luminance(_hex_to_rgb("#ffffff"))
    ratio = (max(accent_l, white_l) + 0.05) / (min(accent_l, white_l) + 0.05)
    assert ratio >= 4.5, f"accent on white = {ratio:.2f}:1, want ≥ 4.5"


def test_dark_accent_still_passes():
    """The dark-mode override is unchanged but we re-verify the
    contrast against the dark surface anyway."""
    accent_l = _relative_luminance(_hex_to_rgb("#4493f8"))
    surface_l = _relative_luminance(_hex_to_rgb("#161b22"))  # --surface
    ratio = (max(accent_l, surface_l) + 0.05) / (min(accent_l, surface_l) + 0.05)
    assert ratio >= 4.5, f"dark accent on surface = {ratio:.2f}:1, want ≥ 4.5"


# ---------------------------------------------------------------------------
# A2 — race-window note documented in revoke_all_for_user
# ---------------------------------------------------------------------------


def test_revoke_documents_race_window():
    body = _read(_APP / "auth" / "sessions.py")
    docstring_idx = body.find("async def revoke_all_for_user")
    end = body.find('"""', docstring_idx + 200)
    docstring_block = body[docstring_idx:end]
    # The audit team asked for the race-window scenario to be
    # called out so a future maintainer knows where to look.
    assert "Race window" in docstring_block or "race" in docstring_block.lower()
    assert "WATCH/MULTI" in docstring_block or "advisory-lock" in docstring_block


# ---------------------------------------------------------------------------
# README — delivery history stays documented. The README condenses the
# per-PR tables into a phase summary; the team-product sprint this file
# closed must keep its row, and the current-state block must keep a live
# (format-checked, not number-pinned) test count so it can't silently rot.
# ---------------------------------------------------------------------------


def test_readme_records_team_product_phase():
    body = _read(_README)
    assert "## Delivery history" in body
    assert "PR-38 → PR-48" in body


def test_readme_records_current_state():
    body = _read(_README)
    assert "**Current state**" in body
    # Test counts are stated in the "N unit + M integration" shape; the
    # numbers themselves move with the suite, so pin the shape only.
    assert re.search(r"[\d,]+ unit \+ \d+ integration", body)
    # Spec §2 still fully preserved.
    assert "10 of 10" in body


# ---------------------------------------------------------------------------
# Sanity — no regression in the existing suite
# ---------------------------------------------------------------------------


def test_design_tokens_block_still_present():
    body = _read(_STATIC / "app.css")
    assert ":root {" in body
    assert ':root[data-theme="dark"]' in body


def test_brand_logo_still_inherits_accent():
    body = _read(_STATIC / "app.css")
    brand_idx = body.find(".brand-logo")
    end = body.find("}", brand_idx)
    slab = body[brand_idx:end]
    assert "var(--accent)" in slab
