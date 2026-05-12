"""Static asset assertions for the UI a11y / IME / timer fixes (PR-12)."""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_ui_js_exports_new_helpers():
    js = _read(_STATIC / "ui.js")
    for name in (
        "bindRationaleCounter",
        "clockOffsetFromPayload",
        "renderJsonFromScript",
    ):
        assert name in js


def test_assertive_toast_host_for_errors():
    js = _read(_STATIC / "ui.js")
    # Error/warn toasts must announce on a separate aria-live=assertive
    # host so screen readers interrupt for them.
    assert "toast-stack-assertive" in js
    assert 'aria-live"' in js or '"aria-live"' in js


def test_token_dialog_has_onclose_handler():
    """ESC / backdrop click must clear the countdown timer."""
    diffs = _read(_TPL / "diffs.html")
    assert 'onclose="dismissTokenDialog()"' in diffs


def test_grant_rationale_uses_ime_safe_counter():
    diffs = _read(_TPL / "diffs.html")
    assert "bindRationaleCounter" in diffs
    # The inline oninput counter is gone.
    assert 'oninput="updateRationaleCounter()"' not in diffs


def test_textarea_has_autofocus_and_aria_invalid():
    diffs = _read(_TPL / "diffs.html")
    assert "autofocus" in diffs
    assert 'aria-invalid="true"' in diffs


def test_approve_token_trims_on_input():
    diffs = _read(_TPL / "diffs.html")
    assert 'oninput="this.value = this.value.trim()"' in diffs


def test_token_countdown_uses_clock_offset():
    diffs = _read(_TPL / "diffs.html")
    assert "clockOffsetFromPayload" in diffs


def test_alerts_purged_from_main_tabs():
    """The 5 alert() call sites the UI audit flagged must be gone."""
    for name in ("findings.html", "plans.html", "organizations.html", "settings.html", "diffs.html"):
        body = _read(_TPL / name)
        assert "alert(await" not in body, f"{name} still has alert(await ...)"
        assert "alert(`HTTP" not in body, f"{name} still has alert(`HTTP ...)"


def test_all_replaced_with_mnemos_ui():
    """Each former alert() call should now use the shared helper."""
    for name in ("findings.html", "plans.html", "organizations.html", "settings.html"):
        body = _read(_TPL / name)
        assert "MnemosUI." in body, f"{name} must use MnemosUI helpers"
