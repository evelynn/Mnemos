"""PR-95 (P1-T2): close the last small Q&A + platform gaps.

* Q&A reviewer #12 — ``search_symbols`` lacked the spec §11.3
  ``component_id`` filter; scoping the search to one component was
  impossible. Param added end-to-end (helper, MCP schema, dispatch).
* Q&A reviewer #13 — ``find_runtime_path`` had no ``time_window``;
  an edge exercised once six months ago leaked into "common paths
  today". A ``"7d"`` / ``"24h"`` / ``"4w"`` window now filters edges
  by ``data.last_seen_at``.
* Platform reviewer #15 — ``_STALE_RUN_AFTER_SEC`` was hardcoded to
  6h. Monorepo runs that legitimately exceed could be flipped to
  failed by the cron sweep. ``MNEMOS_STALE_RUN_AFTER_SEC`` now
  overrides at module load.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_search_symbols_accepts_component_id():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def search_symbols(")
    slab = body[idx:idx + 2200]
    assert "component_id: str | None = None" in slab
    assert 'Node.data["component_id"]' in slab


def test_search_symbols_tool_schema_advertises_component_id():
    body = _read(_APP / "mcp" / "server.py")
    idx = body.find('name="search_symbols"')
    slab = body[idx:idx + 2000]
    assert '"component_id"' in slab


def test_search_symbols_dispatch_threads_component_id():
    body = _read(_APP / "mcp" / "server.py")
    idx = body.find('name == "search_symbols"')
    slab = body[idx:idx + 700]
    assert 'component_id=arguments.get("component_id")' in slab


def test_find_runtime_path_accepts_time_window():
    body = _read(_APP / "mcp" / "queries.py")
    idx = body.find("async def find_runtime_path(")
    next_fn = body.find("\nasync def ", idx + 1)
    slab = body[idx:next_fn]
    assert "time_window: str | None = None" in slab
    assert "_parse_time_window" in slab
    # The cutoff filters the durable runtime overlay's canonical read view.
    assert "GraphEdgeRuntimeOverlay" in slab
    assert 'view["data"].get("last_seen_at")' in slab


def test_parse_time_window_unit():
    from app.mcp.queries import _parse_time_window

    assert _parse_time_window("7d") == 7 * 86400
    assert _parse_time_window("24h") == 24 * 3600
    assert _parse_time_window("4w") == 4 * 7 * 86400
    assert _parse_time_window("") is None
    assert _parse_time_window(None) is None
    assert _parse_time_window("garbage") is None
    assert _parse_time_window("0d") is None  # zero rejected


def test_stale_run_window_overridable(monkeypatch):
    # Re-import to re-evaluate the module-level helper.
    monkeypatch.setenv("MNEMOS_STALE_RUN_AFTER_SEC", "12345")
    from app.orchestrator.cron_jobs import _stale_run_after_sec

    assert _stale_run_after_sec() == 12345
    monkeypatch.setenv("MNEMOS_STALE_RUN_AFTER_SEC", "garbage")
    assert _stale_run_after_sec() == 24 * 60 * 60
    monkeypatch.delenv("MNEMOS_STALE_RUN_AFTER_SEC", raising=False)
    assert _stale_run_after_sec() == 24 * 60 * 60
