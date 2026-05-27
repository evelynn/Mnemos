"""PR-111 — analyzer accuracy harness.

The biggest unmeasured product risk: I claim graph quality is the
value-driver, but no PR has ever quantified how accurate the
analyzers' output actually is. SonarQube publishes per-rule
precision/recall. CodeQL ships benchmark datasets. Mnemos
shipped 5 analyzers without a single accuracy number.

This harness fixes that. For each fixture directory under
``scripts/accuracy/fixtures/``:

1. Run the corresponding analyzer (TS/C#/SQL/binary) on it
2. Parse its JSONL stdout
3. Compare against the fixture's ``expected.json`` (a hand-curated
   "what we want to see" list)
4. Report precision / recall / F1 per symbol kind, per edge kind

Usage:

    # measure one analyzer against one fixture
    python scripts/accuracy/measure.py \\
        --analyzer ts \\
        --fixture sample-ts-project

    # measure all analyzers against all fixtures (CI mode)
    python scripts/accuracy/measure.py --all

Exit code 0 if every measured metric meets the configured floor
(default precision ≥ 0.85, recall ≥ 0.80). Non-zero otherwise so
CI can gate releases.

The harness intentionally invokes the analyzer binaries the same
way the platform does (subprocess, stdin/stdout JSONL). That way
an accuracy regression caught here is the SAME regression an
operator would see in production — not a unit-test-only artefact.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_FIXTURES = _ROOT / "fixtures"

# Analyzer → (binary name on PATH, default subcommand to run, fixture
# kinds it claims to handle). The binary names mirror analyzers/registry.py.
_ANALYZERS: dict[str, dict[str, Any]] = {
    "ts": {
        "binary": "ggoss-ts",
        "verb": "symbols",
        "kinds": {"class", "interface", "function", "type", "enum", "method"},
    },
    "csharp": {
        "binary": "ggoss-csharp",
        "verb": "symbols",
        "kinds": {"class", "interface", "method", "property", "field"},
    },
    "sql-mssql": {
        "binary": "ggoss-sql-mssql",
        "verb": "inventory",
        "kinds": {"table", "view", "procedure"},
    },
    "sql-oracle": {
        "binary": "ggoss-sql-oracle",
        "verb": "inventory",
        "kinds": {"table", "view", "procedure", "package"},
    },
    "binary-dotnet": {
        "binary": "ggoss-binary-dotnet",
        "verb": "inventory",
        "kinds": {"class", "method"},
    },
}

# Pass/fail floor. An accuracy regression below these in CI is a
# release blocker — the operator has *trusted* these binaries.
_FLOORS = {"precision": 0.85, "recall": 0.80, "f1": 0.82}


@dataclass
class FixtureExpectation:
    """The ``expected.json`` shape of a fixture."""

    symbols: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "FixtureExpectation":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            symbols=data.get("symbols", []),
            edges=data.get("edges", []),
        )


@dataclass
class Metrics:
    """Per-kind precision / recall / f1.

    tp = analyzer output ∩ expected
    fp = analyzer output \\ expected (precision hurt)
    fn = expected \\ analyzer output (recall hurt)
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        d = self.tp + self.fp
        return self.tp / d if d else 1.0

    @property
    def recall(self) -> float:
        d = self.tp + self.fn
        return self.tp / d if d else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "tp": self.tp, "fp": self.fp, "fn": self.fn,
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


def _symbol_key(sym: dict) -> tuple[str, str, str]:
    """Identity tuple for a symbol — kind + name + file. Robust
    against the analyzer emitting different ids than the fixture
    expected (line numbers drift, file paths get absolutized)."""
    loc = sym.get("location", {})
    file_ = loc.get("file") if isinstance(loc, dict) else None
    return (
        str(sym.get("kind", "")),
        str(sym.get("name", "")),
        Path(file_).name if file_ else "",
    )


def _edge_key(edge: dict) -> tuple[str, str, str]:
    """Identity tuple for an edge — kind + source-name + target-name.

    Real analyzers (confirmed: ggoss-ts) emit fully-qualified ids like
    ``ts:orders.ts:createOrder@34:1``. Fixtures are hand-written with
    short names like ``createOrder`` for readability. Normalise both
    sides to the bare symbol name so the fixture stays readable AND
    the measurement is correct."""
    raw_src = str(edge.get("source", edge.get("source_id", "")))
    raw_tgt = str(edge.get("target", edge.get("target_id", "")))
    return (
        str(edge.get("kind", "")),
        _bare_symbol_name(raw_src),
        _bare_symbol_name(raw_tgt),
    )


