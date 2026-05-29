"""PR-75 — symbol-extraction recall, found by real-world testing.

Benchmarking against 15 GitHub projects showed ``cmdSymbols`` missed
most of a modern codebase:

* it only emitted ``FunctionDeclaration`` / ``class`` / ``interface``
  — not ``const f = () => {}`` / ``const f = function () {}``, the
  dominant export shape in current JS/TS (axios: 13 symbols from 218
  files);
* ``buildProgram`` trusted a project's build ``tsconfig.json`` for
  the file set, but next.js' root tsconfig ``include``s only its test
  suite — so the analyzer measured next.js' tests, not next.js.

The fix adds function-valued ``VariableDeclaration``s to the symbol
visitor and makes ``buildProgram`` always walk the tree for the file
set (a tsconfig now contributes only compilerOptions). Measured:
axios 13 → 700 symbols, next.js 2k → 80k.
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


def _symbols(node: str, target: Path) -> dict[str, str]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "symbols", str(target)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    out = {}
    for ln in proc.stdout.splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r["record_type"] == "symbol":
                out[r["data"]["name"]] = r["data"]["kind"]
    return out


def test_arrow_and_function_expression_consts_are_symbols(tmp_path):
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "mod.ts").write_text(
        textwrap.dedent(
            """
            export const handler = () => 1;
            export const compute = function (n: number) { return n * 2; };
            export const Widget = class { render() {} };
            const PI = 3.14;
            export const config = { a: 1 };
            export function classic() { return 0; }
            """
        ),
        encoding="utf-8",
    )
    syms = _symbols(node, tmp_path)
    # Function-valued consts are now captured.
    assert syms.get("handler") == "function"
    assert syms.get("compute") == "function"
    assert syms.get("Widget") == "class"
    # The classic FunctionDeclaration still works.
    assert syms.get("classic") == "function"
    # Non-function values must NOT flood the symbol stream.
    assert "PI" not in syms
    assert "config" not in syms


def test_buildprogram_always_walks_not_just_tsconfig(tmp_path):
    """A build tsconfig that scopes to a non-representative subset
    (next.js' root tsconfig includes only tests) must not limit the
    analysis — the whole tree is walked."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    # A tsconfig that only includes a tests dir.
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"include": ["tests/**/*.ts"]}), encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "spec.ts").write_text(
        "export function testHelper() {}\n", encoding="utf-8",
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "product.ts").write_text(
        "export class ProductCode {}\n", encoding="utf-8",
    )
    syms = _symbols(node, tmp_path)
    # The product code is analysed even though the tsconfig ignores it.
    assert "ProductCode" in syms
    assert "testHelper" in syms
