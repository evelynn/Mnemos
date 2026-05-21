"""PR-57 — guided tour + contextual help (core-value D4).

The 2nd value reassessment found D4 (learning curve) had *fallen*
to 48 — five new tabs added with no teaching material. This PR
adds a vanilla spotlight tour (fires once on first visit) + a
reusable contextual-help button.
"""

from __future__ import annotations

from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tour engine
# ---------------------------------------------------------------------------


def test_tour_api_exposed():
    body = _read(_STATIC / "ui.js")
    assert "startTour: startTour" in body
    assert "tourDone: tourDone" in body
    assert "helpButton: helpButton" in body


def test_tour_is_vanilla_no_library():
    """The tour must be built from scratch — no Intro.js / Shepherd
    dependency. The bundle stays a single ui.js file."""
    body = _read(_STATIC / "ui.js")
    assert "introjs" not in body.lower()
    assert "shepherd" not in body.lower()
    # The spotlight is a plain div with a big box-shadow.
    assert "tour-spotlight" in body


def test_tour_steps_have_selector_title_body():
    body = _read(_STATIC / "ui.js")
    # The renderer reads step.selector / step.title / step.body.
    assert "step.selector" in body
    assert "step.title" in body
    assert "step.body" in body


def test_tour_marks_done_in_localstorage():
    """Once finished or skipped, the tour must not fire again —
    keyed off ``mnemos_tour_done``."""
    body = _read(_STATIC / "ui.js")
    assert 'localStorage.setItem("mnemos_tour_done"' in body
    assert 'localStorage.getItem("mnemos_tour_done")' in body


def test_tour_has_skip_back_next():
    body = _read(_STATIC / "ui.js")
    assert "tour-skip" in body
    assert "tour-back" in body
    assert "tour-next" in body
    # Back is disabled on the first step.
    assert "_tourIdx === 0" in body


def test_tour_last_step_shows_done():
    body = _read(_STATIC / "ui.js")
    assert '_tourIdx === _tourSteps.length - 1' in body


def test_tour_spotlight_repositions_to_target():
    """The spotlight box must move to wrap the step's target
    element via getBoundingClientRect."""
    body = _read(_STATIC / "ui.js")
    assert "getBoundingClientRect()" in body
    assert "spotlight.style.top" in body


# ---------------------------------------------------------------------------
# Dashboard — first-run tour
# ---------------------------------------------------------------------------


def test_dashboard_fires_welcome_tour_on_first_visit():
    body = _read(_TPL / "dashboard.html")
    assert "runWelcomeTour" in body
    # Gated on tourDone() — only fires for a fresh browser.
    assert "if (!MnemosUI.tourDone())" in body


def test_welcome_tour_covers_main_surfaces():
    """The 4-step tour walks Projects → Findings → Graph → Report."""
    body = _read(_TPL / "dashboard.html")
    assert 'href="/projects"' in body
    assert 'href="/findings"' in body
    assert 'href="/graph"' in body
    assert 'href="/report"' in body
    # Five steps total (intro + 4 surfaces).
    assert body.count('selector:') >= 4


def test_welcome_tour_steps_are_translatable():
    """Every tour string goes through ``MnemosUI.t`` so a Korean
    operator gets a Korean tour."""
    body = _read(_TPL / "dashboard.html")
    assert 'MnemosUI.t("1. Register a project")' in body
    assert 'MnemosUI.t("3. Explore the graph")' in body


# ---------------------------------------------------------------------------
# Profile — replay tour
# ---------------------------------------------------------------------------


def test_profile_has_replay_tour_button():
    body = _read(_TPL / "profile.html")
    assert 'id="replay-tour"' in body
    assert 'data-i18n="Replay welcome tour"' in body


def test_replay_clears_tour_done_flag():
    body = _read(_TPL / "profile.html")
    assert 'localStorage.removeItem("mnemos_tour_done")' in body


# ---------------------------------------------------------------------------
# CSS + i18n
# ---------------------------------------------------------------------------


def test_tour_and_help_styled():
    body = _read(_STATIC / "app.css")
    assert ".tour-overlay" in body
    assert ".tour-spotlight" in body
    assert ".tour-pop" in body
    assert ".help-btn" in body
    assert ".help-modal" in body


def test_phrase_book_translates_tour_strings():
    body = _read(_STATIC / "ui.js")
    for kr in (
        "건너뛰기",
        "이전",
        "다음",
        "완료",
        "환영 투어 다시 보기",
        "1. 프로젝트 등록",
        "3. 그래프 탐색",
    ):
        assert kr in body, f"phrase book missing {kr!r}"
