"""Static asset + template sanity checks (UI audit findings).

Tests here are intentionally simple grep-style assertions. They are
fast (no browser), cover the regressions Team B flagged, and run in
the default `pytest` cycle — heavier Playwright work lives behind
a separate nightly marker.
"""

from __future__ import annotations

from pathlib import Path


_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_focus_style_present():
    """WCAG 2.1 AA — keyboard focus must be visible. The pre-PR-9 stylesheet
    had zero `:focus` rules, so a Tab traversal showed no indicator."""
    css = _read(_STATIC / "app.css")
    assert ":focus-visible" in css, "global :focus-visible rule missing"


def test_toast_and_modal_styles_present():
    css = _read(_STATIC / "app.css")
    assert ".toast" in css
    assert "dialog.modal" in css
    assert ".danger-banner" in css


def test_layout_loads_ui_js():
    base = _read(_TPL / "base.html")
    assert "ui.js" in base, "base.html must include the shared UI helper"


def test_ui_js_exports_helpers():
    js = _read(_STATIC / "ui.js")
    for name in (
        "showToast",
        "showError",
        "renderJson",
        "copyToClipboard",
        "showDialog",
        "closeDialog",
        "escapeHtml",
    ):
        assert name in js, f"ui.js must export {name}"


def test_diffs_html_no_longer_uses_prompt():
    """The 4 prompt() calls in diffs.html were the main UI Critical."""
    diffs = _read(_TPL / "diffs.html")
    # ``prompt(`` is the JS form (window.prompt).
    assert "prompt(" not in diffs, (
        "diffs.html must use <dialog> modals, not window.prompt()"
    )


def test_diffs_html_has_three_dialogs():
    diffs = _read(_TPL / "diffs.html")
    assert 'id="grant-dialog"' in diffs
    assert 'id="token-dialog"' in diffs
    assert 'id="approve-dialog"' in diffs


def test_settings_412_no_longer_silent():
    settings = _read(_TPL / "settings.html")
    assert "dr.ok ? await dr.json() : []" not in settings, (
        "settings.html must not swallow 412/500 into an empty list"
    )
    assert "Failed to load DB bindings" in settings


def test_claude_chat_ui_does_not_advertise_key_free_subscription():
    settings = _read(_TPL / "settings.html")
    chat = _read(_TPL / "chat.html")
    assert "Uses this machine's Claude Code subscription" not in settings
    assert "Anthropic API key required" in settings
    assert "full 128K output ceiling" in settings
    assert "Agent SDK-only" in chat
    assert "not advertised as available" in chat


def test_gdpr_uses_class_not_inline_style():
    gdpr = _read(_TPL / "gdpr.html")
    assert 'style="background:#c62828' not in gdpr
    assert "class=\"danger\"" in gdpr or "class='danger'" in gdpr


def test_token_dialog_has_copy_button_and_countdown():
    diffs = _read(_TPL / "diffs.html")
    assert "copyTokenToClipboard" in diffs
    assert "token-countdown" in diffs


def test_alert_calls_removed_from_diffs():
    """Raw ``alert(await r.text())`` was the JSON-dump UX problem."""
    diffs = _read(_TPL / "diffs.html")
    assert "alert(await" not in diffs
