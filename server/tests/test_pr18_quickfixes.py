"""PR-18 quick-fix verifications (6th-round self-review)."""

from __future__ import annotations

from pathlib import Path

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates"
_API = Path(__file__).resolve().parents[1] / "app" / "api"
_DOCS = Path(__file__).resolve().parents[2] / "docs" / "operator-guide"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# C4: submit_diff requires operator role
# ---------------------------------------------------------------------------


def test_submit_diff_requires_operator():
    """6th-round audit C4: a viewer must not be able to post diffs and
    burn ultrareview cycles.
    """
    body = _read(_API / "diffs.py")
    # Find the submit_diff handler — it should depend on require_operator.
    lines = body.splitlines()
    in_submit = False
    saw_require_operator = False
    for i, line in enumerate(lines):
        if 'async def submit_diff(' in line:
            in_submit = True
            continue
        if in_submit:
            if line.startswith(") -> DiffOut:"):
                break
            if "require_operator" in line:
                saw_require_operator = True
                break
    assert saw_require_operator, (
        "submit_diff must depend on require_operator — viewer must not "
        "be able to post diffs (6th-round audit C4)"
    )


# ---------------------------------------------------------------------------
# S4: dashboard hash redirect to /diffs
# ---------------------------------------------------------------------------


def test_base_html_redirects_approve_hash_to_diffs():
    body = _read(_TPL / "base.html")
    assert 'approve=' in body
    assert 'window.location.replace("/diffs#' in body


def test_base_html_does_not_redirect_when_already_on_diffs():
    body = _read(_TPL / "base.html")
    # The guard prevents an infinite loop where /diffs redirects to /diffs.
    assert 'onDiffs' in body or 'pathname === "/diffs"' in body


# ---------------------------------------------------------------------------
# B3: SSE toast message clarity
# ---------------------------------------------------------------------------


def test_sse_failure_toast_mentions_monitor_button():
    body = _read(_TPL / "analysis.html")
    # The previous "refresh to retry" message was ambiguous. The new
    # text must point at the Monitor button explicitly.
    assert "Monitor button" in body
    # And still acknowledge the page-reload alternative for users
    # who want a clean slate.
    assert "reload the page" in body


# ---------------------------------------------------------------------------
# Phase 2 backlog doc
# ---------------------------------------------------------------------------


def test_phase2_backlog_doc_exists():
    p = _DOCS / "phase2_backlog.md"
    assert p.exists(), "Phase 2 backlog doc must be created"
    body = p.read_text(encoding="utf-8")
    # Every deferred item the 5/6th-round audits flagged should have a
    # P2-* identifier so a future PR can link to it.
    for key in (
        "P2-1",       # OTLP Tier 2
        "P2-2",       # RuntimeObservation table
        "P2-3",       # i18n
        "P2-4",       # relative timestamps
        "P2-5",       # color-blind badge glyphs
        "P2-6",       # large-result pagination
    ):
        assert key in body, f"Phase 2 backlog must include section {key}"


def test_phase2_backlog_doc_is_linked_internally():
    """The backlog only does its job if humans can find it. The PR-18
    commit message will link to it; this test just guards the file's
    structure stays referenceable (anchor headings present)."""
    body = (_DOCS / "phase2_backlog.md").read_text(encoding="utf-8")
    # Section anchors GitHub Markdown derives.
    assert body.startswith("# Phase 2 backlog")
