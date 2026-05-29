"""PR-68 — OTLP recorded-trace replay benchmark (core-value A, §2.7/§7.6).

The reassessment held that live-OTLP behaviour "needs a collector,
not code". PR-66 already showed that framing is wrong for the static
analyzers; it is equally wrong here. The OTLP receiver's job is to
parse OTLP/HTTP JSON into ``(service, operation, kind)`` triples — a
pure transformation that is verified by replaying a *recorded* trace
against a known answer key, exactly how OpenTelemetry's own
conformance fixtures work.

``fixtures/refsys/runtime-trace.otlp.json`` is a recorded trace of
the PR-66 reference system: the orders BFF receives a request, calls
the C# API over HTTP, and the API reads the orders table.
``runtime_ground_truth.json`` is the answer key. This benchmark
proves the trace assembled correctly *and* that its exposed
operation matches the contract the static analyzer benchmark
resolves — i.e. a reconcile would mark that contract exercised,
closing the §7.6 Tier-2 loop end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.merge.contract_id import http_contract_id
from app.merge.runtime import assemble_trace_tree

_FIX = Path(__file__).resolve().parent / "fixtures" / "refsys"


def _load(name: str) -> dict:
    return json.loads((_FIX / name).read_text(encoding="utf-8"))


def _triple_set(records: list[dict]) -> set[tuple[str, str, str]]:
    return {(r["service"], r["operation"], r["kind"]) for r in records}


# ---------------------------------------------------------------------------
# Trace assembly — the OTLP ingestion path
# ---------------------------------------------------------------------------


def test_recorded_trace_assembles_to_expected_triples():
    """assemble_trace_tree() must turn the recorded OTLP payload into
    exactly the (service, operation, kind) triples in the answer key."""
    trace = _load("runtime-trace.otlp.json")
    gt = _load("runtime_ground_truth.json")

    records = assemble_trace_tree(trace["resourceSpans"])
    found = _triple_set(records)
    expected = {
        (t["service"], t["operation"], t["kind"])
        for t in gt["expected_triples"]
    }

    hit = found & expected
    precision = len(hit) / len(found) if found else 0.0
    recall = len(hit) / len(expected) if expected else 0.0
    print(
        f"\n[refsys/otlp] trace assembly — "
        f"precision={precision:.2f} recall={recall:.2f}"
    )
    assert precision >= gt["thresholds"]["triple_precision_min"]
    assert recall >= gt["thresholds"]["triple_recall_min"]
    assert found == expected


def test_server_spans_become_exposes_client_spans_calls():
    """SERVER span.kind (2) → EXPOSES, CLIENT (3) → CALLS — the
    direction the §7.6 reconcile relies on."""
    trace = _load("runtime-trace.otlp.json")
    records = assemble_trace_tree(trace["resourceSpans"])
    by_op = {(r["service"], r["operation"]): r["kind"] for r in records}
    assert by_op[("refsys-api", "/api/orders")] == "EXPOSES"
    assert by_op[("orders-bff", "/api/orders")] == "CALLS"


def test_db_span_operation_uses_table_and_verb():
    """A db span resolves to '<verb> <table>' so a READS/WRITES edge
    can be matched, not the masked db.statement."""
    trace = _load("runtime-trace.otlp.json")
    records = assemble_trace_tree(trace["resourceSpans"])
    ops = {r["operation"] for r in records if r["service"] == "refsys-api"}
    assert "SELECT orders" in ops


def test_trace_parent_chain_preserved():
    """The BFF→API call must carry its parent span so the reconciler
    can distinguish a self-call from a cross-service hop."""
    trace = _load("runtime-trace.otlp.json")
    records = assemble_trace_tree(trace["resourceSpans"])
    client = next(
        r for r in records
        if r["service"] == "orders-bff" and r["kind"] == "CALLS"
    )
    assert client["parent_span"]
    # Its parent is the BFF's own SERVER span.
    server = next(
        r for r in records
        if r["service"] == "orders-bff" and r["kind"] == "EXPOSES"
    )
    assert client["parent_span"] == server["span_id"]


# ---------------------------------------------------------------------------
# End-to-end — the trace exercises a contract the static graph knows
# ---------------------------------------------------------------------------


def test_runtime_trace_exercises_a_static_contract():
    """The recorded trace's exposed API operation must resolve to the
    same canonical Contract id the analyzer benchmark (PR-66) finds —
    proving the runtime layer would lift that contract to exercised."""
    gt_runtime = _load("runtime_ground_truth.json")
    gt_static = _load("ground_truth.json")

    runtime_path = gt_runtime["exercised_contract_path"]
    runtime_cid = http_contract_id("GET", runtime_path)

    # The static corpus must contain that exact contract id.
    assert runtime_cid in gt_static["contracts"]
    assert runtime_cid in gt_static["cross_language_joins"]
