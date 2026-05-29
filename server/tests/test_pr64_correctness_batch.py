"""PR-64 — correctness batch from the post-PR-63 reassessment.

A re-audit caught four code-addressable defects, including one
regression introduced by PR-62:

* ``tag_push`` was treated like a branch push, enqueueing a redundant
  analysis of an already-analysed commit (its all-zero ``before`` SHA
  also defeats job dedup).
* ``retention_purge`` only deleted ``webhook.received`` audit rows —
  PR-62's new ``webhook.skipped`` rows would grow unbounded.
* ``findings_roi`` precision / risk-eliminated aggregated all-time, so
  a mature project's headline numbers froze.
* ``CWE-561`` (dead code) mapped to a PCI DSS "secure coding" entry
  that doesn't honestly apply.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# tag_push no longer enqueues
# ---------------------------------------------------------------------------


def test_tag_push_skipped_with_reason():
    body = _read(_APP / "api" / "webhooks.py")
    assert '"tag_push_no_new_commits"' in body
    # tag_push handled in its own branch, not lumped with push.
    assert 'object_kind == "tag_push"' in body
    assert 'object_kind == "push"' in body


# ---------------------------------------------------------------------------
# retention purge covers webhook.skipped
# ---------------------------------------------------------------------------


def test_retention_purges_webhook_skipped():
    body = _read(_APP / "orchestrator" / "cron_jobs.py")
    assert "'webhook.received', 'webhook.skipped'" in body


# ---------------------------------------------------------------------------
# ROI time window
# ---------------------------------------------------------------------------


def test_roi_accepts_days_window():
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    slab = body[idx:idx + 3800]
    assert "days: int = 0" in slab
    assert "window_start" in slab
    assert '"window_days": days' in slab


def test_roi_open_risk_not_windowed():
    """open_risk_remaining is a live snapshot — it must be counted
    before the window filter, not skipped for old findings."""
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    slab = body[idx:idx + 3800]
    open_at = slab.find("open_risk_remaining += risk")
    window_at = slab.find("if window_start is not None and (")
    assert open_at != -1 and window_at != -1
    # The open-risk accumulation + continue happens before the
    # terminal-state window filter.
    assert open_at < window_at


# ---------------------------------------------------------------------------
# compliance — CWE-561 drops the spurious PCI entry
# ---------------------------------------------------------------------------


def test_dead_code_cwe_has_no_pci_tag():
    from app.merge.compliance import compliance_for, compliance_tags

    mapping = compliance_for("CWE-561")
    # NIST CM-7 still applies; PCI does not.
    assert "nist_800_53" in mapping
    assert "pci_dss" not in mapping
    assert not any("PCI" in t for t in compliance_tags("CWE-561"))
