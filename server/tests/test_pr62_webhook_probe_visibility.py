"""PR-62 — surface silently-dropped webhooks + overdue DB probes (A).

A critical reassessment flagged two §2.7 always-on gaps:

* ``gitlab_webhook`` returned a bare 200 ``{"enqueued": false}`` for a
  push whose project wasn't matched — an operator whose project's
  ``gitlab_project_id`` is unset got no analysis and no signal.
* ``run_probe_recheck`` ``continue``-skips any ProjectDB with a running
  analysis, so a continuously-busy project's credentials could go
  re-verified-never, quietly weakening §2.5.

This PR adds a ``skip_reason`` to the webhook response + a distinct
``webhook.skipped`` audit action, and an overdue-probe ceiling that
logs + counts bindings the recheck keeps skipping.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# webhook skip visibility
# ---------------------------------------------------------------------------


def test_webhook_computes_skip_reason():
    body = _read(_APP / "api" / "webhooks.py")
    assert "skip_reason" in body
    assert '"project_not_registered"' in body
    assert '"malformed_push_payload"' in body


def test_webhook_uses_distinct_skipped_action():
    body = _read(_APP / "api" / "webhooks.py")
    assert '"webhook.skipped"' in body
    # The action flips based on skip_reason.
    assert 'action = "webhook.skipped" if skip_reason else "webhook.received"' in body


def test_webhook_returns_skip_reason():
    body = _read(_APP / "api" / "webhooks.py")
    idx = body.rfind("return {")
    slab = body[idx:idx + 200]
    assert '"skip_reason": skip_reason' in slab


# ---------------------------------------------------------------------------
# probe-recheck overdue ceiling
# ---------------------------------------------------------------------------


def test_probe_overdue_ceiling_defined():
    from app.orchestrator.cron_jobs import (
        PROBE_OVERDUE_CEILING,
        PROBE_RECHECK_INTERVAL,
    )

    # The ceiling is a multiple of the normal recheck window, not equal
    # to it — a busy project gets several missed cycles before warning.
    assert PROBE_OVERDUE_CEILING > PROBE_RECHECK_INTERVAL


def test_probe_recheck_counts_overdue_skips():
    body = _read(_APP / "orchestrator" / "cron_jobs.py")
    idx = body.find("async def _probe_recheck_one(")
    slab = body[idx:idx + 3200]
    # The running-analysis skip now classifies the skip as overdue.
    assert "overdue" in slab
    assert "overdue_cutoff" in slab
    assert "log.warning(" in slab
    # The result dict reports the overdue count.
    assert '"overdue": overdue' in slab
