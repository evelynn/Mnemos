"""PR-92 — MCP response-size cap (§11.7) + binary-dotnet entry-point fix.

The Q&A reviewer flagged that ``_call_tool`` did an unconditional
``json.dumps(result)`` and could return ~100KB+ JSON for a god-object
caller list (spec §11.7 mandates 50KB + a cursor / truncated marker).

The analyzer reviewer flagged that ``ggoss-binary-dotnet`` aliased
``is_entry_point = method.IsConstructor`` — every C# constructor in
the analysed DLL was incorrectly flagged the assembly entry point.

This PR adds ``_cap_response`` (truncates the largest list field and
appends ``response_truncated`` / ``truncated_field`` markers) and
fixes the entry-point comparison to ``method == asm.MainModule.EntryPoint``.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"
_DOTNET = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-binary-dotnet" / "src" / "Program.cs"
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_cap_function_present_and_default_50kb():
    body = _read(_APP / "mcp" / "server.py")
    assert "def _cap_response(" in body
    idx = body.find("_MAX_RESPONSE_BYTES")
    slab = body[idx:idx + 200]
    assert "50 * 1024" in slab


def test_call_tool_passes_through_cap():
    body = _read(_APP / "mcp" / "server.py")
    assert "_cap_response(result)" in body
    # Old unconditional json.dumps in the return is gone.
    assert "json.dumps(result, default=str))" not in body


def test_cap_function_unit_behaviour():
    """Pure-function check — a small result returns its JSON exactly;
    a list-shaped overflow comes back with the response_truncated
    marker and the original total."""
    import json
    from app.mcp.server import _cap_response

    small = {"a": 1, "b": [1, 2, 3]}
    assert json.loads(_cap_response(small)) == small

    big = {"edges": ["x" * 100] * 1000}
    out = json.loads(_cap_response(big, max_bytes=2000))
    assert out["response_truncated"] is True
    assert out["truncated_field"] == "edges"
    assert out["truncated_total"] == 1000
    assert len(out["edges"]) < 1000


def test_binary_dotnet_entry_point_fixed():
    body = _read(_DOTNET)
    # No longer aliases constructor → entry point.
    assert "is_entry_point = method.IsConstructor" not in body
    # Compares against the assembly's actual entry point.
    assert "MainModule.EntryPoint" in body
