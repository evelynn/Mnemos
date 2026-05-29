"""PR-74 — buildProgram robustness, found by 10-project validation.

Running the TypeScript analyzer against ten more large GitHub repos
exposed two buildProgram defects:

* **silent empty result** — withastro/astro (2832 source files)
  produced 0 records, exit 0, no diagnostic. Its root tsconfig.json
  is solution-style (project ``references``, an almost-empty
  ``files``); ``parseJsonConfigFileContent`` does not follow
  references, so the program resolved to ~0 files.
* **hard crash** — microsoft/TypeScript (39k files incl. 18,876
  intentionally-malformed compiler-test fixtures) crashed
  ``createProgram`` with ``Debug Failure``, exit 1, zero output.

buildProgram now walks the tree when the tsconfig is solution-style
(or absent), and on a createProgram crash retries once without test
/ fixture directories — so a normal codebase still yields a result.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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


def _symbols(node: str, target: Path) -> set[str]:
    proc = subprocess.run(
        [node, str(_ANALYZER), "symbols", str(target)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    names = set()
    for ln in proc.stdout.splitlines():
        if ln.strip():
            r = json.loads(ln)
            if r["record_type"] == "symbol":
                names.add(r["data"]["name"])
    return names


def test_solution_style_tsconfig_falls_back_to_walk(tmp_path):
    """A references-only tsconfig must not make the analyzer silently
    see an empty project — it should walk the tree instead."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"files": [], "references": [{"path": "./packages/a"}]}),
        encoding="utf-8",
    )
    (tmp_path / "app.ts").write_text(
        "export class Widget {}\nexport function build() { return 1; }\n",
        encoding="utf-8",
    )
    names = _symbols(node, tmp_path)
    assert "Widget" in names
    assert "build" in names


def test_generated_dirs_skipped(tmp_path):
    """dist / build / coverage are generated output — not analysed."""
    node = _node()
    if node is None:
        pytest.skip("node + typescript unavailable")
    (tmp_path / "src.ts").write_text("export class RealCode {}\n", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "bundle.ts").write_text("export class GeneratedJunk {}\n", encoding="utf-8")
    names = _symbols(node, tmp_path)
    assert "RealCode" in names
    assert "GeneratedJunk" not in names


# ---------------------------------------------------------------------------
# Source-level — the crash-retry path
# ---------------------------------------------------------------------------


def test_buildprogram_has_crash_retry():
    body = _ANALYZER.read_text(encoding="utf-8")
    idx = body.find("function buildProgram(")
    slab = body[idx:idx + 2200]
    # createProgram is wrapped; on failure it retries without test dirs.
    assert "try {" in slab
    assert "catch (err)" in slab
    assert "skipTests: true" in slab


def test_solution_tsconfig_detection_present():
    body = _ANALYZER.read_text(encoding="utf-8")
    idx = body.find("function buildProgram(")
    slab = body[idx:idx + 2200]
    assert "references" in slab
    assert "walkFiles(target, _SOURCE_EXTS)" in slab
