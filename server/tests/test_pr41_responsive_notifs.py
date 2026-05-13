"""PR-41 — responsive layout, SVG icons, notification centre MVP.

Closes the 13th-round audit's UI/UX B4 (responsive), B8 (icons),
and C1 Critical (notification centre).
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B4 — responsive layout breakpoints
# ---------------------------------------------------------------------------


def test_responsive_breakpoints_defined():
    body = _read(_STATIC / "app.css")
    for bp in ("max-width: 1024px", "max-width: 768px", "max-width: 480px"):
        assert f"@media ({bp})" in body, f"missing breakpoint {bp!r}"


def test_mobile_sidebar_becomes_drawer():
    """At ≤ 768px the sidebar transforms off-screen and only slides
    in when ``.sidebar.open`` is set by the hamburger toggle."""
    body = _read(_STATIC / "app.css")
    assert "transform: translateX(-100%)" in body
    assert ".sidebar.open" in body


def test_hamburger_toggle_present_in_layout():
    body = _read(_TPL / "_layout.html")
    assert 'id="menu-toggle"' in body
    assert 'aria-controls="sidebar"' in body
    assert 'aria-expanded="false"' in body


def test_hamburger_toggle_handler_wired():
    body = _read(_TPL / "_layout.html")
    # The handler must toggle ``.open`` and update aria-expanded.
    assert 'sidebar.classList.toggle("open"' in body
    assert 'setAttribute("aria-expanded"' in body


def test_hamburger_closes_on_escape():
    """Keyboard a11y — a screen-reader user must be able to dismiss
    the drawer without grabbing the mouse."""
    body = _read(_TPL / "_layout.html")
    assert 'ev.key === "Escape"' in body


def test_hamburger_closes_when_nav_link_clicked():
    """On a phone viewport, clicking a nav link to navigate must
    close the drawer so the new page isn't covered."""
    body = _read(_TPL / "_layout.html")
    assert 'sidebar.querySelectorAll("nav a")' in body


def test_scrim_revealed_when_drawer_open():
    """Modal-feel: a semi-transparent scrim sits behind the open
    drawer so the operator clearly knows the rest of the page is
    inert."""
    body = _read(_STATIC / "app.css")
    assert ".sidebar-scrim" in body
    assert ".sidebar.open + .sidebar-scrim" in body


# ---------------------------------------------------------------------------
# B8 — inline-SVG icon system
# ---------------------------------------------------------------------------


def test_ui_js_exposes_icon_helper():
    body = _read(_STATIC / "ui.js")
    assert "function icon(name, opts)" in body
    assert "icon: icon," in body


def test_icon_set_covers_dashboard_essentials():
    body = _read(_STATIC / "ui.js")
    # The icons the dashboard actually uses today.
    for name in ("bell", "check", "x", "warn", "info", "search", "plus", "cog"):
        assert f'{name}:' in body or f'"{name}":' in body, (
            f"icon set missing {name!r}"
        )


def test_icon_uses_currentcolor_so_themes_apply():
    """The SVGs must inherit the CSS ``color`` (via ``stroke=
    'currentColor'``) so dark mode automatically gets light-coloured
    icons without per-theme assets."""
    body = _read(_STATIC / "ui.js")
    assert 'stroke="currentColor"' in body


def test_icon_marks_aria_hidden_for_decorative_use():
    """Icons that pair with visible text labels must hide from
    screen readers to avoid duplicate announcement. Standalone
    icon buttons get an ``aria-label`` on the button instead."""
    body = _read(_STATIC / "ui.js")
    assert 'aria-hidden="true"' in body


# ---------------------------------------------------------------------------
# C1 Critical — notification centre MVP
# ---------------------------------------------------------------------------


def test_notify_api_exposed():
    body = _read(_STATIC / "ui.js")
    for name in ("notify", "readNotifications", "clearNotifications"):
        assert f"{name}:" in body, f"MnemosUI.{name} not exposed"


def test_notify_persists_to_localstorage_with_cap():
    body = _read(_STATIC / "ui.js")
    assert 'localStorage.setItem(_NOTIF_KEY' in body
    assert "_NOTIF_MAX" in body
    # 50 entries is plenty for an MVP — keeps localStorage usage
    # bounded and matches the audit's UX recommendation.
    assert "_NOTIF_MAX = 50" in body


def test_notify_cross_tab_via_broadcast_channel():
    """A notification fired from /analysis must light the bell on
    /findings without a reload."""
    body = _read(_STATIC / "ui.js")
    assert 'BroadcastChannel("mnemos-notifs")' in body


def test_bell_renders_unread_badge():
    body = _read(_STATIC / "ui.js")
    assert "notif-badge" in body
    # 9+ cap so the badge stays the same width regardless of count.
    assert 'unread > 9 ? "9+"' in body


def test_layout_renders_bell_and_panel():
    body = _read(_TPL / "_layout.html")
    assert 'id="notif-bell"' in body
    assert 'id="notif-badge"' in body
    assert 'id="notif-panel"' in body
    assert 'id="notif-list"' in body
    # Clear-all action exists.
    assert 'id="notif-clear"' in body


def test_bell_button_has_aria_label():
    body = _read(_TPL / "_layout.html")
    assert 'aria-label="Notifications"' in body


def test_panel_styled_in_css():
    body = _read(_STATIC / "app.css")
    assert ".notif-panel" in body
    assert ".notif-item.unread" in body
    assert ".notif-empty" in body


def test_unread_item_uses_accent_left_border():
    """Visual cue separate from text — the left-border colour
    matches the notification's level (info=accent, warn=warn,
    error=danger, success=success)."""
    body = _read(_STATIC / "app.css")
    assert "border-left-color: var(--accent)" in body
    assert "border-left-color: var(--danger-button)" in body
    assert "border-left-color: var(--warn)" in body
    assert "border-left-color: var(--success)" in body


def test_analysis_emits_notification_on_run_completion():
    """The first place the platform fires a notification: when an
    analysis run completes or fails on the SSE stream the operator
    might not be looking at."""
    body = _read(_TPL / "analysis.html")
    assert "MnemosUI.notify(" in body
    # Both terminal events surface as inbox entries.
    assert '"run_failed"' in body
    assert '"run_completed"' in body


def test_phrase_book_translates_notification_strings():
    body = _read(_STATIC / "ui.js")
    for kr in ("알림", "모두 지우기", "알림이 없습니다.", "분석 실행 실패"):
        assert kr in body, f"phrase book missing Korean {kr!r}"
