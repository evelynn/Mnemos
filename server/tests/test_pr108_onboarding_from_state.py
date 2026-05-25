"""PR-108 — derive onboarding card progress from platform state.

Pre-PR: ``updateOnboarding(projectCount)`` read ``mnemos_onboarding_step2_done``
and ``step3_done`` from ``localStorage``. Those flags were written
by click handlers in analysis.html / findings.html. Two failure
modes:

1. A new browser (Firefox vs Chrome, Incognito, post-cache-clear)
   has empty localStorage — even though the user IS past step 2/3
   on the platform — and sees the onboarding card forever.
2. A different user on the same browser inherits the previous
   user's localStorage flags and sees "done" lit up for steps
   they personally never completed.

This PR makes the platform the source of truth: when loadSummary
walks the project list it already fetches analysis_runs +
findings; we observe ``hasCompletedRun`` and ``hasFinding`` from
that walk and pass them into ``updateOnboarding``. No extra
fetches; no localStorage; no cross-browser drift.

A failed run still counts as step-2 completion: the operator
DID get analysis to actually try running, which is what the
step is teaching. Only perpetually-running runs don't help.
"""

from __future__ import annotations

from pathlib import Path

_DASH = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "dashboard"
    / "templates"
    / "dashboard.html"
)


def _read() -> str:
    return _DASH.read_text(encoding="utf-8")


def test_updateonboarding_takes_observed_object():
    body = _read()
    # Signature carries a second arg that holds the observed flags.
    assert "function updateOnboarding(projectCount, observed)" in body


def test_localstorage_flag_read_removed():
    """The pre-PR localStorage reads were the bug. They must be
    gone — otherwise we still consult them as the second source of
    truth and the card mis-lights on a fresh browser."""
    body = _read()
    idx = body.find("function updateOnboarding(")
    slab = body[idx:idx + 2500]
    assert "mnemos_onboarding_step2_done" not in slab
    assert "mnemos_onboarding_step3_done" not in slab


def test_step2_derived_from_completed_run_observation():
    body = _read()
    idx = body.find("function updateOnboarding(")
    slab = body[idx:idx + 2500]
    assert "step2Done = !!(observed && observed.hasCompletedRun)" in slab


def test_step3_derived_from_finding_observation():
    body = _read()
    idx = body.find("function updateOnboarding(")
    slab = body[idx:idx + 2500]
    assert "step3Done = !!(observed && observed.hasFinding)" in slab


def test_loadsummary_computes_both_flags_during_existing_walk():
    """The observed flags must be derived from the same project
    walk that already runs — adding new fetches would slow the
    dashboard. The detection is folded into the same runs/findings
    loop."""
    body = _read()
    # Inside the loadSummary walk: both flags get observed.
    idx = body.find("async function loadSummary(")
    slab = body[idx:idx + 5000]
    assert "let hasCompletedRun = false" in slab
    assert "let hasFinding = false" in slab
    # Each flag toggles to true on its own evidence.
    assert "hasCompletedRun = runRows.some(" in slab
    assert "hasFinding = true" in slab


def test_failed_run_counts_for_step2():
    """A failed analysis is still evidence the operator got far
    enough to TRY running one — the onboarding step is about
    successfully reaching the analysis trigger, not about a
    perfect first run."""
    body = _read()
    idx = body.find("hasCompletedRun = runRows.some(")
    slab = body[idx:idx + 400]
    assert '"completed"' in slab
    assert '"failed"' in slab


def test_card_hides_when_all_three_done():
    """The collapse behaviour stays unchanged — once the platform
    confirms all three steps, the card stops nagging."""
    body = _read()
    idx = body.find("function updateOnboarding(")
    slab = body[idx:idx + 2500]
    assert "onb.hidden = step1Done && step2Done && step3Done" in slab


def test_no_extra_fetches_added():
    """Cap the loadSummary outbound fetches at the original 1 +
    2*N (projects + analysis_runs + findings). Anything more
    would slow the dashboard. The flags are observed FROM the
    runs/findings responses, not from new endpoints."""
    body = _read()
    idx = body.find("async function loadSummary(")
    slab = body[idx:idx + 5000]
    # Only the three URL roots that existed pre-PR.
    fetches = sum(slab.count(p) for p in (
        "/api/v1/projects/",
        '"/api/v1/projects"',
    ))
    # Two project-keyed fetches (analysis_runs + findings) plus a
    # third pass for "latest" rendering, plus the initial list.
    # Definitely under 6 fetches total within this function.
    assert fetches <= 6


def test_updateonboarding_call_passes_observed():
    body = _read()
    # The single call site inside loadSummary hands the observed
    # flags object across.
    assert "updateOnboarding(rows.length, { hasCompletedRun, hasFinding })" in body
