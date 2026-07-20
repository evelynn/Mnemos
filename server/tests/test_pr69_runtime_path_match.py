"""PR-69 — runtime↔static path-template matching + corpus decoys.

The final reassessment found a real bug: ``reconcile_observations``
matched a runtime-observed operation against a stored edge operation
with raw string equality. An OTel ``http.route`` is the *templated*
path (``/api/orders/{id}``) while a static analyzer may have stored a
literal one (``/api/orders/42``) — so the §7.6 Tier-2 reconcile
silently never marked those edges ``exercised``.

``_operation_matches`` now compares both raw and after the same
path-template normalisation contract ids use. (The companion corpus
hardening — ``noise.ts`` decoys — is asserted by test_pr66.)
"""

from __future__ import annotations

import pytest

from app.merge.runtime import _operation_from_node_id, _operation_matches


def test_exact_match():
    assert _operation_matches("/api/orders", "/api/orders")


def test_literal_matches_templated_path():
    """A runtime trace's templated /api/orders/{id} must match a
    stored literal /api/orders/42 — the bug this PR fixes."""
    assert _operation_matches("/api/orders/42", "/api/orders/{id}")
    assert _operation_matches("/api/orders/{id}", "/api/orders/99")


def test_distinct_paths_do_not_match():
    assert not _operation_matches("/api/orders", "/api/customers")
    assert not _operation_matches("/api/orders/{id}", "/api/orders")


def test_db_operations_match_only_raw():
    """A db operation ('SELECT orders') is not a path — it must match
    exactly and never be run through path normalisation."""
    assert _operation_matches("SELECT orders", "SELECT orders")
    assert not _operation_matches("SELECT orders", "INSERT orders")
    # Mixed path / non-path never matches.
    assert not _operation_matches("SELECT orders", "/api/orders")


def test_none_and_empty_are_safe():
    assert not _operation_matches(None, "/api/orders")
    assert not _operation_matches("/api/orders", "")


def test_reconcile_uses_the_matcher():
    """The reconcile loop must route its candidate comparison through the
    matcher contract, not raw equality.

    The loop now resolves candidates through a prebuilt key index instead
    of calling the pairwise matcher per edge, so the structural check
    tracks ``_match_keys`` — and the equivalence test below is what
    actually pins the semantics.
    """
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "merge" / "runtime.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def reconcile_observations(")
    end = body.find("\nasync def ", idx + 1)
    slab = body[idx:end if end != -1 else len(body)]
    assert "_match_keys(" in slab


@pytest.mark.parametrize(
    "candidate",
    [
        "/api/orders",
        "/api/orders/42",
        "/api/orders/{id}",
        "SELECT orders",
        "INSERT orders",
        "",
        None,
        "/",
    ],
)
@pytest.mark.parametrize(
    "observed",
    ["/api/orders", "/api/orders/42", "/api/orders/{id}", "SELECT orders", ""],
)
def test_match_keys_index_agrees_with_pairwise_matcher(candidate, observed):
    """The key index must select exactly what the pairwise matcher does.

    This is the invariant the reconcile refactor rests on: a shared key
    between the two sides is equivalent to ``_operation_matches``.
    """
    from app.merge.runtime import _match_keys

    index_hit = bool(set(_match_keys(candidate)) & set(_match_keys(observed)))
    assert index_hit is _operation_matches(candidate, observed)


# ---------------------------------------------------------------------------
# PR-159 — the route a runtime observation must match usually lives on the
# *target contract node id* (e.g. ``contract:POST /orders``), not on
# ``edge.data``. Without parsing it out the standard EXPOSES/CALLS edge
# never reconciles and every observation is stuck unmatched.
# ---------------------------------------------------------------------------


def test_operation_from_node_id_parses_each_contract_shape():
    # seed + merge canonical
    assert _operation_from_node_id("contract:POST /orders") == "/orders"
    assert _operation_from_node_id("contract:GET /orders/{id}") == "/orders/{id}"
    # ggoss-py analyzer shape
    assert _operation_from_node_id("contract:http:POST:/orders") == "/orders"
    # contract_id.http_contract_id shape
    assert _operation_from_node_id("http.POST./orders") == "/orders"


def test_operation_from_node_id_ignores_non_http_nodes():
    for nid in (
        "sym:orders-api:CreateOrder",
        "data:orders.dbo.Orders",
        "comp:orders-api",
        "",
        None,
    ):
        assert _operation_from_node_id(nid) is None


def test_observed_route_matches_contract_node_path():
    """End-to-end of the matcher pair: a runtime ``/orders/42`` resolves
    against a ``contract:GET /orders/{id}`` target node via path templating."""
    assert _operation_matches(
        _operation_from_node_id("contract:POST /orders"), "/orders"
    )
    assert _operation_matches(
        _operation_from_node_id("contract:GET /orders/{id}"), "/orders/42"
    )
    # distinct routes still don't match
    assert not _operation_matches(
        _operation_from_node_id("contract:POST /customers"), "/orders"
    )
