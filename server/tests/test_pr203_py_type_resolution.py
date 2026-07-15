"""PR-203 — ggoss-py type-aware call resolution (accuracy drive round 2).

The Python analog of PR-202: infer receiver types from constructor
assignments (``self.x = Repo()`` / ``x = Repo()``), type hints
(``repo: Repo``), and constructor-injected fields (``def __init__(self,
repo: Repo): self.repo = repo``), then resolve ``recv.method()`` in the
receiver's class. Disambiguates same-named methods and fixes the same-file
first-wins precision hazard.

Note: the gain is realised on repos that USE typed patterns (FastAPI/DI-style
services); a purely functional codebase (library-typed receivers like a
SQLAlchemy session) has fewer resolvable receivers — the resolution is correct
either way, staying extern for library-typed receivers.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PY = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"

_SRC = """\
class OrderRepo:
    def find(self, id): return None

class UserRepo:
    def find(self, id): return None

class Service:
    def __init__(self, orders: OrderRepo):
        self.orders = orders

    def load(self, id):
        return self.orders.find(id)       # self.attr type -> OrderRepo.find

    def load_user(self, users: UserRepo, id):
        return users.find(id)             # param type -> UserRepo.find

    def load_local(self, id):
        r = OrderRepo()
        return r.find(id)                 # local ctor type -> OrderRepo.find
"""


def _calls(target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_PY), "calls", str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x)["data"] for x in cp.stdout.splitlines() if x.strip()]


def _edge(edges, caller, callee_frag):
    for e in edges:
        if (e["source_id"].split("@")[0].endswith(caller)
                and callee_frag in e["target_id"]):
            return e
    return None


def test_self_attr_type_resolves(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "load", "OrderRepo.find")
    assert e and e["metadata"]["callee_resolved"] is True
    assert e["metadata"].get("resolution") == "receiver_type"


def test_param_type_fixes_precision(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    assert _edge(edges, "load_user", "UserRepo.find") is not None
    assert _edge(edges, "load_user", "OrderRepo.find") is None  # not wrong class


def test_local_ctor_type_resolves(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "load_local", "OrderRepo.find")
    assert e and e["metadata"].get("resolution") == "receiver_type"


def test_untyped_ambiguous_stays_extern(tmp_path: Path) -> None:
    src = """\
class A:
    def run(self): pass
class B:
    def run(self): pass
class C:
    def go(self, x):
        return x.run()
"""
    (tmp_path / "x.py").write_text(src, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "go", "run")
    # x has no known project type → ambiguous run() stays extern, not a guess.
    assert e is not None and e["metadata"]["callee_resolved"] is False
