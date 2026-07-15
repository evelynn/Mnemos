"""PR-202 — ggoss-java type-aware call resolution (accuracy drive round 1).

The measured accuracy gap vs cbm's Hybrid LSP was call resolution: name-based
resolution (a) misses ambiguous calls (same method name in 2+ classes) and
(b) actively mis-resolves them (same-file first-wins → WRONG class). This adds
receiver-TYPE resolution: track field / parameter / local variable types, then
resolve ``recv.method()`` in the receiver's class. Plus constructor calls
``new T()`` → the type's constructor.

Measured effect (Spring PetClinic): in-project call recall 78.9% → 94.1%, and
the precision bug (a call resolving to the wrong same-named method) is fixed —
pinned below on a ground-truth fixture.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JAVA = _ROOT / "analyzers" / "ggoss-java" / "src" / "ggoss_java.py"

# Two classes with the SAME method name ``find`` — only the receiver's type
# disambiguates. Ground truth is in the comments.
_SRC = """\
package com.demo;

class OrderRepo {
    Order find(int id) { return null; }
}
class UserRepo {
    User find(int id) { return null; }
}
class Service {
    private final OrderRepo orders;
    Service(OrderRepo orders) { this.orders = orders; }

    Order load(int id) {
        return orders.find(id);          // field type → OrderRepo.find
    }
    User loadUser(UserRepo users, int id) {
        return users.find(id);           // param type → UserRepo.find
    }
    Order loadLocal(int id) {
        OrderRepo r = orders;
        return r.find(id);               // local type → OrderRepo.find
    }
    Order make(int id) {
        return new Order();              // constructor call
    }
}
class Order {}
class User {}
"""


def _calls(target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_JAVA), "calls", str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x)["data"] for x in cp.stdout.splitlines() if x.strip()]


def _edge(edges: list[dict], caller: str, callee_frag: str) -> dict | None:
    for e in edges:
        if (e["source_id"].split("@")[0].endswith(caller)
                and callee_frag in e["target_id"]):
            return e
    return None


def test_field_type_resolves_ambiguous_call(tmp_path: Path) -> None:
    (tmp_path / "App.java").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "load", "OrderRepo.find")
    assert e and e["metadata"]["callee_resolved"] is True
    assert e["metadata"].get("resolution") == "receiver_type"


def test_param_type_fixes_precision_bug(tmp_path: Path) -> None:
    """The load-bearing precision fix: ``users.find()`` must resolve to
    UserRepo.find, NOT OrderRepo.find (which the old first-wins path picked)."""
    (tmp_path / "App.java").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    assert _edge(edges, "loadUser", "UserRepo.find") is not None
    # And must NOT have the wrong edge.
    assert _edge(edges, "loadUser", "OrderRepo.find") is None


def test_local_type_resolves(tmp_path: Path) -> None:
    (tmp_path / "App.java").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "loadLocal", "OrderRepo.find")
    assert e and e["metadata"].get("resolution") == "receiver_type"


def test_constructor_call_resolves(tmp_path: Path) -> None:
    (tmp_path / "App.java").write_text(_SRC, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "make", "Order")
    assert e and e["metadata"]["callee_resolved"] is True
    assert e["metadata"].get("resolution") == "constructor"


def test_ambiguous_without_type_stays_extern_not_wrong(tmp_path: Path) -> None:
    """When the receiver's type is unknown, an ambiguous name must stay extern
    (honest) rather than resolve to an arbitrary same-named method."""
    src = """\
package com.demo;
class A { void run() {} }
class B { void run() {} }
class C {
    void go(Object x) { x.run(); }
}
"""
    (tmp_path / "X.java").write_text(src, encoding="utf-8")
    edges = _calls(tmp_path)
    e = _edge(edges, "go", "run")
    # ``x`` has no known project type → run() is ambiguous → extern, not a guess.
    assert e is not None
    assert e["target_id"] == "java:extern:run"
    assert e["metadata"]["callee_resolved"] is False
