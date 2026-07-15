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
    """Open risk is exact-head current state and never time-windowed."""
    body = _read(_APP / "api" / "findings.py")
    idx = body.find("async def findings_roi(")
    slab = body[idx:idx + 3800]
    assert "current_findings_select(project_id)" in slab
    assert 'Finding.status.in_(("open", "acknowledged"))' in slab
    assert "func.sum(Finding.risk_score)" in slab
    # The current-head aggregate has no generated/resolved timestamp filter;
    # only terminal historical flow metrics use ``window_start``.
    current_start = slab.find("open_risk_stmt =")
    current_end = slab.find("open_risk_remaining =", current_start)
    assert current_start != -1 and current_end != -1
    assert "window_start" not in slab[current_start:current_end]


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
