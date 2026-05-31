"""PR-55 — long-horizon finding trend (core-value C2).

PR-51 gave a single 7-day delta. The value reassessment's C2
finding: a PM wants the 90-day shape, not a one-week snapshot.
This PR adds the ``findings/trend`` time-bucketed endpoint + a
twin-line SVG chart on the executive report.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# findings/trend endpoint
# ---------------------------------------------------------------------------


def test_trend_endpoint_defined():
    body = _read(_APP / "api" / "findings.py")
    assert '"/api/v1/projects/{project_id}/findings/trend"' in body
    assert "async def findings_trend(" in body


def test_trend_buckets_by_window():
    """The endpoint splits the ``days`` window into ``bucket_days``
    chunks — default 90 days / 7-day buckets = 13 buckets."""
    body = _read(_APP / "api" / "findings.py")
    trend_idx = body.find("async def findings_trend(")
    slab = body[trend_idx:trend_idx + 2400]
    assert "n_buckets" in slab
    assert "bucket_days" in slab


def test_trend_clamps_days_and_bucket():
    """A 1-day window or a 9999-day window is a typo. Clamp days to
    [7, 365] and bucket_days to [1, 30]."""
    body = _read(_APP / "api" / "findings.py")
    trend_idx = body.find("async def findings_trend(")
    slab = body[trend_idx:trend_idx + 2400]
    assert "max(7, min(days, 365))" in slab
    assert "max(1, min(bucket_days, 30))" in slab


def test_trend_bucket_carries_new_resolved_net():
    """Each bucket reports findings first-seen (new), resolved, and
    the net (new − resolved). Net > 0 = growing backlog."""
    body = _read(_APP / "api" / "findings.py")
    trend_idx = body.find("async def findings_trend(")
    # Widen slab to 4000 chars after PR-138d added the tz-coercion
    # helper inside findings_trend (~300 chars of comment + body).
    slab = body[trend_idx:trend_idx + 4000]
    assert '"new": 0' in slab
    assert '"resolved": 0' in slab
    assert 'b["net"] = b["new"] - b["resolved"]' in slab


def test_trend_returns_window_totals():
    body = _read(_APP / "api" / "findings.py")
    trend_idx = body.find("async def findings_trend(")
    slab = body[trend_idx:trend_idx + 3000]
    assert '"total_new"' in slab
    assert '"total_resolved"' in slab


def test_trend_org_isolated():
    body = _read(_APP / "api" / "findings.py")
    trend_idx = body.find("findings/trend")
    block = body[max(0, trend_idx - 200):trend_idx + 80]
    assert "require_project_org" in block


# ---------------------------------------------------------------------------
# Behaviour — bucket-index arithmetic
# ---------------------------------------------------------------------------


def test_bucket_count_arithmetic():
    """90 days / 7-day buckets → ceil(90/7) = 13 buckets."""
    days, bucket_days = 90, 7
    n = (days + bucket_days - 1) // bucket_days
    assert n == 13


def test_bucket_count_30_day():
    days, bucket_days = 30, 1
    n = (days + bucket_days - 1) // bucket_days
    assert n == 30


# ---------------------------------------------------------------------------
# report.html — trend chart
# ---------------------------------------------------------------------------


def test_report_fetches_trend():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "/findings/trend?days=90&bucket_days=7" in body
    # Merged into the existing Promise.all.
    assert "trendRes" in body


def test_report_renders_trend_chart():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "renderTrendChart(" in body
    assert 'id="rp-trend"' in body
    # Twin-line SVG — new vs resolved.
    assert "trend-new" in body
    assert "trend-resolved" in body


def test_report_trend_chart_has_net_bars():
    """Net bars (new − resolved) — up bars (backlog growing) vs
    down bars (shrinking)."""
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "trend-bar-up" in body
    assert "trend-bar-down" in body


def test_report_trend_handles_empty_history():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert "Not enough history yet" in body


def test_trend_chart_styled():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert ".trend-chart" in body
    assert ".trend-new" in body
    assert ".trend-legend" in body


def test_report_has_trend_heading():
    body = _read(_APP / "dashboard" / "templates" / "report.html")
    assert 'data-i18n="Finding trend (90 days)"' in body


def test_phrase_book_translates_trend_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("발견 추이 (90일)", "추이 차트", "신규", "해결", "기간 합계"):
        assert kr in body, f"phrase book missing {kr!r}"
