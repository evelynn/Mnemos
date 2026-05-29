"""PR-111 — analyzer accuracy harness behavioural tests.

Biggest unmeasured product risk: graph quality is the
value-driver but no PR has ever quantified analyzer accuracy.
SonarQube publishes per-rule precision/recall; Mnemos shipped
5 analyzers with zero numbers.

PR-111 ships ``scripts/accuracy/measure.py`` — a harness that:
1. invokes each analyzer the same way the platform does
2. compares output against a hand-curated expected.json
3. reports precision/recall/F1 per symbol & edge kind
4. exit-codes non-zero in --strict when below a configurable floor

These tests exercise the scoring math + the dependency-injected
``measure()`` call (so we don't need a built analyzer binary to
prove the harness itself works). The harness's CLI is then
ready for operators / CI to call against real binaries with real
fixtures.

Karpathy: behavioural. Every test actually executes the harness
function and checks its return value, not the source text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# scripts/ isn't on the import path by default; add it for these tests.
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts" / "accuracy"))

import measure as harness  # type: ignore  # noqa: E402


# ─── Metrics math ──────────────────────────────────────────────────


def test_metrics_perfect_score_is_p1_r1_f1():
    m = harness.Metrics(tp=5, fp=0, fn=0)
    assert m.precision == 1.0
    assert m.recall == 1.0
    assert m.f1 == 1.0


def test_metrics_all_false_positives_zero_precision():
    """Analyzer hallucinated 5 symbols that weren't there. Recall
    is undefined-but-call-it-1 (we found everything we expected
    to — zero expected), precision is 0."""
    m = harness.Metrics(tp=0, fp=5, fn=0)
    assert m.precision == 0.0


def test_metrics_all_false_negatives_zero_recall():
    """Analyzer missed every symbol. Precision is vacuously 1.0
    (everything it DID emit was correct — namely nothing), recall
    is 0."""
    m = harness.Metrics(tp=0, fp=0, fn=5)
    assert m.recall == 0.0


def test_metrics_f1_is_harmonic_mean():
    m = harness.Metrics(tp=7, fp=3, fn=2)
    p = 7 / 10
    r = 7 / 9
    expected_f1 = 2 * p * r / (p + r)
    assert m.f1 == pytest.approx(expected_f1, abs=1e-6)


# ─── Identity tuples — the key that makes fp/fn detection work ─────


def test_symbol_key_uses_kind_name_filename_only():
    """The key MUST be robust to line-number drift and absolute-
    vs-relative path differences; otherwise re-running the
    fixture with a fresh checkout gets 0% accuracy for a no-op."""
    sym_abs = {
        "kind": "function", "name": "createOrder",
        "location": {"file": "/abs/path/orders.ts", "line": 42},
    }
    sym_rel = {
        "kind": "function", "name": "createOrder",
        "location": {"file": "orders.ts", "line": 99},
    }
    assert harness._symbol_key(sym_abs) == harness._symbol_key(sym_rel)


def test_edge_key_accepts_source_or_source_id_alias():
    """Analyzer emits ``source_id`` / ``target_id``; the hand-
    curated fixture uses ``source`` / ``target`` for readability.
    The key normalises both."""
    e_from_analyzer = {"kind": "CALLS", "source_id": "A", "target_id": "B"}
    e_from_fixture = {"kind": "CALLS", "source": "A", "target": "B"}
    assert harness._edge_key(e_from_analyzer) == harness._edge_key(e_from_fixture)


# ─── _parse_jsonl — analyzer protocol ──────────────────────────────


def test_parse_jsonl_handles_blank_lines_and_garbage():
    """Real analyzer stdout has trailing newlines and the odd
    stray log line. Parser must not crash on either."""
    blob = (
        '{"type":"symbol","data":{"name":"A"}}\n'
        '\n'
        'this is not json\n'
        '{"type":"edge","data":{"kind":"CALLS"}}\n'
    )
    out = harness._parse_jsonl(blob)
    assert len(out) == 2
    assert out[0]["data"]["name"] == "A"


def test_unwrap_payload_handles_both_envelope_shapes():
    wrapped = {"type": "symbol", "data": {"name": "X", "kind": "function"}}
    bare = {"name": "X", "kind": "function"}
    assert harness._unwrap_payload(wrapped) == bare
    assert harness._unwrap_payload(bare) == bare


# ─── measure() end-to-end with injected envelopes ──────────────────


def test_measure_perfect_match_reports_full_score(tmp_path, monkeypatch):
    """Hand-build a fixture + actual that match exactly; every
    metric should be 1.0."""
    fix = tmp_path / "sample-ts-perfect"
    fix.mkdir()
    (fix / "expected.json").write_text(json.dumps({
        "symbols": [
            {"kind": "function", "name": "f", "location": {"file": "a.ts"}},
            {"kind": "class",    "name": "C", "location": {"file": "a.ts"}},
        ],
        "edges": [
            {"kind": "CALLS", "source": "f", "target": "g"},
        ],
    }))
    monkeypatch.setattr(harness, "_FIXTURES", tmp_path)
    actual = [
        {"kind": "function", "name": "f", "location": {"file": "a.ts"}},
        {"kind": "class",    "name": "C", "location": {"file": "a.ts"}},
        {"kind": "CALLS",    "source_id": "f", "target_id": "g"},
    ]
    report = harness.measure("ts", "sample-ts-perfect", actual_envelopes=actual)
    assert report["symbols"]["precision"] == 1.0
    assert report["symbols"]["recall"] == 1.0
    assert report["edges"]["precision"] == 1.0
    assert report["edges"]["recall"] == 1.0


def test_measure_detects_false_positive_symbol(tmp_path, monkeypatch):
    """Analyzer emits a symbol the fixture didn't expect. Precision
    drops, recall stays."""
    fix = tmp_path / "sample-ts-fp"
    fix.mkdir()
    (fix / "expected.json").write_text(json.dumps({
        "symbols": [{"kind": "function", "name": "real",
                     "location": {"file": "a.ts"}}],
        "edges": [],
    }))
    monkeypatch.setattr(harness, "_FIXTURES", tmp_path)
    actual = [
        {"kind": "function", "name": "real",   "location": {"file": "a.ts"}},
        {"kind": "function", "name": "ghost",  "location": {"file": "a.ts"}},
    ]
    report = harness.measure("ts", "sample-ts-fp", actual_envelopes=actual)
    assert report["symbols"]["tp"] == 1
    assert report["symbols"]["fp"] == 1
    assert report["symbols"]["fn"] == 0
    assert report["symbols"]["precision"] == 0.5
    assert report["symbols"]["recall"] == 1.0


def test_measure_detects_false_negative_symbol(tmp_path, monkeypatch):
    """Fixture expected a symbol the analyzer didn't emit. Recall
    drops; precision stays."""
    fix = tmp_path / "sample-ts-fn"
    fix.mkdir()
    (fix / "expected.json").write_text(json.dumps({
        "symbols": [
            {"kind": "function", "name": "wanted",  "location": {"file": "a.ts"}},
            {"kind": "function", "name": "missing", "location": {"file": "a.ts"}},
        ],
        "edges": [],
    }))
    monkeypatch.setattr(harness, "_FIXTURES", tmp_path)
    actual = [
        {"kind": "function", "name": "wanted", "location": {"file": "a.ts"}},
    ]
    report = harness.measure("ts", "sample-ts-fn", actual_envelopes=actual)
    assert report["symbols"]["tp"] == 1
    assert report["symbols"]["fp"] == 0
    assert report["symbols"]["fn"] == 1
    assert report["symbols"]["precision"] == 1.0
    assert report["symbols"]["recall"] == 0.5


def test_measure_rejects_unknown_analyzer(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "_FIXTURES", tmp_path)
    with pytest.raises(ValueError) as ei:
        harness.measure("nonexistent", "whatever", actual_envelopes=[])
    assert "unknown analyzer" in str(ei.value)


def test_measure_rejects_missing_expected_file(tmp_path, monkeypatch):
    fix = tmp_path / "no-expected"
    fix.mkdir()  # but no expected.json inside
    monkeypatch.setattr(harness, "_FIXTURES", tmp_path)
    with pytest.raises(FileNotFoundError):
        harness.measure("ts", "no-expected", actual_envelopes=[])


def test_meets_floors_passes_on_perfect_score():
    report = {
        "symbols": {"precision": 1.0, "recall": 1.0, "f1": 1.0},
        "floors": {"precision": 0.85, "recall": 0.80, "f1": 0.82},
    }
    assert harness.meets_floors(report, "symbols") is True


def test_meets_floors_fails_below_recall():
    report = {
        "symbols": {"precision": 0.95, "recall": 0.50, "f1": 0.65},
        "floors": {"precision": 0.85, "recall": 0.80, "f1": 0.82},
    }
    assert harness.meets_floors(report, "symbols") is False


# ─── Real fixture file: sample-ts-project ──────────────────────────


def test_real_ts_fixture_is_well_formed():
    """The hand-curated fixture under scripts/accuracy/fixtures/
    must parse and reference real source files. An accidentally-
    deleted fixture would make --all silently no-op."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "accuracy" / "fixtures" / "sample-ts-project"
    )
    assert fixture.is_dir(), "real TS fixture missing"
    expected = fixture / "expected.json"
    assert expected.is_file()
    data = json.loads(expected.read_text(encoding="utf-8"))
    assert "symbols" in data and len(data["symbols"]) > 0
    assert "edges" in data
    # Source file referenced by location.file must actually exist.
    for sym in data["symbols"]:
        loc = sym.get("location", {}).get("file")
        if loc:
            src = fixture / "src" / loc
            assert src.is_file(), f"expected.json references missing src: {loc}"


def test_real_ts_fixture_scores_correctly_when_analyzer_perfect():
    """Synthesize a 'perfect analyzer' run for the real fixture by
    echoing back its expected.json — confirms the harness can in
    fact pass on the real fixture given the right input.

    Catches a class of bugs where the harness only works on
    synthetic monkeypatched paths."""
    fixture = (
        Path(__file__).resolve().parents[2]
        / "scripts" / "accuracy" / "fixtures" / "sample-ts-project"
    )
    expected = json.loads(
        (fixture / "expected.json").read_text(encoding="utf-8")
    )
    # Build envelopes that exactly match what the fixture wants.
    actual = []
    for sym in expected["symbols"]:
        actual.append({
            "kind": sym["kind"], "name": sym["name"],
            "location": sym["location"],
        })
    for edge in expected["edges"]:
        actual.append({
            "kind": edge["kind"],
            "source_id": edge["source"],
            "target_id": edge["target"],
        })
    report = harness.measure("ts", "sample-ts-project", actual_envelopes=actual)
    assert report["symbols"]["recall"] == 1.0
    assert report["edges"]["recall"] == 1.0