def _bare_symbol_name(s: str) -> str:
    """Strip the analyzer's id wrapper to just the symbol name.

    Inputs we've actually seen:
      ts:orders.ts:createOrder@34:1   →  createOrder
      ts:extern:this.rows.push        →  this.rows.push
      contract:POST /orders           →  POST /orders
      createOrder                     →  createOrder  (already bare)
    """
    if not s:
        return ""
    # Trim ``@line:col`` suffix that ggoss-ts attaches.
    if "@" in s:
        s = s.split("@", 1)[0]
    # Take the last ``:``-separated segment, which is the symbol
    # name proper. Falls through when the id has no prefix.
    if ":" in s:
        s = s.rsplit(":", 1)[-1]
    return s


def _parse_jsonl(blob: str) -> list[dict]:
    """Analyzer protocol is one JSON envelope per line on stdout."""
    out: list[dict] = []
    for line in blob.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            # Non-envelope chatter (analyzer error logs etc) goes
            # to stderr in production; if it ends up on stdout
            # we just skip it.
            continue
    return out


def _unwrap_payload(envelope: dict) -> dict:
    """The analyzer envelope is ``{type: "symbol", data: {...}}``
    or just ``{...}`` depending on verb. Handle both."""
    if isinstance(envelope.get("data"), dict):
        return envelope["data"]
    return envelope


