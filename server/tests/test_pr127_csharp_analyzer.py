"""PR-127 — ggoss-csharp real accuracy measurement.

User's second point made plain: "Docker 없이도 다른 솔루션들은
가능했는데..." — apt installed dotnet 8.0 directly, ggoss-csharp
builds with `dotnet publish`, and now its accuracy is measured
on a hand-curated C# fixture.

3/6 analyzers now floor-pass:
- ggoss-ts: 1.0/1.0/1.0 (PR-114)
- ggoss-py: 1.0/1.0/1.0 (PR-115)
- ggoss-csharp: 1.0/1.0/1.0 (THIS PR)

What this proves:
1. ggoss-csharp builds locally without docker (apt dotnet-sdk-8.0)
2. It extracts every symbol in the C# fixture (perfect symbol recall)
3. It resolves intra-project method calls correctly
4. The harness's framework-prefix extern filter correctly excludes
   System.Collections.* / System.Linq.* callees (BCL is library code,
   not project code, and operators don't enumerate it in fixtures)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = _ROOT / "scripts" / "accuracy" / "fixtures" / "sample-cs-project"
_DLL = Path("/tmp/ggoss-csharp-out/ggoss-csharp.dll")

sys.path.insert(0, str(_ROOT / "scripts" / "accuracy"))
import measure as harness  # type: ignore  # noqa: E402

requires_dotnet_and_build = pytest.mark.skipif(
    shutil.which("dotnet") is None or not _DLL.is_file(),
    reason="dotnet missing or ggoss-csharp.dll not built (run `dotnet publish`)",
)


def _run(verb: str) -> list[dict]:
    cp = subprocess.run(
        ["dotnet", str(_DLL), verb, str(_FIXTURE)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"ggoss-csharp {verb} failed: {cp.stderr[:400]}"
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


@requires_dotnet_and_build
def test_csharp_extracts_all_eight_fixture_symbols():
    """sample-cs-project has 8 symbols (Order record, OrdersRepo
    + 3 methods, OrderService + 2 methods). ggoss-csharp must
    find all of them — symbol-recall floor."""
    envs = _run("symbols")
    syms = [e["data"] for e in envs if e["record_type"] == "symbol"]
    names = sorted(s["name"] for s in syms)
    expected = sorted([
        "Order", "OrdersRepo", "Add", "GetById", "Cancel",
        "OrderService", "CreateOrder", "ConfirmOrder",
    ])
    assert names == expected, f"got: {names}, expected: {expected}"


@requires_dotnet_and_build
def test_csharp_resolves_intra_project_call_creator_to_add():
    """OrderService.CreateOrder calls OrdersRepo.Add — the edge
    must be emitted with both ids in the project namespace."""
    envs = _run("calls")
    edges = [e["data"] for e in envs if e["record_type"] == "edge"]
    matches = [
        e for e in edges
        if "CreateOrder" in e["source_id"] and "OrdersRepo.Add" in e["target_id"]
    ]
    assert matches, "CreateOrder → OrdersRepo.Add edge missing"


@requires_dotnet_and_build
def test_csharp_meets_floors_on_fixture():
    """The integration claim: ggoss-csharp scores at floor on
    its hand-curated fixture. CI gate-worthy."""
    envs = _run("symbols") + _run("calls")
    unwrapped = [harness._unwrap_payload(e) for e in envs]
    report = harness.measure("csharp", "sample-cs-project", actual_envelopes=unwrapped)
    assert harness.meets_floors(report, "symbols"), report["symbols"]
    assert harness.meets_floors(report, "edges"), report["edges"]
    # Perfect score on this fixture.
    assert report["symbols"]["f1"] == 1.0
    assert report["edges"]["f1"] == 1.0


@requires_dotnet_and_build
def test_csharp_framework_calls_classified_as_extern():
    """BCL calls (System.Collections.Generic.List.Add, System.Linq.*)
    must be excluded from the in-project edge count — otherwise
    the precision metric is destroyed by them."""
    envs = _run("symbols") + _run("calls")
    unwrapped = [harness._unwrap_payload(e) for e in envs]
    report = harness.measure("csharp", "sample-cs-project", actual_envelopes=unwrapped)
    # The fixture's OrdersRepo.Add calls List.Add, GetById calls
    # FirstOrDefault — that's at least 2 BCL calls.
    assert report["extern_edges_emitted"] >= 2


def test_framework_prefix_filter():
    """Direct unit test of the framework-prefix extern filter
    (regression guard)."""
    bcl_edge = {"target_id": "csharp:M:System.Collections.Generic.List.Add",
                "kind": "CALLS"}
    project_edge = {"target_id": "csharp:M:MyApp.Service.DoIt", "kind": "CALLS"}
    assert harness._is_extern_edge(bcl_edge) is True
    assert harness._is_extern_edge(project_edge) is False


def test_bare_symbol_name_handles_csharp_param_list():
    """The id normaliser must strip C#-style (Type, Type) param
    lists so fixture short names match analyzer-emitted full ids."""
    cases = [
        ("csharp:M:SampleProject.OrdersRepo.Add(SampleProject.Order)", "Add"),
        ("csharp:T:SampleProject.OrderService", "OrderService"),
        ("csharp:M:SampleProject.OrdersRepo.GetById(System.String)", "GetById"),
    ]
    for raw, want in cases:
        got = harness._bare_symbol_name(raw)
        assert got == want, f"_bare_symbol_name({raw!r}) → {got!r}, want {want!r}"
