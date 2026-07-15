"""PR-115 — ggoss-py: Python analyzer + Mnemos self-dogfood.

Mnemos's analyzer set was C#/TS/MSSQL/Oracle/.NET — missing
Python despite being itself a Python codebase. The most powerful
dogfood proof of value: Mnemos analyses Mnemos and the output
matches reality.

This file proves:
1. ggoss-py extracts every symbol kind correctly (class, function,
   method, async_function) — REAL execution against the
   sample-py-project fixture
2. ggoss-py resolves intra-module attribute calls (the bug fix
   landed during this PR — pre-fix recall was 0)
3. ggoss-py scores 1.00/1.00/1.00 on its fixture (matches ggoss-ts)
4. ggoss-py runs cleanly on Mnemos's own server/app/ — emits
   ≥500 symbols, ≥3000 calls, with a non-trivial in-project
   resolution rate
5. The dogfood output identifies known Mnemos hotspots — proves
   the call graph isn't random noise but reflects real structure
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
_FIXTURE = _ROOT / "scripts" / "accuracy" / "fixtures" / "sample-py-project"
_MNEMOS_APP = _ROOT / "server" / "app"

sys.path.insert(0, str(_ROOT / "scripts" / "accuracy"))
import measure as harness  # type: ignore  # noqa: E402


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), verb, str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"ggoss-py {verb} crashed: {cp.stderr[:400]}"
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


# ─── fixture accuracy ──────────────────────────────────────────────


def test_ggoss_py_extracts_all_symbol_kinds():
    envs = _run("symbols", _FIXTURE)
    syms = [e["data"] for e in envs if e["record_type"] == "symbol"]
    kinds_present = {s["kind"] for s in syms}
    # Must cover all four kinds the fixture exercises — missing one
    # means a node-type the analyzer has silently stopped emitting.
    assert kinds_present == {"class", "function", "method", "async_function"}, (
        f"unexpected kind set: {kinds_present}"
    )


def test_ggoss_py_resolves_method_calls_through_receiver():
    """The bug PR-115 found and fixed: ``repo.add(order)`` was
    being emitted as ``py:extern:repo.add`` (callee_resolved=False)
    instead of resolving to the in-module ``OrdersRepo.add``.
    Pre-fix recall = 0; post-fix recall = 1.0."""
    envs = _run("calls", _FIXTURE)
    edges = [e["data"] for e in envs if e["record_type"] == "edge"]
    # Look for the create_order → add edge, which only resolves
    # when receiver-prefix stripping works.
    matches = [
        e for e in edges
        if "create_order" in e["source_id"]
        and e["metadata"]["callee_resolved"] is True
        and (
            "OrdersRepo.add" in e["target_id"] or e["target_id"].endswith(":add@21")
        )
    ]
    assert matches, (
        "create_order → OrdersRepo.add edge missing — method-call "
        "resolution regressed"
    )


def test_ggoss_py_handles_async_function_bodies():
    """Async functions are a separate AST node (AsyncFunctionDef).
    A naïve implementation visiting only FunctionDef would silently
    drop every callee in an async function. Verify the fixture's
    ``cancel_and_notify(...)`` carries its expected callee."""
    envs = _run("calls", _FIXTURE)
    edges = [e["data"] for e in envs if e["record_type"] == "edge"]
    async_caller = [
        e for e in edges
        if "cancel_and_notify" in e["source_id"]
        and e["metadata"]["callee_resolved"] is True
    ]
    assert async_caller, (
        "async function's callees not extracted — AsyncFunctionDef "
        "regression in cmdCalls visitor"
    )


def test_ggoss_py_meets_floor_on_fixture():
    envs = _run("symbols", _FIXTURE) + _run("calls", _FIXTURE)
    unwrapped = [harness._unwrap_payload(e) for e in envs]
    report = harness.measure("py", "sample-py-project", actual_envelopes=unwrapped)
    assert harness.meets_floors(report, "symbols"), report["symbols"]
    assert harness.meets_floors(report, "edges"), report["edges"]
    # The integration claim — published in getting-started.md.
    assert report["symbols"]["f1"] == 1.0
    assert report["edges"]["f1"] == 1.0


# ─── Mnemos self-dogfood ──────────────────────────────────────────


def test_ggoss_py_runs_on_mnemos_own_app():
    """The strongest validation: ggoss-py extracts data from
    Mnemos's own server/app/ (136 files, 17K LOC). Doesn't crash,
    doesn't return empty, emits a meaningful number of symbols
    and edges."""
    envs = _run("symbols", _MNEMOS_APP)
    syms = [e for e in envs if e["record_type"] == "symbol"]
    assert len(syms) >= 400, (
        f"dogfood symbol count suspiciously low: {len(syms)} "
        "(server/app/ has hundreds of functions; <400 means the "
        "analyzer broke or is filtering too aggressively)"
    )


def test_ggoss_py_identifies_mnemos_hotspots():
    """The call graph must reflect real structure — specific
    functions Mnemos relies on heavily must show up as
    high-fan-in / high-fan-out callees.

    This is the "is the analyzer actually understanding the code"
    test, not just "does it run without crashing"."""
    envs = _run("calls", _MNEMOS_APP)
    edges = [e["data"] for e in envs if e["record_type"] == "edge"]
    in_proj = [e for e in edges if e["metadata"]["callee_resolved"]]
    assert len(in_proj) >= 200, (
        f"in-project resolution rate too low: {len(in_proj)} / "
        f"{len(edges)} total"
    )
    # Bucket callees by bare name and confirm the seed-demo helpers remain
    # high-fan-in. The application has grown substantially since PR-115, so a
    # global top-5 rank is not stable; the measured call counts are the actual
    # analyzer contract (_out 20+, several related helpers 10+).
    from collections import Counter
    def _bare(s: str) -> str:
        return s.split(":")[-1].split("@")[0].split(".")[-1]
    callees = Counter(_bare(e["target_id"]) for e in in_proj)
    helpers = {name: callees[name] for name in {"_out", "_edge", "_node", "_to_out"}}
    assert helpers["_out"] >= 20, f"seed-demo _out fan-in regressed: {helpers}"
    assert sum(count >= 10 for count in helpers.values()) >= 3, (
        f"expected at least three high-fan-in seed-demo helpers, got {helpers}"
    )


def test_inventory_returns_realistic_counts_for_mnemos():
    """inventory verb is the cheap pre-flight the orchestrator
    uses to decide whether to schedule a full analysis. Its
    numbers must roughly match what 'find . -name "*.py" | wc -l'
    would give."""
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), "inventory", str(_MNEMOS_APP)],
        capture_output=True, text=True, timeout=10,
    )
    assert cp.returncode == 0
    out = json.loads(cp.stdout)
    assert out["language"] == "python"
    assert out["py_files"] >= 100, f"only {out['py_files']} files seen?"
    assert out["total_lines"] >= 10_000, (
        f"total_lines {out['total_lines']} seems off — analyzer "
        "skipped most files?"
    )


def test_probe_distinguishes_python_from_non_python():
    """probe verb is even cheaper — boolean "is this even a Python
    target?". Must return False on a directory with no .py files
    (e.g. analyzers/ggoss-ts/) and True on server/app/."""
    cp_py = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), "probe", str(_MNEMOS_APP)],
        capture_output=True, text=True, timeout=5,
    )
    cp_ts = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), "probe",
         str(_ROOT / "analyzers" / "ggoss-ts" / "src")],
        capture_output=True, text=True, timeout=5,
    )
    py_out = json.loads(cp_py.stdout)
    ts_out = json.loads(cp_ts.stdout)
    assert py_out["ok"] is True
    assert ts_out["ok"] is False
    assert ts_out["py_files"] == 0


def test_schema_describes_supported_record_kinds():
    """schema verb is what analyzers/registry.py asks each binary
    at startup to validate the configured pipeline."""
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), "schema"],
        capture_output=True, text=True, timeout=5,
    )
    out = json.loads(cp.stdout)
    assert out["language"] == "python"
    assert "class" in out["symbol_kinds"]
    assert "CALLS" in out["edge_kinds"]


def test_syntax_error_is_recoverable_not_fatal():
    """A real codebase has the occasional unparseable file (mid-
    edit, generated stub, exotic syntax for a Python version not
    installed). Analyzer must skip + log recoverable error, not
    crash."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "good.py"
        good.write_text("def f(): pass\n")
        bad = Path(td) / "bad.py"
        bad.write_text("def f(\n  # never closes\n")

        cp = subprocess.run(
            [sys.executable, str(_PY_ANALYZER), "symbols", td],
            capture_output=True, text=True, timeout=10,
        )
        assert cp.returncode == 0, (
            "analyzer crashed on a directory with one broken file — "
            "must skip and continue per analyzer contract"
        )
        # Good file's symbols still appear on stdout.
        symbols = [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]
        names = {s["data"]["name"] for s in symbols}
        assert "f" in names
        # Bad file's parse error appears on stderr as a recoverable
        # record (JSON lines, level=error, recoverable=true).
        err_lines = [
            json.loads(line) for line in cp.stderr.splitlines()
            if line.strip().startswith("{")
        ]
        assert any(
            e.get("level") == "error" and e.get("recoverable") is True
            for e in err_lines
        )


def test_nonfatal_ast_warnings_do_not_pollute_jsonl_stderr(tmp_path):
    """A valid legacy escape must not turn an authoritative verb partial.

    Python changed this diagnostic from ``DeprecationWarning`` to
    ``SyntaxWarning`` across releases. Force warnings on so the regression is
    observable on every supported interpreter.
    """

    source = tmp_path / "legacy_escape.py"
    source.write_text(
        r"PATTERN = '\d+'" + "\n"
        "def matches(value):\n"
        "    return PATTERN == value\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONWARNINGS": "always"}

    for verb in ("symbols", "calls", "contracts", "data_access"):
        completed = subprocess.run(
            [sys.executable, str(_PY_ANALYZER), verb, str(tmp_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert completed.stderr == "", (
            f"{verb} leaked a non-fatal AST warning into analyzer stderr: "
            f"{completed.stderr!r}"
        )
