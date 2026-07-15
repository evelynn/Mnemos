"""PR-67 — human certainty feedback loop (spec §1.2, core-value A/B).

Spec §1.2 says the graph's certainty must not stay frozen at whatever
the analyzers first guessed — facts confirmed while *using* the
platform strengthen it. PR-25 implemented the runtime (OTLP) half of
that loop; the *human* half — an operator confirming an inferred fact
during Q&A / graph review — was never built.

This PR adds ``POST /graph/confirm_fact``: a confirmation lifts an
``inferred`` fact to ``asserted`` (§2.3 — a human is one trustworthy
source), records who/when/why in the fact's ``data`` JSONB (no schema
change, same mechanism as PR-25's ``exercised`` stamp), and the graph
tab gains a confirm/dispute form.
"""

from __future__ import annotations

from pathlib import Path

from app.api.analysis import _confirmation_action, _strengthened_certainty

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _strengthened_certainty — the §1.2 + §2.3 promotion rule
# ---------------------------------------------------------------------------


def test_confirm_lifts_inferred_to_asserted():
    assert _strengthened_certainty("confirm", "inferred") == "asserted"


def test_confirm_never_reaches_verified():
    """``verified`` is reserved for the system observing a fact
    directly — a human assertion must not forge it."""
    assert _strengthened_certainty("confirm", "asserted") == "asserted"
    assert _strengthened_certainty("confirm", "verified") == "verified"


def test_dispute_never_changes_certainty():
    """A dispute records doubt; it must not silently rewrite the
    analyzer's certainty in either direction."""
    for c in ("inferred", "asserted", "verified"):
        assert _strengthened_certainty("dispute", c) == c


# ---------------------------------------------------------------------------
# _confirmation_action — surfacing the human verdict on graph reads
# ---------------------------------------------------------------------------


def test_confirmation_action_extracted_from_data():
    data = {"human_confirmation": {"action": "confirm", "by": "user:1"}}
    assert _confirmation_action(data) == "confirm"


def test_confirmation_action_absent_is_none():
    assert _confirmation_action({}) is None
    assert _confirmation_action({"human_confirmation": "garbage"}) is None


# ---------------------------------------------------------------------------
# Endpoint — definition, RBAC, body model
# ---------------------------------------------------------------------------


def test_confirm_fact_endpoint_defined():
    body = _read(_APP / "api" / "analysis.py")
    assert '"/projects/{project_id}/graph/confirm_fact"' in body
    assert "async def confirm_fact(" in body


def test_confirm_fact_is_org_scoped_and_operator_only():
    body = _read(_APP / "api" / "analysis.py")
    idx = body.find("async def confirm_fact(")
    decorator = body[max(0, idx - 260):idx]
    assert "require_project_org" in decorator
    assert "require_operator" in decorator


def test_confirm_fact_persists_who_when_why():
    body = _read(_APP / "api" / "analysis.py")
    idx = body.find("async def confirm_fact(")
    slab = body[idx:idx + 2400]
    assert '"human_confirmation"' in slab
    assert '"rationale"' in slab
    # The bitemporal current row is updated in place, like PR-25.
    assert "Edge.valid_from == row.valid_from" in slab
    assert "audit_record(" in body[idx:idx + 3000]


def test_component_map_surfaces_confirmation_and_edge_id():
    """The graph payload must carry the confirmation verdict (so the
    UI can colour it) and the edge id (so an operator can confirm a
    specific edge)."""
    body = _read(_APP / "api" / "analysis.py")
    idx = body.find("async def graph_component_map(")
    # Scope to the function body (up to the next endpoint) so an edit
    # earlier in the function can't push these past a fixed-size window.
    nxt = body.find("async def ", idx + 1)
    slab = body[idx:nxt if nxt != -1 else len(body)]
    assert '"confirmed": _confirmation_action(' in slab
    assert '"id": str(e.id)' in slab


# ---------------------------------------------------------------------------
# GUI — confirm/dispute form on the graph tab
# ---------------------------------------------------------------------------


def test_graph_template_has_confirm_form():
    body = _read(_APP / "dashboard" / "templates" / "graph.html")
    assert 'data-i18n="Confirm a graph fact"' in body
    assert "submitFactConfirmation" in body
    assert "/graph/confirm_fact" in body
    # Clicking a node pre-fills the form.
    assert "_prefillConfirm(" in body


def test_phrase_book_translates_confirmation_strings():
    body = _read(_APP / "dashboard" / "static" / "ui.js")
    for kr in ("그래프 사실 확인", "근거", "이의 제기", "기록됨"):
        assert kr in body, f"phrase book missing {kr!r}"
