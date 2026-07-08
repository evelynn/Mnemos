"""PR-195 — ggoss-treesitter: config-driven multi-language analyzer.

Absorption-review T2: absorb cbm's language-extensibility *mechanism*
(tree-sitter) natively — ONE analyzer that extracts symbols + CALLS for any
configured language. These tests pin the Go PoC and prove extensibility by
also exercising Rust and Ruby through the SAME analyzer, plus graceful
degradation and the availability gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

# The whole analyzer is the opt-in breadth path — skip cleanly when the
# tree-sitter package isn't installed (base install stays dependency-free).
pytest.importorskip("tree_sitter_language_pack")

_ROOT = Path(__file__).resolve().parents[2]
_TS = _ROOT / "analyzers" / "ggoss-treesitter" / "src" / "ggoss_treesitter.py"

_GO = """\
package orders

type Order struct { ID int }

func validateOrder(o *Order) bool { return o.ID > 0 }

func CreateOrder(id int) bool {
	o := &Order{ID: id}
	return validateOrder(o)
}
"""

_RUST = """\
struct Order { id: i32 }
fn validate(o: &Order) -> bool { o.id > 0 }
fn create(id: i32) -> bool {
    let o = Order { id };
    validate(&o)
}
"""

_RUBY = """\
class Repo
  def save(o)
    persist(o)
  end
end
"""


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_TS), verb, str(target)],
        capture_output=True, text=True, timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def _write(root: Path, name: str, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(body, encoding="utf-8")
    return root


def test_go_symbols_and_calls(tmp_path: Path) -> None:
    d = _write(tmp_path / "go", "orders.go", _GO)
    syms = {s["data"]["name"]: s["data"] for s in _run("symbols", d)}
    assert syms["Order"]["kind"] == "type"
    assert syms["validateOrder"]["kind"] == "function"
    assert syms["CreateOrder"]["kind"] == "function"
    assert syms["validateOrder"]["metadata"]["extractor"] == "tree_sitter"

    edges = [e["data"] for e in _run("calls", d)]
    resolved = [
        e for e in edges
        if e["source_id"].split("@")[0].endswith("CreateOrder")
        and e["target_id"].split("@")[0].endswith("validateOrder")
    ]
    assert resolved and resolved[0]["metadata"]["callee_resolved"] is True
    assert resolved[0]["certainty"] == "asserted"


def test_extensibility_rust_and_ruby_same_analyzer(tmp_path: Path) -> None:
    # The point of T2: new languages are config entries, not new analyzers.
    rust = _write(tmp_path / "rs", "orders.rs", _RUST)
    rsyms = {s["data"]["name"] for s in _run("symbols", rust)}
    assert {"Order", "validate", "create"} <= rsyms
    redges = [e["data"] for e in _run("calls", rust)]
    assert any(
        e["source_id"].split("@")[0].endswith("create")
        and e["target_id"].split("@")[0].endswith("validate")
        and e["metadata"]["callee_resolved"] for e in redges
    )

    ruby = _write(tmp_path / "rb", "repo.rb", _RUBY)
    rbsyms = {(s["data"]["kind"], s["data"]["name"]) for s in _run("symbols", ruby)}
    assert ("class", "Repo") in rbsyms
    assert any(k == "method" and n == "save" for k, n in rbsyms)


def test_schema_lists_configured_languages() -> None:
    cp = subprocess.run(
        [sys.executable, str(_TS), "schema"],
        capture_output=True, text=True, timeout=60,
    )
    schema = json.loads(cp.stdout)
    assert schema["backend"] == "tree-sitter-language-pack"
    assert {"go", "rust", "ruby"} <= set(schema["languages"])


def test_registry_maps_and_gates_on_package() -> None:
    from app.analyzers.registry import _treesitter_importable, binary_for

    assert binary_for("go") == "ggoss-treesitter"
    assert binary_for("rust") == "ggoss-treesitter"
    assert binary_for("ruby") == "ggoss-treesitter"
    # Package is importable in this test env (importorskip passed), so the
    # gate says the language is deterministically extractable.
    assert _treesitter_importable() is True
