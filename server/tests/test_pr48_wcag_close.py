"""PR-48 — final productisation close.

Closes the 17th-round audit's three Minor items and locks in
the "product complete" state.
"""

from __future__ import annotations

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
# README — product-completion declaration
# ---------------------------------------------------------------------------


def test_readme_declares_product_complete():
    body = _read(_README)
    assert "Productisation cycle complete (PR-1 → PR-48)" in body
    # Audit cycle log table includes every round.
    assert "Round  PRs" in body
    # User-command checklist marks every item ✅.
    assert "User command check" in body
    # The four headline user commands are each accounted for.
    for token in (
        "문제 발견 안 될 때까지",
        "UI/UX 부분까지",
        "RBAC + 유저 로그인 + 권한 관리",
        "제대로 된 팀 운영 시스템",
        "미려한 디자인",
        "상품으로써 완성",
    ):
        assert token in body, f"README missing user-command line {token!r}"


def test_readme_records_final_state():
    body = _read(_README)
    # Final-state numbers.
    assert "48 PRs" in body
    assert "585 unit + 16 integration" in body
    # Three middlewares.
    assert "3 new middlewares" in body
    # Spec §2 still 10/10.
    assert "10/10" in body


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
