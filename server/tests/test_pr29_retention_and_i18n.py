"""PR-29 — runtime_observations 14-day retention + form-label i18n broaden."""

from __future__ import annotations

from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_TPL = _SERVER / "app" / "dashboard" / "templates"
_STATIC = _SERVER / "app" / "dashboard" / "static"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# D1 — runtime_observations retention sweep
# ---------------------------------------------------------------------------


def test_retention_purge_sweeps_runtime_observations():
    body = _read(_SERVER / "app" / "orchestrator" / "cron_jobs.py")
    # The 9th-round audit caught that PR-25 promised a 14-day TTL
    # but didn't actually wire it into the existing retention cron.
    assert "DELETE FROM runtime_observations" in body
    assert "WHERE last_seen_at < :cutoff" in body


def test_runtime_retention_window_is_14_days():
    body = _read(_SERVER / "app" / "orchestrator" / "cron_jobs.py")
    # 14-day TTL was the spec'd window (Team B 5th-round critique #2,
    # phase2_backlog.md P2-2). Anything longer reintroduces the
    # unbounded-growth risk; shorter risks discarding useful
    # observations before the next analysis run picks them up.
    assert "timedelta(days=14)" in body


def test_retention_purge_returns_two_counts():
    body = _read(_SERVER / "app" / "orchestrator" / "cron_jobs.py")
    # The return shape grows ``runtime_deleted`` alongside the
    # existing ``deleted`` key so the Grafana panel for the cron job
    # can split the two.
    assert '"runtime_deleted"' in body


def test_retention_purge_logs_both_counts():
    body = _read(_SERVER / "app" / "orchestrator" / "cron_jobs.py")
    # One log line per cron pass is enough; the format must show
    # both numbers so an operator tailing the worker log can spot a
    # runaway runtime_observations table quickly.
    assert "audit=%d runtime=%d" in body


def test_audit_sweep_still_works():
    """Regression guard — the new runtime sweep must NOT have replaced
    the audit sweep. Both run in the same transaction.
    """
    body = _read(_SERVER / "app" / "orchestrator" / "cron_jobs.py")
    assert "DELETE FROM audit_logs WHERE created_at < :cutoff" in body
    # 180-day audit window stays.
    assert "timedelta(days=180)" in body


# ---------------------------------------------------------------------------
# D2 (partial close) — form labels and CTAs i18n
# ---------------------------------------------------------------------------


def test_audit_search_button_is_i18n():
    body = _read(_TPL / "audit.html")
    assert 'data-i18n="Search"' in body


def test_findings_load_rebuild_are_i18n():
    body = _read(_TPL / "findings.html")
    assert 'data-i18n="Load"' in body
    assert 'data-i18n="Rebuild"' in body


def test_analysis_start_and_load_runs_are_i18n():
    body = _read(_TPL / "analysis.html")
    assert 'data-i18n="Start analysis"' in body
    assert 'data-i18n="Load runs"' in body
    assert 'data-i18n="Recent runs"' in body


def test_projects_register_is_i18n():
    body = _read(_TPL / "projects.html")
    assert 'data-i18n="Register project"' in body


def test_diffs_load_submissions_is_i18n():
    body = _read(_TPL / "diffs.html")
    assert 'data-i18n="Load submissions"' in body


def test_phrase_book_covers_new_buttons():
    body = _read(_STATIC / "ui.js")
    # Every label we just tagged on a template needs a Korean
    # translation; otherwise the locale switcher leaves the button
    # in English even when set to ``ko``.
    for label in (
        "Search",
        "Load",
        "Rebuild",
        "Start analysis",
        "Load runs",
        "Load submissions",
        "Register project",
        "Recent runs",
    ):
        assert f'"{label}"' in body, f"phrase book missing {label!r}"
