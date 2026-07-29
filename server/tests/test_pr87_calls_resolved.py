"""PR-87 — TypeChecker-resolved callee IDs in the TS analyzer.

Analyzer review found ``cmdCalls`` constructed every CALLS edge's
``target_id`` as a synthetic ``ts:callee:<name>`` string — no real
symbol carries that id, so every CALLS edge was unjoinable. The
spec §5.2 promise (calls → callees in the knowledge graph) was
silently broken even when the analyzer ran cleanly.

cmdCalls now uses ``program.getTypeChecker()``, follows import
aliases, and emits the same ``ts:<basename>:<name>@L:C`` id
``cmdSymbols`` already produces — so intra-project calls JOIN.
Unresolved (external / built-in) calls fall through to
``ts:extern:<name>`` with ``certainty: "inferred"`` and a
``callee_resolved: false`` metadata flag, to keep them distinct.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_ANALYZER = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
)


def _node() -> str | None:
    n = shutil.which("node")
    if n is None:
        return None
    p = subprocess.run(
        [n, "-e", "require('typescript')"],
        cwd=str(_ANALYZER.parent.parent), capture_output=True,
    )
    return n if p.returncode == 0 else None


def _calls(node: str, target: Path) -> list[dict]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "calls", str(target)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(ln)["data"] for ln in proc.stdout.splitlines() if ln.strip()]


def test_intra_project_call_joins_with_symbol_id(tmp_path):
    """A call to a function in another file of the same project must
    resolve to the SAME id ``cmdSymbols`` emits — otherwise CALLS
    edges never join their target node."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "lib.ts").write_text(
        "export function helper() { return 1; }\n", encoding="utf-8",
    )
    (tmp_path / "main.ts").write_text(
        textwrap.dedent(
            """
            import { helper } from "./lib";
            export function caller() { return helper(); }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    edges = _calls(node, tmp_path)
    helper_edges = [e for e in edges if "helper" in e["target_id"]]
    assert helper_edges, "no edge to helper found"
    # Target id is rooted in lib.ts (the declaration), not main.ts (the
    # import binding), and resolution is flagged true.
    assert all(e["target_id"].startswith("ts:lib.ts:helper@") for e in helper_edges)
    assert all(e["metadata"]["callee_resolved"] is True for e in helper_edges)
    assert all(e["certainty"] == "asserted" for e in helper_edges)


def test_external_call_falls_back_to_extern(tmp_path):
    """A call to a built-in / external function (fetch, console.log)
    is tagged ts:extern:<name>, certainty inferred."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "main.ts").write_text(
        'export function f() { return fetch("/api"); }\n', encoding="utf-8",
    )
    edges = _calls(node, tmp_path)
    assert any(e["target_id"] == "ts:extern:fetch" for e in edges)
    extern = [e for e in edges if e["target_id"].startswith("ts:extern:")]
    assert all(e["metadata"]["callee_resolved"] is False for e in extern)
    assert all(e["certainty"] == "inferred" for e in extern)


def test_non_emitted_variable_is_not_claimed_as_resolved_callee(tmp_path):
    """TypeChecker declarations are broader than the symbol inventory.

    A value returned from a factory is a VariableDeclaration, but cmdSymbols
    emits variables only when their initializer is a function/class literal.
    CALLS must not claim a join to a node that was never emitted.
    """
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "main.ts").write_text(
        textwrap.dedent(
            """
            declare function makeCallable(): () => number;
            const callable = makeCallable();
            export function f() { return callable(); }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    edges = _calls(node, tmp_path)
    callable = next(e for e in edges if e["target_id"].endswith("callable"))
    assert callable["target_id"] == "ts:extern:callable"
    assert callable["metadata"]["callee_resolved"] is False
    assert callable["certainty"] == "inferred"


def test_oversized_inline_expression_is_not_an_external_symbol_id(tmp_path):
    """Minified bundles can put a whole function body in callee text.

    Such text is not a useful symbol identity and must not make analyzer
    output violate the graph's 4,096-character identifier contract.
    """
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    body = "x" * 5000
    (tmp_path / "bundle.js").write_text(
        f'export function f() {{ return (function() {{ return "{body}"; }})(); }}\n',
        encoding="utf-8",
    )
    edges = _calls(node, tmp_path)
    assert all(len(edge["target_id"]) <= 4096 for edge in edges)
    assert not any(body in edge["target_id"] for edge in edges)


# ---------------------------------------------------------------------------
# Source — TypeChecker + alias follow + Bundler resolution wired in
# ---------------------------------------------------------------------------


def test_uses_typechecker_and_follows_aliases():
    body = _ANALYZER.read_text(encoding="utf-8")
    assert "program.getTypeChecker()" in body
    assert "getAliasedSymbol" in body
    assert "ModuleResolutionKind.Bundler" in body
