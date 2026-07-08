"""PR-194 — ggoss-kotlin: deterministic Kotlin analyzer (web+Java P3).

Modern Spring Boot is often Kotlin. These tests pin: type/function symbols
with correct nesting + top-level functions, ``fun``-based detection,
Spring annotations (identical to Java) → contracts normalized to the same
``http.<METHOD>.<path>`` node (cross-service), and name-based CALLS.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_KT = _ROOT / "analyzers" / "ggoss-kotlin" / "src" / "ggoss_kotlin.py"


_SRC = """\
package com.demo.owner

import org.springframework.web.bind.annotation.*

@RestController
@RequestMapping("/api/owners")
class OwnerController(private val service: OwnerService) {

    @GetMapping("/{id}")
    fun findOne(@PathVariable(name = "id") id: Int): Owner {
        return service.load(id)
    }

    @PostMapping
    fun create(@RequestBody owner: Owner): Owner {
        validate(owner)
        return service.save(owner)
    }

    private fun validate(owner: Owner) = requireNotNull(owner)
}

@org.springframework.stereotype.Service
class OwnerService {
    fun load(id: Int): Owner? = null
    fun save(o: Owner): Owner = o
}

fun main() {
    println("top-level")
}
"""


def _write(root: Path) -> None:
    d = root / "src"
    d.mkdir(parents=True)
    (d / "OwnerController.kt").write_text(_SRC, encoding="utf-8")


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_KT), verb, str(target)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def test_symbols_types_functions_and_toplevel(tmp_path: Path) -> None:
    _write(tmp_path)
    data = [r["data"] for r in _run("symbols", tmp_path)]
    types = {d["name"]: d for d in data if d["kind"] != "function"}
    fns = {d["qual_name"]: d for d in data if d["kind"] == "function"}

    assert types["OwnerController"]["kind"] == "class"
    assert types["OwnerService"]["kind"] == "class"
    # Methods nest under their class (FQN carries package + type).
    assert "com.demo.owner.OwnerController.findOne" in fns
    assert "com.demo.owner.OwnerService.load" in fns
    # Top-level function has no enclosing type.
    assert "com.demo.owner.main" in fns
    assert all(d["certainty"] == "asserted" for d in data)


def test_contracts_join_prefix_normalize_like_java(tmp_path: Path) -> None:
    _write(tmp_path)
    records = _run("contracts", tmp_path)
    contracts = [r["data"] for r in records if r["record_type"] == "contract"]
    specs = {(c["spec"]["method"], c["spec"]["path"]) for c in contracts}

    assert ("GET", "/api/owners/{id}") in specs  # class prefix + method route
    assert ("POST", "/api/owners") in specs      # class prefix only
    assert all(c["kind"] == "http_endpoint" for c in contracts)
    assert any(r["record_type"] == "edge" and r["data"]["kind"] == "EXPOSES"
               for r in records)


def test_cross_service_node_matches_platform(tmp_path: Path) -> None:
    from app.merge.contract_id import http_contract_id

    _write(tmp_path)
    contracts = [r["data"] for r in _run("contracts", tmp_path)
                 if r["record_type"] == "contract"]
    ids = {http_contract_id(c["spec"]["method"], c["spec"]["path"])
           for c in contracts}
    # A TS fetch / Java handler / HTML form on POST /api/owners lands here too.
    assert http_contract_id("POST", "/api/owners") in ids


def test_calls_resolve_and_extern(tmp_path: Path) -> None:
    _write(tmp_path)
    edges = [r["data"] for r in _run("calls", tmp_path)]
    # create() → validate() resolved same-file.
    assert any(
        e["source_id"].split("@")[0].endswith("create")
        and e["target_id"].split("@")[0].endswith("validate")
        and e["metadata"]["callee_resolved"] for e in edges
    )
    # main() → println is extern (inferred), not fabricated as resolved.
    externs = [e for e in edges if e["target_id"].startswith("kotlin:extern:")]
    assert externs and all(e["certainty"] == "inferred" for e in externs)


def test_registry_maps_kotlin() -> None:
    from app.analyzers.registry import binary_for

    assert binary_for("kotlin") == "ggoss-kotlin"
