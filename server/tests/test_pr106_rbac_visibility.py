"""PR-106 — RBAC visibility in the dashboard.

Pre-PR pattern: pages either rendered admin-/operator-only buttons
unconditionally (so a viewer clicked them and got a 403 toast) or
hand-rolled ``window.MNEMOS_USER_ROLE_HINT === 'admin'`` checks
inline (so the page silently dropped the button — confusing
because the viewer had no clue what they were missing).

This PR centralises the decision in ``MnemosUI.gateByRole(scope?)``.
An element tagged ``data-required-role="admin"`` (or operator)
becomes disabled with a tooltip naming the role when the user
lacks it. Critically the element is still RENDERED — a viewer
should *see* the affordance reserved for higher roles, so they
know what they'd be able to do with elevated privileges. Silent
drops hide functionality and frustrate users who don't know what
they're missing.

Backend authorisation is unchanged — ``require_admin`` /
``require_operator`` still enforces. This layer is UX.
"""

from __future__ import annotations

from pathlib import Path

_BASE = Path(__file__).resolve().parents[1] / "app" / "dashboard"


def _read(rel: str) -> str:
    return (_BASE / rel).read_text(encoding="utf-8")


def test_gatebyrole_function_exists():
    js = _read("static/ui.js")
    assert "function gateByRole(" in js
    # Auto-run on DOMContentLoaded so a viewer never sees enabled
    # admin buttons mid-page-load.
    assert "gateByRole();" in js


def test_gatebyrole_exported_on_namespace():
    js = _read("static/ui.js")
    # The closure must export it on ``window.MnemosUI`` so dynamic
    # renderers (innerHTML loaders) can call it with a scope.
    idx = js.find("window.MnemosUI = {")
    slab = js[idx:idx + 1500]
    assert "gateByRole: gateByRole" in slab


def test_role_rank_orders_admin_above_operator_above_viewer():
    js = _read("static/ui.js")
    idx = js.find("_ROLE_RANK")
    slab = js[idx:idx + 200]
    # Numeric ordering must match real backend role hierarchy.
    assert "viewer: 1" in slab
    assert "operator: 2" in slab
    assert "admin: 3" in slab


def test_role_check_uses_rank_not_equality():
    """If gateByRole used ``actual === required`` an admin would
    fail an operator-required check. The helper must compare ranks
    so admin satisfies operator, operator satisfies viewer, etc."""
    js = _read("static/ui.js")
    idx = js.find("function _roleAllows(")
    slab = js[idx:idx + 300]
    # The check is rank >= rank, not equality.
    assert ">=" in slab
    assert "_ROLE_RANK[actual]" in slab
    assert "_ROLE_RANK[required]" in slab


def test_gate_preserves_original_tooltip_for_restoration():
    """If a user's role is upgraded mid-session (or the helper is
    re-run after a UI state change) the original title attribute
    must come back — we mustn't permanently replace it with the
    role-required message."""
    js = _read("static/ui.js")
    idx = js.find("function gateByRole(")
    slab = js[idx:idx + 2000]
    # The "save original title" + restore path.
    assert "gatedOriginalTitle" in slab
    assert "el.title = el.dataset.gatedOriginalTitle" in slab


def test_gate_disables_buttons_and_adds_aria():
    """A screen-reader user must hear the disabled state, not just
    see it visually. ``aria-disabled="true"`` is the WCAG signal."""
    js = _read("static/ui.js")
    idx = js.find("function gateByRole(")
    slab = js[idx:idx + 2000]
    assert 'setAttribute("aria-disabled", "true")' in slab
    assert "el.disabled = true" in slab
    # And the visible-state class for CSS to dim it.
    assert 'classList.add("role-gated")' in slab


def test_css_styles_role_gated_class():
    css = _read("static/app.css")
    assert ".role-gated" in css
    # Visually distinct — dimmed + cursor change so users understand
    # they can't act on it.
    assert "cursor: not-allowed" in css


def test_korean_tooltip_phrase_registered():
    js = _read("static/ui.js")
    # Templated phrase with $1 substitution — the helper passes the
    # capitalised role name.
    assert '"$1 role required.":' in js
    assert "권한이 필요합니다" in js


def test_findings_rebuild_button_marked_operator():
    """Concrete adoption — the Rebuild button on /findings calls a
    POST /findings/rebuild endpoint guarded by require_operator.
    The viewer should see it dimmed, not get a 403."""
    html = _read("templates/findings.html")
    assert 'data-required-role="operator"' in html
    # Specifically the Rebuild button (not Export, not Load).
    assert 'onclick="rebuild()" data-required-role="operator"' in html


def test_diffs_action_buttons_marked_operator():
    """Concrete adoption — Approve / Reject on a diff submission
    require operator. The rendering uses innerHTML so the templates
    must also re-gate dynamically after the render."""
    html = _read("templates/diffs.html")
    # Both action buttons tagged.
    assert html.count('data-required-role="operator"') >= 2
    # And the post-render re-gate is wired up.
    assert "MnemosUI.gateByRole(out);" in html


def test_helper_handles_missing_role_hint_safely():
    """If window.MNEMOS_USER_ROLE_HINT is unset (e.g. the user
    isn't authenticated yet on a login redirect), the helper must
    treat it as the lowest role rather than crash. Otherwise the
    login page can't render."""
    js = _read("static/ui.js")
    idx = js.find("function _userRoleHint(")
    slab = js[idx:idx + 400]
    # Falls back to empty string when undefined — the rank lookup
    # then returns 0 (no role), failing every check safely.
    assert '|| ""' in slab
