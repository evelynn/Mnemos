"""PR-27 — final Phase 2 sweep: broader i18n + README convergence."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
_README = Path(__file__).resolve().parents[2] / "README.md"
_BACKLOG = Path(__file__).resolve().parents[2] / "docs" / "operator-guide" / "phase2_backlog.md"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Sidebar i18n
# ---------------------------------------------------------------------------


def test_sidebar_nav_entries_are_translatable():
    body = _read(_TPL / "_layout.html")
    for label in ("Dashboard", "Projects", "Analysis", "Data", "Plans",
                  "Diffs", "Findings", "Audit", "Settings"):
        assert f'data-i18n="{label}"' in body


# ---------------------------------------------------------------------------
# Phrase book covers the sidebar + the new empty-state strings
# ---------------------------------------------------------------------------


def test_phrase_book_covers_sidebar_nav():
    body = _read(_STATIC / "ui.js")
    # Each sidebar label has a Korean translation.
    assert '"Dashboard": "대시보드"' in body
    assert '"Findings": "결과"' in body
    assert '"Projects": "프로젝트"' in body


def test_phrase_book_covers_new_empty_states():
    body = _read(_STATIC / "ui.js")
    assert '"No runs yet.": "분석 실행이 없습니다."' in body
    assert '"No rows returned.": "반환된 행이 없습니다."' in body


# ---------------------------------------------------------------------------
# Templates use the i18n helpers
# ---------------------------------------------------------------------------


def test_findings_uses_t_for_rebuild_toast():
    body = _read(_TPL / "findings.html")
    # The toast string runs through MnemosUI.t so a Korean operator
    # sees the translated message.
    assert 'MnemosUI.t("Findings rebuild queued.")' in body


def test_findings_empty_state_is_translatable():
    body = _read(_TPL / "findings.html")
    assert 'data-i18n="No findings."' in body


def test_data_empty_state_is_translatable():
    body = _read(_TPL / "data.html")
    assert 'data-i18n="No data entities yet."' in body


def test_analysis_empty_state_is_translatable():
    body = _read(_TPL / "analysis.html")
    assert 'data-i18n="No runs yet."' in body


def test_findings_page_head_is_translatable():
    body = _read(_TPL / "findings.html")
    assert '<h1 data-i18n="Findings">' in body


# ---------------------------------------------------------------------------
# README documents Phase 2 status
# ---------------------------------------------------------------------------


def test_readme_phase2_status_table_present():
    body = _read(_README)
    assert "## Phase 2 status" in body
    # All 10 P2 items are listed.
    for marker in ("P2-1", "P2-2", "P2-3", "P2-4", "P2-5",
                   "P2-6", "P2-7", "P2-8", "P2-9", "P2-10"):
        assert marker in body


def test_readme_lists_each_phase2_pr():
    body = _read(_README)
    # Sprint table — each PR in the sprint shows up.
    for pr in ("PR-20", "PR-21", "PR-22", "PR-23",
               "PR-24", "PR-25", "PR-26", "PR-27"):
        assert pr in body


# ---------------------------------------------------------------------------
# Phase 2 backlog doc still has all P2-* sections (now annotated with status)
# ---------------------------------------------------------------------------


def test_backlog_doc_still_has_all_sections():
    body = _read(_BACKLOG)
    for marker in ("## P2-1", "## P2-2", "## P2-3", "## P2-4", "## P2-5",
                   "## P2-6", "## P2-7", "## P2-8", "## P2-9", "## P2-10"):
        assert marker in body
