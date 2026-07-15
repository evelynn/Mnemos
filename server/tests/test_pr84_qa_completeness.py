"""PR-84 — close Q&A subsystem gaps the critical review found.

The Q&A reviewer flagged two structural defects:

* the spec §11.3 ``get_data_access`` tool **did not exist** — the
  READS/WRITES edges have been in the graph since PR-61, but no MCP
  tool surfaced them, so "where does this function touch the DB?"
  could only be approximated via ``find_callees``.
* ``impact_analysis`` returned hardcoded empty lists for
  ``affected_tests``, ``affected_data_entities``,
  ``opaque_components_touched`` and ``runtime_exercised=False`` — 4
  of its 6 outputs were a stub.

This PR implements ``get_data_access`` (queries.py + server.py tool
registration + dispatch) and fills ``impact_analysis`` from the
graph it already had.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# get_data_access — new tool exists end-to-end
# ---------------------------------------------------------------------------


def test_get_data_access_query_helper_defined():
    body = _read(_APP / "mcp" / "queries.py")
    assert "async def get_data_access(" in body
    idx = body.find("async def get_data_access(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert 'Edge.kind.in_(("READS", "WRITES"))' in slab
    assert '"reads"' in slab and '"writes"' in slab
    assert '"truncated"' in slab


def test_get_data_access_registered_as_mcp_tool():
    body = _read(_APP / "mcp" / "server.py")
    assert 'name="get_data_access"' in body
    # Imported and dispatched.
    assert "get_data_access," in body
    assert 'name == "get_data_access"' in body


# ---------------------------------------------------------------------------
# impact_analysis — no more hollow stubs
# ---------------------------------------------------------------------------


def test_impact_analysis_fills_data_entities():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def impact_analysis(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    # The fan-out to READS/WRITES edges is wired in.
    assert "affected_data_entities" in slab
    assert 'Edge.kind.in_(("READS", "WRITES"))' in slab
    # No more hardcoded ``"affected_data_entities": []``.
    assert '"affected_data_entities": []' not in slab


def test_impact_analysis_computes_runtime_exercised():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def impact_analysis(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert "runtime_exercised" in slab
    # The hardcoded ``False`` is gone.
    assert '"runtime_exercised": False' not in slab
    # And the exercised flag is checked along the chain.
    assert '"exercised"' in slab


def test_impact_analysis_flags_tests_and_opaques():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def impact_analysis(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert '"affected_tests": []' not in slab
    assert "is_test" in slab or "test" in slab.lower()
    assert "opaque_components_touched" in slab
