from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import app.extractor.packing as packing
from app.extractor.packing import (
    _approx_tokens,
    evidence_hash,
    pack_by_budget,
    serialize_evidence,
)


def _historical_evidence_hash(evidence):
    canonical = []
    for ev in evidence:
        item = dict(ev)
        if item.get("kind") == "edge":
            item.pop("edge_id", None)
        canonical.append(item)
    canonical.sort(
        key=lambda item: (
            str(item.get("kind") or ""),
            str(item.get("node_id") or item.get("source_id") or ""),
            str(item.get("target_id") or ""),
            str(item.get("edge_kind") or ""),
            json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ),
        )
    )
    digest = hashlib.sha256()
    digest.update(b"mnemos.evidence.v2\0")
    digest.update(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    )
    return digest.hexdigest()


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


def test_evidence_hash_matches_historical_canonical_contract():
    fixed_uuid = uuid.UUID("12345678-1234-5678-1234-567812345678")
    fixed_time = datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc)
    duplicate = {
        "kind": "node",
        "node_id": "sym:duplicate",
        "data": {"unicode": "근거 β é", "nested": {"b": 2, "a": 1}},
    }
    evidence = [
        duplicate,
        {
            "data": {"nested": {"a": 1, "b": 2}, "unicode": "근거 β é"},
            "node_id": "sym:duplicate",
            "kind": "node",
        },
        {
            "kind": "node",
            "node_id": "sym:typed-values",
            "data": {
                "uuid": fixed_uuid,
                "observed_at": fixed_time,
                "amount": Decimal("123.4500"),
            },
        },
        {
            "kind": "edge",
            "edge_id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            "edge_kind": "CALLS",
            "source_id": "sym:duplicate",
            "target_id": "sym:typed-values",
            "certainty": "asserted",
        },
    ]

    expected = _historical_evidence_hash(evidence)
    assert evidence_hash(evidence) == expected
    assert evidence_hash(list(reversed(evidence))) == expected

    rewritten_edge = [
        {
            **evidence[-1],
            "edge_id": uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
        }
    ]
    assert evidence_hash([evidence[-1]]) == evidence_hash(rewritten_edge)
    assert evidence_hash(rewritten_edge) == _historical_evidence_hash(rewritten_edge)


def test_evidence_hash_serializes_each_canonical_item_once(monkeypatch):
    evidence = [
        {
            "kind": "node",
            "node_id": f"sym:{index}",
            "data": {"name": f"Service{index}", "summary": "근거" * 8},
        }
        for index in range(100)
    ]
    expected = _historical_evidence_hash(evidence)
    real_dumps = json.dumps
    serialized_objects = []

    def counted_dumps(obj, *args, **kwargs):
        serialized_objects.append(obj)
        return real_dumps(obj, *args, **kwargs)

    monkeypatch.setattr(packing.json, "dumps", counted_dumps)

    assert evidence_hash(evidence) == expected
    assert len(serialized_objects) == len(evidence)
    assert all(isinstance(obj, dict) for obj in serialized_objects)


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

    assert serialize_evidence(first) == json.dumps(
        first, default=str, sort_keys=True
    )
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


def test_pack_avoids_repeatedly_serializing_complete_prefixes(monkeypatch):
    items = [
        {
            "kind": "node",
            "node_id": f"sym:{index}",
            "data": {"name": f"Service{index}", "summary": "근거" * 8},
        }
        for index in range(100)
    ]
    real_dumps = json.dumps
    canonical_size = len(real_dumps(items, default=str, sort_keys=True).encode("utf-8"))
    serialized_bytes = 0

    def counted_dumps(*args, **kwargs):
        nonlocal serialized_bytes
        rendered = real_dumps(*args, **kwargs)
        serialized_bytes += len(rendered.encode("utf-8"))
        return rendered

    monkeypatch.setattr(packing.json, "dumps", counted_dumps)

    assert pack_by_budget(items, max_tokens=canonical_size) == [items]
    # One per-item bound check, one per-item cached size, and one final
    # independent whole-chunk assertion stay linear in the input bytes.
    assert serialized_bytes < canonical_size * 4
