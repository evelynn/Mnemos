"""PR-128 — ggoss-binary-dotnet REAL execution against compiled DLL.

4번째 분석기 검증. ggoss-binary-dotnet 는 record/property/methods 의
auto-generated 멤버까지 모두 emit 함 — precision 측정은 의미 없음
(operator 가 .ctor, get_X, set_X 매번 enumerate 못 함). 대신 recall
fixed-set: developer-defined methods 가 전부 발견되는가만 검증.

What this proves:
1. ggoss-binary-dotnet builds locally without docker
2. It loads + reflects a real compiled DLL (Project.dll from PR-127)
3. It emits every developer-defined method
4. It emits the data_entity record for the assembly
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DLL = Path("/tmp/ggoss-bin-out/ggoss-binary-dotnet.dll")
_TARGET_DIR = _ROOT / "scripts" / "accuracy" / "fixtures" / "sample-cs-project" / "bin" / "Release" / "net8.0"


requires_built = pytest.mark.skipif(
    shutil.which("dotnet") is None or not _DLL.is_file() or not _TARGET_DIR.is_dir(),
    reason="dotnet missing OR ggoss-binary-dotnet not published OR Project.dll missing",
)


def _run() -> list[dict]:
    cp = subprocess.run(
        ["dotnet", str(_DLL), "symbols", str(_TARGET_DIR)],
        capture_output=True, text=True, timeout=60,
    )
    assert cp.returncode == 0, f"crashed: {cp.stderr[:400]}"
    return [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]


@requires_built
def test_binary_dotnet_emits_data_entity_for_assembly():
    """Every analysed DLL emits a data_entity envelope describing
    the assembly (version, references). Dashboard 'Components' tab
    relies on this."""
    envs = _run()
    de = [e for e in envs if e["record_type"] == "data_entity"]
    assert de, "no data_entity envelope for the DLL"
    d = de[0]["data"]
    assert d.get("kind") == "binary"
    assert "Project" in d.get("name", "") or "Project" in d.get("id", "")


@requires_built
def test_binary_dotnet_extracts_every_developer_method():
    """Developer-defined methods (Add, GetById, Cancel, CreateOrder,
    ConfirmOrder) must all be extracted. Auto-generated record
    members (.ctor, get_X, set_X, Equals, etc.) are implementation
    detail and not asserted on."""
    envs = _run()
    names = {e["data"]["name"] for e in envs if e["record_type"] == "symbol"}
    must_have = {"Add", "GetById", "Cancel", "CreateOrder", "ConfirmOrder"}
    missing = must_have - names
    assert not missing, f"missing developer methods: {missing}"


@requires_built
def test_binary_dotnet_emits_record_auto_members():
    """Record types in C# emit auto getters/setters; the reflection
    pass captures them too. Confirm the record's expected
    auto-generated members appear so a downstream consumer can
    distinguish 'really missing' from 'never had it'."""
    envs = _run()
    names = {e["data"]["name"] for e in envs if e["record_type"] == "symbol"}
    # Order record (positional record) → these auto-members exist.
    record_auto = {"get_Id", "set_Id", "get_CustomerId", "Equals", "GetHashCode"}
    assert record_auto.issubset(names), (
        f"missing record auto-members: {record_auto - names}"
    )


@requires_built
def test_binary_dotnet_includes_assembly_metadata():
    """The data_entity envelope must carry assembly version +
    references so the platform can reason about deployment
    compatibility."""
    envs = _run()
    de = next(e for e in envs if e["record_type"] == "data_entity")
    meta = de["data"].get("metadata", {})
    assert "version" in meta
    assert "references" in meta
    assert isinstance(meta["references"], list)
    # Project.dll references System.Runtime / System.Collections /
    # System.Linq — at least one must show.
    assert any("System." in r for r in meta["references"])
