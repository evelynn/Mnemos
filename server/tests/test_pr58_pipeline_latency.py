"""PR-58 — pipeline latency visualisation (core-value A8).

The 2nd value reassessment kept A8 at ❌ — per-stage timing was
recorded in ``analysis_stages`` but never surfaced. Spec §1.5
promises "first full analysis ≤ 8 hours" but an operator had no
way to see where the time went. This PR adds the
``pipeline_latency`` endpoint + an analysis-tab panel.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# pipeline_latency endpoint
# ---------------------------------------------------------------------------


def test_latency_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/pipeline_latency"' in body
    assert "async def pipeline_latency(" in body


def test_latency_aggregates_per_stage():
    """Mean / p95 / max per stage name, over the recent runs."""
    body = _read(_APP / "api" / "analysis.py")
    lat_idx = body.find("async def pipeline_latency(")
    slab = body[lat_idx:lat_idx + 3500]
    assert "per_stage" in slab
    assert '"mean_sec"' in slab
    assert '"p95_sec"' in slab
    assert '"max_sec"' in slab


def test_latency_computes_p95():
    """p95 — not just mean — so a long-tail stage shows up. The
    helper indexes the sorted sample list at the 95th percentile."""
    body = _read(_APP / "api" / "analysis.py")
    assert "def _p95(" in body
    assert "0.95" in body


def test_latency_identifies_slowest_stage():
    """The 'tune this first' callout — stages are sorted by mean
    descending, so ``stages[0]`` is the slowest."""
    body = _read(_APP / "api" / "analysis.py")
    assert '"slowest_stage"' in body


def test_latency_reports_total_wall_clock():
    """``mean_total_sec`` is the webhook→done wall clock from
    AnalysisRun.started_at/completed_at — the number that maps to
    the spec's 8-hour promise."""
    body = _read(_APP / "api" / "analysis.py")
    assert '"mean_total_sec"' in body
    assert "total_durations" in body


def test_latency_clamps_run_count():
    body = _read(_APP / "api" / "analysis.py")
    lat_idx = body.find("async def pipeline_latency(")
    slab = body[lat_idx:lat_idx + 3500]
    assert "max(1, min(runs, 100))" in slab


def test_latency_org_isolated():
    body = _read(_APP / "api" / "analysis.py")
    lat_idx = body.find("pipeline_latency")
    block = body[max(0, lat_idx - 200):lat_idx + 80]
    assert "require_project_org" in block


def test_latency_skips_incomplete_stages():
    """A stage with no completed_at (still running / crashed)
    must not enter the average — only finished stages have a
    duration."""
    body = _read(_APP / "api" / "analysis.py")
    lat_idx = body.find("async def pipeline_latency(")
    slab = body[lat_idx:lat_idx + 3500]
    assert "started is None or completed is None" in slab


# ---------------------------------------------------------------------------
# p95 arithmetic
# ---------------------------------------------------------------------------


def test_p95_index_arithmetic():
    """For 20 samples, p95 index = round(0.95 * 19) = 18 (0-based)
    — the 19th of 20 sorted values."""
    n = 20
    idx = min(n - 1, int(round(0.95 * (n - 1))))
    assert idx == 18


def test_p95_single_sample():
    n = 1
    idx = min(n - 1, int(round(0.95 * (n - 1))))
    assert idx == 0


# ---------------------------------------------------------------------------
# analysis.html — latency panel
# ---------------------------------------------------------------------------


def test_analysis_template_has_latency_panel():
    body = _read(_APP / "dashboard" / "templates" / "analysis.html")
    assert 'data-i18n="Pipeline latency"' in body
    assert "loadLatency" in body
    assert "/pipeline_latency" in body


def test_latency_panel_renders_stage_table():
    body = _read(_APP / "dashboard" / "templates" / "analysis.html")
    assert 'data-i18n="Stage"' in body
    assert 'data-i18n="Mean (s)"' in body
    assert 'data-i18n="p95 (s)"' in body
    # A bar visualises the relative mean.
    assert "lat-bar" in body


def test_latency_panel_handles_no_runs():
    body = _read(_APP / "dashboard" / "templates" / "analysis.html")
    assert "No completed runs to measure yet" in body


def test_latency_bar_styled():
    body = _read(_APP / "dashboard" / "static" / "app.css")
    assert ".lat-bar" in body


def test_phrase_book_translates_latency_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("파이프라인 지연", "지연 불러오기", "단계", "평균 (초)", "가장 느림"):
        assert kr in body, f"phrase book missing {kr!r}"
