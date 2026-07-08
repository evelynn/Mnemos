"""PR-198 — ggoss-py cross-module call resolution (Phase-2 gap).

Pre-fix, ggoss-py resolved calls only within a single file — a call to a
function/class defined in another module degraded to ``py:extern``. This
adds project-wide unique-name resolution (precision-safe: ambiguous names
stay extern). Import-aware disambiguation remains future work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PY = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"


def _calls(target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY), "calls", str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x)["data"] for x in cp.stdout.splitlines() if x.strip()]


def test_cross_module_call_resolves_to_unique_symbol(tmp_path: Path) -> None:
    (tmp_path / "repo.py").write_text(
        "class OrdersRepo:\n"
        "    def persist_order(self, o):\n"
        "        return o\n",
        encoding="utf-8",
    )
    (tmp_path / "service.py").write_text(
        "from repo import OrdersRepo\n\n"
        "def create_order(payload):\n"
        "    repo = OrdersRepo()\n"
        "    return persist_order(repo)\n",
        encoding="utf-8",
    )
    edges = _calls(tmp_path)
    # create_order -> persist_order, defined in the OTHER module, now resolved.
    hit = [
        e for e in edges
        if e["source_id"].split("@")[0].endswith("create_order")
        and "persist_order" in e["target_id"]
        and not e["target_id"].startswith("py:extern:")
    ]
    assert hit, [e["target_id"] for e in edges]
    assert hit[0]["metadata"]["callee_resolved"] is True
    assert hit[0]["metadata"].get("resolution") == "cross_module"
    assert hit[0]["certainty"] == "asserted"


def test_ambiguous_cross_module_name_stays_extern(tmp_path: Path) -> None:
    # Two modules define ``helper`` — a bare call must NOT guess (precision).
    (tmp_path / "a.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def helper():\n    return 2\n", encoding="utf-8")
    (tmp_path / "c.py").write_text(
        "def run():\n    return helper()\n", encoding="utf-8"
    )
    edges = _calls(tmp_path)
    run_calls = [e for e in edges if e["source_id"].split("@")[0].endswith("run")]
    assert run_calls
    # helper is ambiguous (2 project-wide) → stays extern, not a wrong guess.
    assert all(e["target_id"] == "py:extern:helper" for e in run_calls)
    assert all(e["metadata"]["callee_resolved"] is False for e in run_calls)


def test_same_file_resolution_still_works(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "def validate(x):\n    return x > 0\n\n"
        "def create(x):\n    return validate(x)\n",
        encoding="utf-8",
    )
    edges = _calls(tmp_path)
    hit = [
        e for e in edges
        if e["source_id"].split("@")[0].endswith("create")
        and "validate" in e["target_id"]
    ]
    assert hit and hit[0]["metadata"]["callee_resolved"] is True
    # Same-file resolution is NOT tagged cross_module.
    assert hit[0]["metadata"].get("resolution") != "cross_module"