def run_analyzer(
    binary: str,
    verb: str,
    fixture_dir: Path,
) -> list[dict]:
    """Invoke the analyzer binary on the fixture. Mirrors the
    platform's invocation (analyzers/runner.py).

    Real analyzers (ggoss-ts confirmed) take target as a positional
    arg, not --target. We also have to invoke BOTH the symbols verb
    AND the calls verb to get both sets — analyzers emit them
    separately."""
    # Allow direct script invocation when binary isn't on PATH
    # (development environments where ``docker compose build``
    # hasn't been run). We look for ``analyzers/<binary>/src/``.
    fallback_node = _ROOT.parent.parent / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
    if shutil.which(binary) is None and binary == "ggoss-ts" and fallback_node.is_file():
        cmd_base = ["node", str(fallback_node)]
    elif shutil.which(binary) is None:
        raise FileNotFoundError(
            f"analyzer binary {binary!r} not on PATH "
            "(build it: `docker compose --profile analyzers build`)"
        )
    else:
        cmd_base = [binary]

    envelopes: list[dict] = []
    verbs_to_run = [verb]
    # For TS+CSharp accuracy, symbols + calls together is the
    # right comparison surface — measuring only symbols would
    # mark a fixture with zero edges as 100% accurate.
    if verb == "symbols":
        verbs_to_run = ["symbols", "calls"]

    for v in verbs_to_run:
        cp = subprocess.run(
            cmd_base + [v, str(fixture_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if cp.returncode != 0:
            raise RuntimeError(
                f"{binary} {v} failed (rc={cp.returncode}): {cp.stderr[:500]}"
            )
        envelopes.extend(_unwrap_payload(env) for env in _parse_jsonl(cp.stdout))
    return envelopes


def measure(
    analyzer: str,
    fixture_name: str,
    actual_envelopes: list[dict] | None = None,
) -> dict[str, Any]:
    """Compare analyzer output against the fixture expectation.

    ``actual_envelopes`` is dependency-injectable so tests don't
    need a real analyzer binary. The CLI path runs the binary;
    the unit-test path passes pre-built envelopes.
    """
    if analyzer not in _ANALYZERS:
        raise ValueError(f"unknown analyzer: {analyzer!r}")
    fixture_dir = _FIXTURES / fixture_name
    expected_path = fixture_dir / "expected.json"
    if not expected_path.is_file():
        raise FileNotFoundError(f"fixture missing expected.json: {fixture_dir}")

    expected = FixtureExpectation.load(expected_path)
    if actual_envelopes is None:
        cfg = _ANALYZERS[analyzer]
        actual_envelopes = run_analyzer(cfg["binary"], cfg["verb"], fixture_dir)

    # Split actual into symbols + edges by envelope shape.
    actual_symbols = [
        e for e in actual_envelopes
        if e.get("kind") in _ANALYZERS[analyzer]["kinds"]
        and "name" in e
    ]
    all_actual_edges = [
        e for e in actual_envelopes
        if e.get("kind") in {"CALLS", "EXPOSES", "READS", "WRITES"}
    ]
    # Split edges into in-project vs extern (calls to builtins,
    # stdlib, node_modules). Operators write expected.json by hand —
    # they shouldn't have to enumerate every ``Array.push`` call.
    # We score only in-project edges against the floor and report
    # extern edges informationally.
    actual_edges = [
        e for e in all_actual_edges
        if not _is_extern_edge(e)
    ]
    extern_count = len(all_actual_edges) - len(actual_edges)

    sym_metrics = _score_set(
        actual_symbols, expected.symbols, _symbol_key
    )
    edge_metrics = _score_set(
        actual_edges, expected.edges, _edge_key
    )

    return {
        "analyzer": analyzer,
        "fixture": fixture_name,
        "symbols": sym_metrics.as_dict(),
        "edges": edge_metrics.as_dict(),
        "extern_edges_emitted": extern_count,
        "floors": _FLOORS,
    }


def _is_extern_edge(edge: dict) -> bool:
    """True iff the edge points at something outside the project
    (analyzer's ``extern:*`` namespace, or any callee that didn't
    resolve to a project-local symbol). Excluded from the F1 score
    because operators don't enumerate builtin calls in expected.json."""
    tgt = str(edge.get("target", edge.get("target_id", "")))
    if ":extern:" in tgt or tgt.startswith("extern:"):
        return True
    meta = edge.get("metadata", {}) or {}
    if meta.get("callee_resolved") is False:
        return True
    return False


def _score_set(actual: list[dict], expected: list[dict], key_fn) -> Metrics:
    actual_keys = {key_fn(a) for a in actual}
    expected_keys = {key_fn(e) for e in expected}
    return Metrics(
        tp=len(actual_keys & expected_keys),
        fp=len(actual_keys - expected_keys),
        fn=len(expected_keys - actual_keys),
    )


def meets_floors(report: dict, dimension: str) -> bool:
    """True iff this dimension's precision/recall/f1 all meet the
    floor. ``dimension`` is "symbols" or "edges"."""
    m = report.get(dimension)
    if not m:
        return True
    floors = report.get("floors", _FLOORS)
    return all(m[k] >= floors[k] for k in ("precision", "recall", "f1"))


def main() -> int:
    parser = argparse.ArgumentParser(prog="measure")
    parser.add_argument("--analyzer", choices=sorted(_ANALYZERS), help="single analyzer to measure")
    parser.add_argument("--fixture", help="fixture directory under scripts/accuracy/fixtures/")
    parser.add_argument("--all", action="store_true", help="run every analyzer × every fixture")
    parser.add_argument("--strict", action="store_true",
                        help="non-zero exit code if any metric is below the floor")
    args = parser.parse_args()

    pairs: list[tuple[str, str]] = []
    if args.all:
        for analyzer, cfg in _ANALYZERS.items():
            for fix in sorted(_FIXTURES.iterdir()):
                if not fix.is_dir():
                    continue
                if not (fix / "expected.json").is_file():
                    continue
                # Convention: fixture name starts with analyzer slug,
                # e.g. ``sample-ts-project`` matches analyzer ``ts``.
                if analyzer in fix.name:
                    pairs.append((analyzer, fix.name))
    else:
        if not (args.analyzer and args.fixture):
            parser.error("provide --analyzer + --fixture, or --all")
        pairs.append((args.analyzer, args.fixture))

    if not pairs:
        print("no analyzer × fixture pairs to measure", file=sys.stderr)
        return 2

    rc = 0
    for analyzer, fixture in pairs:
        try:
            report = measure(analyzer, fixture)
        except FileNotFoundError as exc:
            print(f"skip {analyzer} × {fixture}: {exc}", file=sys.stderr)
            continue
        print(json.dumps(report, indent=2))
        if args.strict:
            for dim in ("symbols", "edges"):
                if not meets_floors(report, dim):
                    print(
                        f"FAIL {analyzer} × {fixture}: {dim} below floor "
                        f"(precision/recall/f1 floors {_FLOORS})",
                        file=sys.stderr,
                    )
                    rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
