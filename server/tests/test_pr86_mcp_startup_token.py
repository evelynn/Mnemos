"""PR-86 — MCP server fail-closed startup gate (spec §11.6).

The Q&A reviewer flagged that ``ggoss-mcp --project <uuid>`` had no
auth: any process that could spawn the stdio binary owned full read
of that project, including the data tools. Spec §11.6 mandates a
per-deployment / per-project token.

The MCP server now requires ``MNEMOS_MCP_TOKEN`` in the environment
at startup; absent token → structured error on stderr, exit 2. The
platform sets it when it intentionally launches the server; an
operator running it by hand sets it themselves.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_startup_gate_function_defined():
    body = _read(_APP / "mcp" / "server.py")
    assert "def _require_mcp_token(" in body
    idx = body.find("def _require_mcp_token(")
    slab = body[idx:idx + 1300]
    assert "MNEMOS_MCP_TOKEN" in slab
    # Fail-closed: exit non-zero with a structured error.
    assert "sys.exit(2)" in slab
    assert "MNEMOS_MCP_TOKEN_required" in slab


def test_main_invokes_startup_gate_before_running():
    body = _read(_APP / "mcp" / "server.py")
    idx = body.find("def main()")
    slab = body[idx:idx + 400]
    assert "_require_mcp_token()" in slab
    # Gate before argparse / before _run.
    gate_at = slab.find("_require_mcp_token()")
    run_at = slab.find("asyncio.run")
    assert gate_at < run_at
