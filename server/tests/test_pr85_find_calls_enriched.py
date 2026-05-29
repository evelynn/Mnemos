"""PR-85 — find_callers / find_callees enriched per spec §11.3.

The Q&A review found both helpers shipped a stripped-down shape:
- no ``transitive`` parameter — every call was depth-1;
- no ``max_depth`` — agents couldn't bound a transitive walk;
- no ``exercised`` field on the returned edges — the OTLP-confirmed
  flag was invisible to Q&A;
- no ``truncated`` on the response — a 1000-row cap silently chopped
  a god-object's caller list.

Both now share a ``_walk_calls`` BFS that returns
``{edges, truncated, depth_reached}``. ``impact_analysis`` was
updated to use the new shape.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_walk_helper_shared_by_both_finds():
    body = _read(_APP / "mcp" / "queries.py")
    assert "async def _walk_calls(" in body
    # find_callers and find_callees both delegate.
    idx_callers = body.find("async def find_callers(")
    idx_callees = body.find("async def find_callees(")
    slab_callers = body[idx_callers:idx_callers + 600]
    slab_callees = body[idx_callees:idx_callees + 600]
    assert "_walk_calls(" in slab_callers
    assert "_walk_calls(" in slab_callees
    assert 'direction="callers"' in slab_callers
    assert 'direction="callees"' in slab_callees


def test_finds_accept_transitive_and_max_depth():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def find_callers(")
    slab = body[idx:idx + 600]
    assert "transitive: bool = False" in slab
    assert "max_depth: int = 3" in slab


def test_edge_carries_exercised_flag():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("def _edge_out(")
    slab = body[idx:idx + 500]
    assert '"exercised"' in slab
    assert '(e.data or {}).get("exercised"' in slab


def test_response_carries_truncated_and_depth_reached():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def _walk_calls(")
    slab = body[idx:idx + 2200]
    assert '"truncated"' in slab and "truncated" in slab
    assert '"depth_reached"' in slab


def test_impact_analysis_uses_new_edges_shape():
    """impact_analysis must consume the new dict shape, not the old
    flat list — otherwise it KeyErrors."""
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def impact_analysis(")
    slab = body[idx:idx + 1800]
    assert '"edges"' in slab
    # Old `for e in await find_callers(...)` is gone.
    assert "for e in (\n            await find_callers(" in slab \
        or 'walk = await find_callers(' in slab


def test_mcp_tool_schema_advertises_new_params():
    body = _read(_APP / "mcp" / "server.py")
    idx = body.find('name="find_callers"')
    # PR-105 grew the description block — bump the slab so the
    # input-schema params (which sit after the description) stay
    # in range.
    slab = body[idx:idx + 1800]
    assert '"transitive"' in slab
    assert '"max_depth"' in slab
