"""PR-53 — executive report + L3 summary surfacing + LLM cost.

The value audit's C-area: L1-L3 summaries were produced but never
shown (C4), and LLM token spend was untracked (C5). This PR adds
the summaries + llm_cost endpoints and a printable /report page.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# summaries endpoint — C4
# ---------------------------------------------------------------------------


def test_summaries_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/summaries"' in body
    assert "async def list_summaries(" in body


def test_summaries_filterable_by_level():
    """The report page asks for ``?level=3``; the drill-down asks
    for all."""
    body = _read(_APP / "api" / "analysis.py")
    assert "level: int | None" in body
    assert "Summary.level == level" in body


def test_summaries_excludes_superseded():
    """An operator must never read a stale narrative — only
    current (non-superseded) summaries are returned."""
    body = _read(_APP / "api" / "analysis.py")
    summ_idx = body.find("async def list_summaries(")
    next_fn = body.find("\nasync def ", summ_idx + 1)
    slab = body[summ_idx:next_fn]
    assert "superseded_by.is_(None)" in slab


def test_summaries_org_isolated():
    body = _read(_APP / "api" / "analysis.py")
    summ_idx = body.find("async def list_summaries(")
    block = body[max(0, summ_idx - 300):summ_idx + 100]
    assert "require_project_org" in block


# ---------------------------------------------------------------------------
# llm_cost endpoint — C5
# ---------------------------------------------------------------------------


def test_llm_cost_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/llm_cost"' in body
    assert "async def project_llm_cost(" in body


def test_llm_cost_sums_physical_call_tokens():
    body = _read(_APP / "api" / "analysis.py")
    cost_idx = body.find("async def project_llm_cost(")
    next_fn = body.find("\nasync def ", cost_idx + 1)
    slab = body[cost_idx:next_fn]
    assert "tokens_used" in slab
    assert "total_tokens" in slab
    assert "LLMCall" in slab
    # Estimated USD from a configurable per-Mtok rate.
    assert "estimated_usd" in slab
    assert "MNEMOS_LLM_USD_PER_MTOK" in slab


# ---------------------------------------------------------------------------
# /report page
# ---------------------------------------------------------------------------


def test_report_template_present():
    p = _APP / "dashboard" / "templates" / "report.html"
    assert p.exists()
    body = _read(p)
    assert 'data-i18n="Executive report"' in body


def test_report_registered_in_router():
    body = _read(_APP / "dashboard" / "router.py")
    assert '"report"' in body


def test_sidebar_links_to_report():
    body = _read(_APP / "dashboard" / "templates" / "_layout.html")
    assert 'href="/report"' in body


def test_report_fetches_all_three_sources():
    """The report composes findings/summary + summaries(L3) +
    llm_cost in one parallel fetch."""
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "/findings/summary" in body
    assert "/summaries?level=3" in body
    assert "/llm_cost" in body
    assert "Promise.all(" in body


def test_report_has_print_css():
    """A PM saves the report as PDF — the @media print block hides
    the sidebar + nav so the printout is just the report."""
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "@media print" in body
    assert ".sidebar" in body and "display: none" in body


def test_report_renders_l3_narrative_section():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "rp-l3" in body
    assert "l3-item" in body
    # Open questions surfaced under each L3 item.
    assert "open_questions" in body


def test_report_renders_priority_and_kind_bars():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "rp-priority" in body
    assert "rp-kinds" in body
    assert "report-bar-row" in body


def test_report_shows_cost():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "rp-cost" in body
    assert "estimated_usd" in body


def test_report_honours_project_query_param():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert 'URLSearchParams(window.location.search).get("project")' in body


def test_phrase_book_translates_report_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in (
        "보고서",
        "경영 보고서",
        "시스템 분석 보고서",
        "건강 요약",
        "시스템 수준 서술 (L3)",
        "분석 비용",
    ):
        assert kr in body, f"phrase book missing {kr!r}"
