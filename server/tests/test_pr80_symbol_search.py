"""PR-80 — symbol search upgraded from substring match to ranked
multi-term lexical search (spec §11.3).

An assessment of the Q&A subsystem found ``search_symbols`` — the
entry point for "where is X?" questions — was a plain ``ILIKE
%query%`` substring match. A search for "payment retry" therefore
could not find ``retryPayment``: the literal phrase wasn't a
substring. Spec §11.3 mandates a vector + BM25 ensemble.

This PR implements the lexical (BM25-ish) half: the query is
tokenised and each symbol scored by term coverage weighted by field
(name > id > signature). ``get_symbol`` also now returns the L1
summary, as §11.3 specifies. The vector half needs an embedding
model and is out of scope here.
"""

from __future__ import annotations

from app.mcp.queries import _score_symbol, _tokenize


# ---------------------------------------------------------------------------
# _tokenize
# ---------------------------------------------------------------------------


def test_tokenize_splits_and_lowercases():
    assert _tokenize("Payment Retry!") == ["payment", "retry"]
    # snake_case keeps the whole identifier AND its parts (PR-186), so an
    # exact symbol query (``should_compress``) matches that symbol while the
    # parts still drive cross-convention concept matching.
    assert _tokenize("find-callers_v2") == ["find", "callers_v2", "callers", "v2"]
    assert _tokenize("order_create") == ["order_create", "order", "create"]
    assert _tokenize("") == []
    assert _tokenize(None) == []


# ---------------------------------------------------------------------------
# _score_symbol
# ---------------------------------------------------------------------------


def test_multiterm_query_matches_camelcase_symbol():
    """'payment retry' must find retryPayment — the defect this fixes:
    the old substring match needed the literal phrase."""
    score = _score_symbol(
        ["payment", "retry"], "retryPayment", "ts:p.ts:retryPayment@1:1", None
    )
    assert score > 0


def test_name_hit_outranks_id_hit():
    by_name = _score_symbol(["login"], "loginHandler", "ts:a:x@1:1", None)
    by_id = _score_symbol(["login"], "handler", "ts:auth/login.ts:x@1:1", None)
    assert by_name > by_id > 0


def test_exact_name_match_scores_highest():
    exact = _score_symbol(["login"], "login", "ts:a:login@1:1", None)
    partial = _score_symbol(["login"], "loginHandler", "ts:a:x@1:1", None)
    assert exact > partial


def test_full_term_coverage_bonus():
    """A symbol matching every query term outranks one matching some."""
    both = _score_symbol(["payment", "retry"], "retryPayment", "ts:x", None)
    one = _score_symbol(["payment", "retry"], "paymentForm", "ts:x", None)
    assert both > one


def test_no_match_scores_zero():
    assert _score_symbol(["payment"], "userProfile", "ts:user.ts:x", None) == 0.0
    # An empty query is not a match either.
    assert _score_symbol([], "anything", "ts:x", None) == 0.0


def test_signature_is_searched_but_weak():
    """A term only in the signature still matches — weakly."""
    sig = _score_symbol(
        ["amount"], "process", "ts:x", "function process(amount: number)"
    )
    assert 0 < sig < _score_symbol(["amount"], "amount", "ts:x", None)


# ---------------------------------------------------------------------------
# Source-level — search_symbols ranks, get_symbol returns the L1 summary
# ---------------------------------------------------------------------------


def test_search_symbols_is_ranked():
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "mcp" / "queries.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def search_symbols(")
    slab = body[idx:idx + 5500]
    assert "_tokenize(query)" in slab
    assert "_score_symbol(" in slab
    assert "scored.sort(" in slab
    assert '"score"' in slab


def test_get_symbol_returns_l1_summary():
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1] / "app" / "mcp" / "queries.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def get_symbol(")
    slab = body[idx:idx + 2200]
    assert '"l1_summary"' in slab
    assert "Summary.level == 1" in slab
