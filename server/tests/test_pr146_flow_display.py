"""PR-146 — the "show me the process" surfaces.

Verifying Q&A surfaced a display gap: trace_flow persists a level-4
Summary, but Claude Code had no MCP tool to discover/show flows, and the
report GUI only rendered level-3. So a requested process couldn't be
shown. This adds the list_flows MCP tool and a level-4 panel in the
report tab. These guards keep both surfaces wired.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr146")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

_TPL = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "templates" / "report.html"


def test_list_flows_query_helper_exists():
    from app.mcp import queries

    assert hasattr(queries, "list_flows")


def test_mcp_server_registers_list_flows_tool_and_dispatch():
    import uuid

    from app.mcp import server as mcp_server

    names = {t.name for t in mcp_server._TOOLS}
    assert "list_flows" in names, "list_flows MCP tool must be registered"
    # description must convey it SHOWS the cross-tier process
    tool = next(t for t in mcp_server._TOOLS if t.name == "list_flows")
    assert "process" in tool.description.lower()

    # build_server wires a dispatcher; the source must handle the tool.
    import inspect

    src = inspect.getsource(mcp_server.build_server)
    assert 'name == "list_flows"' in src
    assert isinstance(mcp_server.build_server(uuid.uuid4()), object)


def test_report_tab_fetches_and_renders_level4_flows():
    html = _TPL.read_text(encoding="utf-8")
    # fetches level-4 summaries
    assert "summaries?level=4" in html, "report must fetch level-4 flows"
    # has a container + renders steps/flags
    assert 'id="rp-flows"' in html
    assert "flow-steps" in html and "flow-flags" in html
