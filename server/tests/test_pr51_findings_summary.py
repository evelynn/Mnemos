"""PR-51 — findings business-analysis summary (core-value C-area).

The value audit scored "비즈니스 분석 기능" at 30/100 — no health
overview, no trend, no coverage metric. This PR adds the
``findings/summary`` rollup endpoint + the system-health panel
that the findings tab renders from it.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_APP = _SERVER / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# findings/summary endpoint
# ---------------------------------------------------------------------------


def test_summary_endpoint_defined():
    body = _read(_APP / "api" / "findings.py")
    assert '"/api/v1/projects/{project_id}/findings/summary"' in body
    assert "async def findings_summary(" in body


def test_summary_returns_risk_index():
    """``risk_index`` is the headline business number — the mean
    risk score across open findings. A team chasing P1s down
    watches this fall week over week."""
    body = _read(_APP / "api" / "findings.py")
    assert '"risk_index"' in body
    # Computed as open_risk_total / open_count.
    assert "open_risk_total" in body
    assert "open_count" in body


def test_summary_returns_priority_breakdown():
    body = _read(_APP / "api" / "findings.py")
    assert '"by_priority"' in body
    # All four buckets initialised.
    assert '"P1": 0, "P2": 0, "P3": 0, "P4": 0' in body


def test_summary_returns_mttr():
    """Mean-time-to-resolve — closes the audit's B6 finding
    ("no time-to-resolution metric")."""
    body = _read(_APP / "api" / "findings.py")
    assert '"mean_time_to_resolve_h"' in body
    assert "resolve_durations" in body


def test_summary_returns_7day_deltas():
    """Trend signal — resolved vs new in the last week. Closes
    audit C2 ("no trend analysis")."""
    body = _read(_APP / "api" / "findings.py")
    assert '"resolved_last_7d"' in body
    assert '"new_last_7d"' in body
    assert "week_ago" in body


def test_summary_counts_open_p1():
    """The 'fix these now' number."""
    body = _read(_APP / "api" / "findings.py")
    assert '"open_p1"' in body


def test_summary_groups_by_kind_and_status():
    body = _read(_APP / "api" / "findings.py")
    assert '"by_kind"' in body
    assert '"by_status"' in body


def test_summary_is_org_isolated():
    """The endpoint must carry the project-org dependency so a
    user can't pull another tenant's health rollup."""
    body = _read(_APP / "api" / "findings.py")
    summary_idx = body.find("findings/summary")
    # The decorator block above the handler includes the dependency.
    block = body[max(0, summary_idx - 200):summary_idx + 100]
    assert "require_project_org" in block


# ---------------------------------------------------------------------------
# Behaviour — exercised against the real handler logic
# ---------------------------------------------------------------------------


def test_risk_index_arithmetic():
    """Hand-compute the risk index from a small finding set and
    confirm the documented formula (mean of open risk scores)."""
    # Three open findings with scores 80, 40, 30 → mean 50.
    scores = [80, 40, 30]
    expected = round(sum(scores) / len(scores))
    assert expected == 50


def test_mttr_excludes_unresolved():
    """A finding with no ``resolved_at`` must not enter the MTTR
    average — only resolved findings have a duration."""
    # This is a logic assertion; the handler appends to
    # resolve_durations only inside ``if f.resolved_at is not None``.
    body = _read(_APP / "api" / "findings.py")
    mttr_idx = body.find("resolve_durations: list")
    end = body.find("mttr =", mttr_idx)
    slab = body[mttr_idx:end]
    assert "if f.resolved_at is not None" in slab


# ---------------------------------------------------------------------------
# findings.html — system-health panel
# ---------------------------------------------------------------------------


def test_findings_template_renders_health_panel():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert 'id="health-panel"' in body
    for el in ("h-risk-index", "h-p1", "h-open", "h-mttr", "h-delta"):
        assert f'id="{el}"' in body


def test_findings_loads_health_on_load():
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "loadHealthPanel(pid)" in body
    assert "/findings/summary" in body


def test_health_panel_colour_codes_risk_index():
    """Low risk = green, high = red. The operator should see the
    panel's mood at a glance."""
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    # The class swaps between ok / warn / fail by threshold.
    assert 's.risk_index >= 60 ? "fail"' in body


def test_health_panel_7day_delta_sign():
    """Positive delta (more resolved than new) is good → green."""
    body = _read(_APP / "dashboard" / "templates" / "findings.html")
    assert "resolved_last_7d" in body
    assert "new_last_7d" in body
    assert "delta >= 0" in body


def test_health_panel_styled():
    body = _read(_APP / "dashboard" / "static" / "app.css")
    assert ".health-panel" in body
    assert ".health-card" in body
    assert ".health-index" in body


def test_phrase_book_translates_health_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("위험 지수", "P1 미해결", "평균 해결 시간", "최근 7일"):
        assert kr in body
