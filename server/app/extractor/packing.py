"""Helpers used by summarisers to keep LLM inputs bounded.

All functions here are pure / synchronous so they can be unit-tested without
a database. They address the "large file + many files" pathology described
in docs/analysis-strategy.md §7.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable


MAX_EVIDENCE_PROMPT_CHARS = 16_000
# Finite allowance for provider message framing/system delimiters around
# Mnemos-controlled text.  Direct adapters reserve this on top of the UTF-8
# byte upper bound; opaque Agent SDK paths reserve the full model context.
PROVIDER_INPUT_ENVELOPE_TOKENS = 256
_PROMPT_JSON_SEPARATORS = (", ", ": ")
_PROMPT_JSON_LIST_SEPARATOR_BYTES = len(
    _PROMPT_JSON_SEPARATORS[0].encode("utf-8")
)


class EvidencePromptTooLarge(ValueError):
    """Complete evidence JSON cannot fit the provider input contract."""


def _serialize_prompt_json(obj: Any) -> str:
    """Use one canonical JSON dialect for prompt text and byte accounting."""

    return json.dumps(
        obj,
        default=str,
        sort_keys=True,
        separators=_PROMPT_JSON_SEPARATORS,
    )


def serialize_evidence(
    evidence: list[dict[str, Any]],
    *,
    max_chars: int = MAX_EVIDENCE_PROMPT_CHARS,
) -> str:
    """Serialize one complete evidence scope or reject it before a call.

    Blind character slicing can leave invalid JSON and expose IDs to a model
    that the downstream validator never saw.  Provider adapters share this
    function so their prompt contains exactly the rows accepted as the
    validation scope.
    """

    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    serialized = _serialize_prompt_json(evidence)
    if len(serialized) > max_chars:
        raise EvidencePromptTooLarge(
            f"evidence JSON exceeds hard limit ({len(serialized)} > {max_chars})"
        )
    return serialized


def evidence_hash(evidence: list[dict[str, Any]]) -> str:
    """Stable hash of the evidence list used to decide whether to re-summarise.

    Hash semantic evidence, including node payloads and child-summary text.
    Physical edge UUIDs are deliberately removed: a bitemporal rewrite of the
    same logical ``(source, target, kind)`` edge is not new evidence.
    """
    canonical: list[tuple[str, str, str, str, str]] = []
    for ev in evidence:
        item = dict(ev)
        if item.get("kind") == "edge":
            item.pop("edge_id", None)
        serialized = json.dumps(
            item,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        canonical.append(
            (
                str(item.get("kind") or ""),
                str(item.get("node_id") or item.get("source_id") or ""),
                str(item.get("target_id") or ""),
                str(item.get("edge_kind") or ""),
                serialized,
            )
        )
    canonical.sort()

    # Reuse the exact JSON text that completed the historical sort key.  A
    # compact JSON list is precisely ``[item,item]``, so streaming those bytes
    # avoids serializing every evidence row a second time and avoids allocating
    # one full-list JSON string plus its encoded copy.
    h = hashlib.sha256(b"mnemos.evidence.v2\0[")
    separator = b""
    for item in canonical:
        h.update(separator)
        h.update(item[4].encode())
        separator = b","
    h.update(b"]")
    return h.hexdigest()


def _json_utf8_size(obj: Any) -> int:
    """Return the exact UTF-8 byte size of Mnemos' canonical prompt JSON."""

    return len(_serialize_prompt_json(obj).encode("utf-8"))


def _approx_tokens(obj: Any) -> int:
    """Return a tokenizer-independent upper bound for JSON input tokens.

    Provider tokenizers are not interchangeable and ``characters / 4`` is
    not an upper bound for source code, Korean text, or adversarial byte
    sequences.  Canonical JSON's UTF-8 byte length is conservative because a
    text token must consume at least one input byte.  Callers add their finite
    provider-envelope allowance separately.

    The historical private name remains to avoid a broad compatibility
    break; its semantics are now a hard upper-bound reservation, not an
    approximation.
    """

    return max(1, _json_utf8_size(obj))


def _bounded_item(item: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Replace one oversized item with identity + digest + bounded preview."""

    if _approx_tokens([item]) <= max_tokens:
        return item
    serialized = json.dumps(
        item, sort_keys=True, separators=(",", ":"), default=str
    )
    bounded: dict[str, Any] = {
        "truncated": True,
        "content_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
    }
    # Add identity fields only while the complete one-item JSON chunk fits.
    # This prevents an adversarially long node id escaping the pack bound.
    for key in (
        "kind",
        "node_id",
        "source_id",
        "target_id",
        "edge_kind",
        "certainty",
    ):
        if key not in item:
            continue
        candidate = {**bounded, key: item[key]}
        if _approx_tokens([candidate]) <= max_tokens:
            bounded = candidate

    # JSON escaping makes char slicing nonlinear, so find the largest preview
    # that still fits using the same final estimator.
    low, high = 0, len(serialized)
    while low < high:
        midpoint = (low + high + 1) // 2
        candidate = {**bounded, "preview": serialized[:midpoint]}
        if _approx_tokens([candidate]) <= max_tokens:
            low = midpoint
        else:
            high = midpoint - 1
    if low:
        bounded["preview"] = serialized[:low]
    if _approx_tokens([bounded]) > max_tokens:
        raise ValueError("max_tokens is too small for truncated evidence")
    return bounded


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
    # A privacy-safe truncation sentinel includes a full SHA-256 digest and
    # therefore cannot fit under the byte-based token upper bound below 128.
    if max_tokens < 128:
        raise ValueError("max_tokens must be >= 128")
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    # Default ``json.dumps`` renders a list as ``[item, item]``.  Track that
    # exact byte size incrementally so appending N items does not repeatedly
    # serialize the complete prefix.  ``ensure_ascii=True`` is intentionally
    # unchanged, so provider input, chunk boundaries, and validator scope are
    # byte-for-byte identical to the historical implementation.
    current_size = 2  # ``[]``
    for item in items:
        bounded_item = _bounded_item(item, max_tokens)
        item_size = _json_utf8_size(bounded_item)
        candidate_size = current_size + item_size + (
            _PROMPT_JSON_LIST_SEPARATOR_BYTES if current else 0
        )
        if current and candidate_size > max_tokens:
            chunks.append(current)
            current = []
            current_size = 2
            candidate_size = current_size + item_size
        if candidate_size > max_tokens:
            raise ValueError("bounded evidence item exceeds max_tokens")
        current.append(bounded_item)
        current_size = candidate_size
    if current:
        chunks.append(current)
    if any(_approx_tokens(chunk) > max_tokens for chunk in chunks):
        raise AssertionError("evidence pack exceeded max_tokens")
    return chunks
