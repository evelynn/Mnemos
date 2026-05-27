"""PR-114 — REAL analyzer execution against real fixtures.

Karpathy-policy strictest: tests that actually shell out to the
analyzer binary AND require a specific accuracy score. Up to
PR-113 the harness was unit-tested with hand-built envelopes;
this file proves the harness ALSO works against a REAL ggoss-ts
invocation extracting REAL data from a REAL TypeScript fixture.

Goal: catch the regression class of "harness works on mocks but
fails on real binary output" — exactly the class PR-111's
synthetic-only tests would miss. Verified against the bug found
during product-evaluation work: ggoss-ts pre-PR-114 missed every
arrow-function caller (``const f = () => g()``) because cmdCalls
only tracked FunctionDeclaration/MethodDeclaration as enclosing
scopes.

Skipped automatically when ``node`` is unavailable or the
analyzer source tree is gone — the test is opportunistic, not
required, so a contributor without node still gets a green
``pytest``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_TS_INDEX = _ROOT / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
_FIXTURE = _ROOT / "scripts" / "accuracy" / "fixtures" / "sample-ts-project"

sys.path.insert(0, str(_ROOT / "scripts" / "accuracy"))
import measure as harness  # type: ignore  # noqa: E402

_NODE = shutil.which("node")

requires_real_analyzer = pytest.mark.skipif(
    _NODE is None or not _TS_INDEX.is_file(),
    reason="node missing or analyzer source not present",
)


def _run_analyzer(verb: str) -> list[dict]:
    cp = subprocess.run(
        [_NODE, str(_TS_INDEX), verb, str(_FIXTURE)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"ggoss-ts {verb} failed: {cp.stderr[:400]}"
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


@requires_real_analyzer
def test_real_ggoss_ts_extracts_eight_symbols_from_fixture():
    """The fixture's expected.json lists 8 symbols; the real
    analyzer must emit all 8. This is the symbol-recall proof."""
    envs = _run_analyzer("symbols")
    symbols = [e for e in envs if e.get("record_type") == "symbol"]
    names = sorted(e["data"]["name"] for e in symbols)
    expected_names = sorted([
        "Order", "OrderId", "OrdersRepo",
        "add", "getById", "cancel",
        "createOrder", "confirmOrder",
    ])
    assert names == expected_names, (
        f"symbol set mismatch — analyzer regressed\n"
        f"  got     : {names}\n  expected: {expected_names}"
    )


@requires_real_analyzer
def test_real_ggoss_ts_detects_arrow_function_callees():
    """The bug PR-114 found and fixed: ``const confirmOrder = (...) => {
    ... getById(...) }`` — analyzer must extract the getById call
    from inside the arrow function body. Pre-fix this returned 0 edges
    for confirmOrder (regression at recall 0.667). Post-fix returns
    the expected call."""
    envs = _run_analyzer("calls")
    edges = [e for e in envs if e.get("record_type") == "edge"]
    # Look for an edge whose source carries the arrow-function's name
    # AND whose target resolved to getById inside the project.
    arrow_edges = [
        e for e in edges
        if "confirmOrder" in e["data"]["source_id"]
        and e["data"]["metadata"].get("callee_resolved") is True
        and "getById" in e["data"]["target_id"]
    ]
    assert arrow_edges, (
        "arrow-function callee not detected — the cmdCalls() "
        "enclosingFn fix regressed. "
        "Expected: confirmOrder → getById"
    )


@requires_real_analyzer
def test_real_ggoss_ts_meets_accuracy_floors_on_fixture():
    """The integration claim: against the curated fixture, the
    real analyzer scores at or above the configured floors
    (precision ≥ 0.85, recall ≥ 0.80, f1 ≥ 0.82).

    If this regresses, an accuracy-fix PR is required BEFORE the
    next release — the platform is making a graph-quality claim
    in getting-started.md."""
    envs = _run_analyzer("symbols") + _run_analyzer("calls")
    unwrapped = [harness._unwrap_payload(e) for e in envs]
    report = harness.measure("ts", "sample-ts-project", actual_envelopes=unwrapped)

    # Symbols floor.
    assert harness.meets_floors(report, "symbols"), (
        f"symbols below floor: {report['symbols']}"
    )
    # Edges floor (the bit that was 0.667 recall before the
    # arrow-function fix).
    assert harness.meets_floors(report, "edges"), (
        f"edges below floor: {report['edges']}"
    )
    # And the strongest claim: this fixture should score perfect.
    # If it doesn't, expected.json is out of sync with src/.
    assert report["symbols"]["f1"] == 1.0
    assert report["edges"]["f1"] == 1.0


@requires_real_analyzer
def test_real_ggoss_ts_extern_classification():
    """The fixture's ``add(order)`` calls ``this.rows.push(order)``
    — that's a builtin Array call, must show up under
    ``extern_edges_emitted``, NOT count against precision/recall."""
    envs = _run_analyzer("symbols") + _run_analyzer("calls")
    unwrapped = [harness._unwrap_payload(e) for e in envs]
    report = harness.measure("ts", "sample-ts-project", actual_envelopes=unwrapped)
    # this.rows.push + this.rows.find = 2 extern calls in the fixture.
    assert report["extern_edges_emitted"] >= 2, (
        f"extern edges miscounted: {report['extern_edges_emitted']}"
    )


@requires_real_analyzer
def test_real_ggoss_ts_runs_on_mnemos_own_ui_js():
    """Dogfood — point the analyzer at Mnemos's own dashboard
    static JS. Just proves the analyzer doesn't crash on
    real-world code (1700-line ui.js with IIFE wrapper, async
    functions, complex closures). Output count is informational."""
    static_dir = _ROOT / "server" / "app" / "dashboard" / "static"
    cp = subprocess.run(
        [_NODE, str(_TS_INDEX), "symbols", str(static_dir)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"dogfood symbols crashed: {cp.stderr[:400]}"
    lines = [line for line in cp.stdout.splitlines() if line.strip()]
    # ui.js has dozens of functions — empty result means the
    # analyzer broke. We don't assert an exact number because
    # ui.js keeps growing, but it's nowhere near zero.
    assert len(lines) >= 30, f"dogfood symbols suspiciously low: {len(lines)}"

    cp2 = subprocess.run(
        [_NODE, str(_TS_INDEX), "calls", str(static_dir)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp2.returncode == 0
    edge_lines = [line for line in cp2.stdout.splitlines() if line.strip()]
    assert len(edge_lines) >= 100, f"dogfood calls suspiciously low: {len(edge_lines)}"


def test_harness_extern_filter_correct():
    """The extern filter is the line between 'analyzer hallucinated'
    and 'analyzer correctly reports a builtin call' — getting this
    wrong destroys the precision metric. Verify the filter on
    sample envelopes."""
    extern = {
        "kind": "CALLS", "source": "X",
        "target_id": "ts:extern:Array.push",
        "metadata": {"callee_resolved": False},
    }
    in_proj = {
        "kind": "CALLS", "source": "X",
        "target_id": "ts:orders.ts:getById@22:3",
        "metadata": {"callee_resolved": True},
    }
    assert harness._is_extern_edge(extern) is True
    assert harness._is_extern_edge(in_proj) is False


def test_bare_symbol_name_normalisation():
    """The id-normalisation that makes hand-written fixtures
    portable against analyzer-emitted ids. Tested directly so
    a future analyzer that emits a different format doesn't
    silently zero out the accuracy score."""
    cases = [
        ("ts:orders.ts:createOrder@34:1", "createOrder"),
        ("ts:extern:this.rows.push",      "this.rows.push"),
        ("contract:POST /orders",         "POST /orders"),
        ("createOrder",                   "createOrder"),
        ("",                              ""),
    ]
    for raw, want in cases:
        got = harness._bare_symbol_name(raw)
        assert got == want, f"_bare_symbol_name({raw!r}) → {got!r}, want {want!r}"
