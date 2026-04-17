"""Helpers used by summarisers to keep LLM inputs bounded.

All functions here are pure / synchronous so they can be unit-tested without
a database. They address the "large file + many files" pathology described
in docs/analysis-strategy.md §7.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


def evidence_hash(evidence: list[dict[str, Any]]) -> str:
    """Stable hash of the evidence list used to decide whether to re-summarise.

    The hash is order-insensitive (we sort on a stable key) and ignores the
    free-form ``data`` payloads on edges — only IDs + edge kinds + certainty
    are significant inputs.
    """
    keyed: list[tuple[str, str, str]] = []
    for ev in evidence:
        kind = ev.get("kind", "")
        identifier = str(ev.get("node_id") or ev.get("edge_id") or "")
        cert = str(ev.get("certainty") or ev.get("edge_kind") or "")
        keyed.append((kind, identifier, cert))
    keyed.sort()
    h = hashlib.sha256()
    h.update(json.dumps(keyed, separators=(",", ":")).encode())
    return h.hexdigest()


def _approx_tokens(obj: Any) -> int:
    """Rough token estimate (chars/4) — good enough for packing decisions."""
    return max(1, len(json.dumps(obj, default=str)) // 4)


def pack_by_budget(
    items: Iterable[dict[str, Any]],
    *,
    max_tokens: int = 3000,
) -> list[list[dict[str, Any]]]:
    """Split ``items`` into chunks whose per-chunk token estimate ≤ max_tokens.

    Used by L2/L3 summarisers when a file has 500+ L1 summaries or a module
    has many files: each chunk yields one partial summary, then a rollup
    pass condenses the partials.
    """
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_tokens = 0
    for item in items:
        t = _approx_tokens(item)
        if current and current_tokens + t > max_tokens:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(item)
        current_tokens += t
    if current:
        chunks.append(current)
    return chunks
