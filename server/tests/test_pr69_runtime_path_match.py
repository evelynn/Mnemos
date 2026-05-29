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

from app.merge.runtime import _operation_matches


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
    """The reconcile loop must route its candidate comparison through
    the matcher, not raw equality."""
    from pathlib import Path

    body = (
        Path(__file__).resolve().parents[1]
        / "app" / "merge" / "runtime.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def reconcile_observations(")
    slab = body[idx:idx + 2400]
    assert "_operation_matches(" in slab
