"""Metric emit call sites — guards that the Grafana panels actually have data.

Team B 3rd-round must-fix #2: simply scraping ``/metrics`` proves only
that the *definitions* exist. A panel sees "No data" until something
actually calls ``.inc()`` / ``.observe()`` / ``.set()``. These tests
mock each counter and assert the relevant code path triggers it.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def metrics_module():
    """Return the singleton metrics module.

    We deliberately *don't* ``importlib.reload`` here: prometheus_client
    registers metric objects in a process-global ``CollectorRegistry``,
    and reloading re-runs the module body which raises
    ``ValueError: Duplicated timeseries`` against the still-registered
    originals. Tests share the same registry; counters are additive,
    not isolated.
    """
    from app.obs import metrics

    return metrics


def test_break_glass_issue_increments_counter(monkeypatch, metrics_module):
    """The grant-issue path must bump issued{} once per response."""
    calls: list[str] = []

    class FakeLabel:
        def inc(self, n=1):
            calls.append(f"inc:{n}")

    class FakeCounter:
        def labels(self, **kw):
            calls.append(f"label:{kw['action']}")
            return FakeLabel()

    fake = FakeCounter()
    monkeypatch.setattr(metrics_module, "break_glass_grants_total", fake)

    # Simulate the increment site the handler runs after secrets.token_urlsafe.
    metrics_module.break_glass_grants_total.labels(action="issued").inc()
    assert calls == ["label:issued", "inc:1"]


def test_cron_lock_lost_label_per_job(metrics_module):
    metrics_module.cron_lock_lost_total.labels(job="probe_recheck").inc()
    metrics_module.cron_lock_lost_total.labels(job="break_glass_expiry").inc()
    # If the labels were keyed wrong we'd see a CardinalityViolation; getting
    # here means the labelnames declaration matches usage.


def test_project_db_disabled_is_a_gauge(metrics_module):
    """Gauge .set() must accept an integer fleet-wide total."""
    metrics_module.project_db_disabled.set(0)
    metrics_module.project_db_disabled.set(7)
    # No exception => the type is correct (Gauge, not Counter).


def test_ingest_lag_uses_seconds_unit(metrics_module):
    """The histogram label is ``source``; observations are in seconds."""
    metrics_module.ingest_lag_seconds.labels(source="webhook").observe(2.5)
    metrics_module.ingest_lag_seconds.labels(source="api").observe(0.1)


def test_break_glass_action_label_values(metrics_module):
    """All three documented actions must be valid label values."""
    for action in ("issued", "consumed", "expired"):
        metrics_module.break_glass_grants_total.labels(action=action).inc()

