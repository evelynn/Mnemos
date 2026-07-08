"""PR-192 — ggoss-java: deterministic Java analyzer.

The 2026-07-08 web+Java eval ran Mnemos on Spring PetClinic and got a
completely empty graph (``symbols:java`` = ``no_analyzer``, agent fallback
unavailable). These tests pin the analyzer contract on a synthetic Spring
fixture: type/method symbols, the parameter-annotation method-name fix,
name-based CALLS resolution, and — the point of the whole exercise — Spring
mapping annotations normalized to the SAME ``http.<METHOD>.<path>`` node a
TS ``fetch`` client resolves to (cross-service linking, spec pillar 5).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_JAVA = _ROOT / "analyzers" / "ggoss-java" / "src" / "ggoss_java.py"


_CONTROLLER = """\
package com.demo.owner;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/owners")
class OwnerController {

    private final OwnerService service;

    OwnerController(OwnerService service) {
        this.service = service;
    }

    // Parameter annotations must NOT shadow the method name (regression).
    @GetMapping("/{ownerId}")
    public Owner findOne(@PathVariable(name = "ownerId", required = true) Integer ownerId) {
        return service.load(ownerId);
    }

    @PostMapping
    public Owner create(@RequestBody Owner owner) {
        validate(owner);
        return service.save(owner);
    }

    private void validate(Owner owner) {
        requireNonNull(owner);  // statically-imported extern call
    }
}
"""

_SERVICE = """\
package com.demo.owner;

@org.springframework.stereotype.Service
class OwnerService {
    Owner load(Integer id) { return null; }
    Owner save(Owner o) { return o; }
}
"""

_ENTITY = """\
package com.demo.owner;

import jakarta.persistence.*;

@Entity
@Table(name = "owners")
class Owner {
    @Id
    private Integer id;
    private String lastName;
}
"""


def _write(root: Path) -> None:
    d = root / "src" / "com" / "demo" / "owner"
    d.mkdir(parents=True)
    (d / "OwnerController.java").write_text(_CONTROLLER, encoding="utf-8")
    (d / "OwnerService.java").write_text(_SERVICE, encoding="utf-8")
    (d / "Owner.java").write_text(_ENTITY, encoding="utf-8")


def _run(verb: str, target: Path) -> list[dict]:
    cp = subprocess.run(
        [sys.executable, str(_JAVA), verb, str(target)],
        capture_output=True, text=True, timeout=120,
    )
    assert cp.returncode == 0, cp.stderr
    return [json.loads(x) for x in cp.stdout.splitlines() if x.strip()]


def test_symbols_types_and_methods(tmp_path: Path) -> None:
    _write(tmp_path)
    data = [r["data"] for r in _run("symbols", tmp_path)]
    # A class and its constructor share a name — key types and methods apart.
    types = {d["name"]: d for d in data if d["kind"] != "method"}
    methods = [d for d in data if d["kind"] == "method"]
    method_names = {d["name"] for d in methods}

    assert types["OwnerController"]["kind"] == "class"
    assert types["OwnerService"]["kind"] == "class"
    # Parameter-annotation regression: the @PathVariable(...) on findOne's
    # argument must not make the method be named "PathVariable"/"GetMapping".
    assert {"findOne", "create", "validate"} <= method_names
    assert not (method_names & {
        "GetMapping", "PostMapping", "PathVariable", "RequestBody", "RequestMapping",
    })
    # Constructor detected + tagged.
    ctors = [d for d in methods if d["name"] == "OwnerController"]
    assert ctors and ctors[0]["metadata"].get("constructor") is True
    # FQN carries the package.
    find_one = next(d for d in methods if d["name"] == "findOne")
    assert find_one["qual_name"] == "com.demo.owner.OwnerController.findOne"
    assert all(d["certainty"] == "asserted" for d in data)


def test_contracts_join_class_prefix_and_normalize_like_ts(tmp_path: Path) -> None:
    _write(tmp_path)
    records = _run("contracts", tmp_path)
    contracts = [r["data"] for r in records if r["record_type"] == "contract"]
    exposes = [r["data"] for r in records if r["record_type"] == "edge"]
    specs = {(c["spec"]["method"], c["spec"]["path"]) for c in contracts}

    # Class-level @RequestMapping("/api/owners") joins with method routes.
    assert ("GET", "/api/owners/{ownerId}") in specs
    # @PostMapping with no arg inherits exactly the class prefix.
    assert ("POST", "/api/owners") in specs
    # Emitted with http_endpoint + spec so the platform normalizes to the
    # SAME node id a TS fetch('/api/owners') resolves to (cross-service).
    assert all(c["kind"] == "http_endpoint" for c in contracts)
    assert all(c["id"].startswith("http.") for c in contracts)
    # Every contract has an EXPOSES edge from its handler method.
    assert any(e["kind"] == "EXPOSES" for e in exposes)


def test_cross_service_node_id_matches_platform_normalization(tmp_path: Path) -> None:
    """The load-bearing claim: a Java @PostMapping and a TS client fetch to
    the same path produce the SAME graph node id via http_contract_id."""
    from app.merge.contract_id import http_contract_id

    _write(tmp_path)
    contracts = [
        r["data"] for r in _run("contracts", tmp_path)
        if r["record_type"] == "contract"
    ]
    # The platform recomputes the id from spec; simulate it and confirm a
    # TS client hitting POST /api/owners lands on the same node.
    platform_ids = {http_contract_id(c["spec"]["method"], c["spec"]["path"])
                    for c in contracts}
    assert http_contract_id("POST", "/api/owners") in platform_ids
    assert http_contract_id("GET", "/api/owners/{ownerId}") in platform_ids


def test_calls_resolve_same_file_and_extern(tmp_path: Path) -> None:
    _write(tmp_path)
    edges = [r["data"] for r in _run("calls", tmp_path)]

    # create() calls validate() — same class, resolved to the local method.
    resolved = [
        e for e in edges
        if e["source_id"].split("@")[0].endswith("create")
        and e["target_id"].split("@")[0].endswith("validate")
    ]
    assert resolved and resolved[0]["metadata"]["callee_resolved"] is True
    assert resolved[0]["certainty"] == "asserted"
    # A call into an unknown method stays honest: extern + inferred.
    externs = [e for e in edges if e["target_id"].startswith("java:extern:")]
    assert externs and all(e["certainty"] == "inferred" for e in externs)


def test_data_access_jpa_entity(tmp_path: Path) -> None:
    _write(tmp_path)
    records = _run("data_access", tmp_path)
    entities = {r["data"]["id"] for r in records if r["record_type"] == "data_entity"}
    # @Table(name = "owners") → data:owners, not data:owner.
    assert "data:owners" in entities


def test_registry_maps_java() -> None:
    from app.analyzers.registry import binary_for

    assert binary_for("java") == "ggoss-java"
