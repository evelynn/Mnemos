"""Stable analyzer identity across disposable source checkouts.

Analysis runs use temporary worktree paths.  Those physical paths must never
leak into graph ids or source locations, otherwise the same commit creates a
new graph on every refresh.  TypeScript also needs the full relative path so
same-named files in different packages do not collapse into one symbol.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"
_TS_ANALYZER = _ROOT / "analyzers" / "ggoss-ts" / "src" / "index.mjs"
_TS_DEPENDENCY = _ROOT / "analyzers" / "ggoss-ts" / "node_modules" / "typescript"
_NODE = shutil.which("node")


def _run(command: list[str], *, project_id: str) -> list[dict]:
    env = {**os.environ, "MNEMOS_PROJECT_ID": project_id}
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-1000:]
    return [
        json.loads(line)
        for line in completed.stdout.splitlines()
        if line.strip()
    ]


def _records_by_id(envelopes: list[dict]) -> dict[str, dict]:
    return {
        envelope["data"]["id"]: envelope["data"]
        for envelope in envelopes
        if envelope.get("record_type") == "symbol"
    }


def test_python_symbol_identity_is_independent_of_checkout_root(tmp_path):
    roots = [tmp_path / "checkout-one", tmp_path / "unrelated-name"]
    for root in roots:
        module = root / "src" / "service.py"
        module.parent.mkdir(parents=True)
        module.write_text(
            "class Service:\n"
            "    def answer(self):\n"
            "        return 42\n",
            encoding="utf-8",
        )

    first = _records_by_id(
        _run(
            [sys.executable, str(_PY_ANALYZER), "symbols", str(roots[0])],
            project_id="stable-project",
        )
    )
    second = _records_by_id(
        _run(
            [sys.executable, str(_PY_ANALYZER), "symbols", str(roots[1])],
            project_id="stable-project",
        )
    )

    assert first == second
    assert set(first) == {
        "py:src/service.py:Service@1",
        "py:src/service.py:Service.answer@2",
    }
    assert {item["component_id"] for item in first.values()} == {
        "py.stable-project"
    }
    assert {item["location"]["file"] for item in first.values()} == {
        "src/service.py"
    }


@pytest.mark.skipif(
    _NODE is None or not _TS_ANALYZER.is_file() or not _TS_DEPENDENCY.exists(),
    reason="node or the in-repo TypeScript dependency is unavailable",
)
def test_typescript_identity_is_root_stable_and_same_basenames_do_not_collide(
    tmp_path,
):
    roots = [tmp_path / "worktree-a", tmp_path / "worktree-b"]
    for root in roots:
        for package in ("alpha", "beta"):
            source = root / "packages" / package / "index.ts"
            source.parent.mkdir(parents=True)
            source.write_text(
                f"export function {package}Handler() {{ return '{package}'; }}\n",
                encoding="utf-8",
            )

    first = _records_by_id(
        _run(
            [_NODE, str(_TS_ANALYZER), "symbols", str(roots[0])],
            project_id="stable-project",
        )
    )
    second = _records_by_id(
        _run(
            [_NODE, str(_TS_ANALYZER), "symbols", str(roots[1])],
            project_id="stable-project",
        )
    )

    assert first == second
    assert len(first) == 2
    files = {item["location"]["file"] for item in first.values()}
    assert files == {
        "packages/alpha/index.ts",
        "packages/beta/index.ts",
    }
    assert len(set(first)) == 2
    assert all(symbol_id.startswith("ts:packages/") for symbol_id in first)
    assert {item["component_id"] for item in first.values()} == {
        "svc.stable-project"
    }
