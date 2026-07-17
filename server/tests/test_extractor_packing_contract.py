from __future__ import annotations

import uuid

from app.extractor.packing import (
    _approx_tokens,
    evidence_hash,
    pack_by_budget,
    serialize_evidence,
)


def test_evidence_hash_tracks_payload_and_child_summary_changes():
    base = [{"kind": "node", "node_id": "sym:a", "data": {"signature": "a()"}}]
    changed = [{"kind": "node", "node_id": "sym:a", "data": {"signature": "a(x)"}}]
    assert evidence_hash(base) != evidence_hash(changed)

    child_a = [{"kind": "node", "node_id": "a.py", "l1_summary": "reads users"}]
    child_b = [{"kind": "node", "node_id": "a.py", "l1_summary": "writes users"}]
    assert evidence_hash(child_a) != evidence_hash(child_b)


def test_physical_edge_uuid_is_not_semantic_evidence():
    logical = {
        "kind": "edge",
        "edge_kind": "CALLS",
        "source_id": "sym:a",
        "target_id": "sym:b",
        "certainty": "asserted",
    }
    first = [{**logical, "edge_id": str(uuid.uuid4())}]
    second = [{**logical, "edge_id": str(uuid.uuid4())}]
    assert evidence_hash(first) == evidence_hash(second)


def test_nested_mapping_insertion_order_cannot_change_prompt_or_reservation():
    first = [
        {
            "kind": "node",
            "node_id": "sym:a",
            "data": {"z": {"b": 2, "a": 1}, "a": "first"},
        }
    ]
    second = [
        {
            "data": {"a": "first", "z": {"a": 1, "b": 2}},
            "node_id": "sym:a",
            "kind": "node",
        }
    ]

    assert serialize_evidence(first) == serialize_evidence(second)
    assert evidence_hash(first) == evidence_hash(second)
    assert _approx_tokens(first) == _approx_tokens(second)


def test_single_oversized_item_is_bounded_with_digest():
    chunks = pack_by_budget(
        [{"kind": "node", "node_id": "sym:a", "data": "x" * 50_000}],
        max_tokens=192,
    )
    assert len(chunks) == 1
    assert len(chunks[0]) == 1
    assert chunks[0][0]["truncated"] is True
    assert chunks[0][0]["node_id"] == "sym:a"
    assert _approx_tokens(chunks[0]) <= 192


def test_pack_hard_bound_includes_container_overhead_and_hostile_ids():
    chunks = pack_by_budget(
        [
            {
                "kind": "node",
                "node_id": "secret-" + "x" * 20_000,
                "data": "\\\"" * 20_000,
            },
            {"kind": "node", "node_id": "sym:small", "data": "y" * 300},
        ],
        max_tokens=128,
    )

    assert chunks
    assert all(_approx_tokens(chunk) <= 128 for chunk in chunks)
    assert chunks[0][0]["truncated"] is True
    assert "content_sha256" in chunks[0][0]
