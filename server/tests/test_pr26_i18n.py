"""PR-26 — P2-3 i18n initial wave (Korean phrase book + locale switcher)."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# ui.js — phrase book + helpers
# ---------------------------------------------------------------------------


def test_ui_js_exposes_t_helper():
    body = _read(_STATIC / "ui.js")
    assert "MnemosUI.t" not in body  # documented as window.MnemosUI.t
    assert "t: t," in body
    assert "setLocale: setLocale," in body
    assert "applyI18n: applyI18n," in body


def test_phrase_book_has_korean_translations():
    body = _read(_STATIC / "ui.js")
    # A handful of essential phrases the dashboard relies on.
    assert "Mnemos에 오신 것을 환영합니다" in body
    assert "분석이 완료되었습니다" in body
    assert "프로젝트" in body
    assert "Findings 재구축이 예약되었습니다" in body


def test_locale_resolution_persists_to_localstorage():
    body = _read(_STATIC / "ui.js")
    assert 'localStorage.getItem("mnemos_locale")' in body
    assert 'localStorage.setItem("mnemos_locale"' in body


def test_locale_resolution_falls_back_to_navigator_language():
    body = _read(_STATIC / "ui.js")
    assert "navigator.language" in body
    assert 'toLowerCase().startsWith("ko")' in body


def test_apply_i18n_handles_placeholders():
    body = _read(_STATIC / "ui.js")
    # Two attribute conventions: ``data-i18n`` for text content and
    # ``data-i18n-placeholder`` for form inputs.
    assert "data-i18n" in body
    assert "data-i18n-placeholder" in body


def test_apply_i18n_runs_on_dom_content_loaded():
    body = _read(_STATIC / "ui.js")
    # The bootstrap must apply once on load and reflect the locale
    # in <html lang> for screen readers.
    assert "applyI18n()" in body
    assert "document.documentElement.lang" in body


# ---------------------------------------------------------------------------
# Locale switcher UI in _layout.html
# ---------------------------------------------------------------------------


def test_layout_has_locale_switcher():
    body = _read(_TPL / "_layout.html")
    assert "locale-switcher" in body
    assert 'data-locale="en"' in body
    assert 'data-locale="ko"' in body
    # Korean button shows the native script — not "KO".
    assert "한국어" in body


def test_layout_wires_locale_buttons():
    body = _read(_TPL / "_layout.html")
    # The click handler delegates to MnemosUI.setLocale.
    assert "MnemosUI.setLocale" in body


def test_layout_sse_strip_is_i18n_marked():
    body = _read(_TPL / "_layout.html")
    assert 'data-i18n="Analysis stream disconnected' in body


# ---------------------------------------------------------------------------
# Page templates pick up the i18n markers
# ---------------------------------------------------------------------------


def test_dashboard_onboarding_is_translatable():
    body = _read(_TPL / "dashboard.html")
    assert 'data-i18n="Welcome to Mnemos"' in body
    assert 'data-i18n="Register a GitLab project"' in body


def test_projects_form_has_translatable_placeholders():
    body = _read(_TPL / "projects.html")
    # ``data-i18n-placeholder`` lets the locale switch swap the
    # placeholder text without a server round-trip.
    assert "data-i18n-placeholder" in body


def test_projects_toast_uses_t_helper():
    body = _read(_TPL / "projects.html")
    # The variable interpolation goes through MnemosUI.t with the
    # ``$1`` substitution convention.
    assert "MnemosUI.t(" in body
    assert '"$1"' in body or "'$1'" in body


# ---------------------------------------------------------------------------
# CSS for the locale switcher
# ---------------------------------------------------------------------------


def test_css_styles_locale_switcher():
    css = _read(_STATIC / "app.css")
    assert ".locale-switcher" in css
    assert ".locale-btn.active" in css
