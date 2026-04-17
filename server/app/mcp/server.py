"""MCP stdio server.

Runs via ``python -m app.mcp.server --project <uuid>``. Implements the
Phase-1 Week-3 query tools: search_symbols, get_symbol, find_callers
(spec §11.3). Additional tools land in Weeks 4-8 alongside their backing
data.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from app.audit.logger import record as audit_record
from app.db import SessionLocal
from app.mcp.queries import find_callers, get_symbol, search_symbols

_TOOLS = [
    Tool(
        name="search_symbols",
        description=(
            "Search symbols by substring over id and data.name. "
            "Vector+BM25 ranking lands in Week 6."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string", "nullable": True},
                "top_k": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_symbol",
        description=(
            "Fetch a symbol plus caller/callee counts. Body source is not "
            "returned — Claude Code reads files directly."
        ),
        inputSchema={
            "type": "object",
            "properties": {"symbol_id": {"type": "string"}},
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="find_callers",
        description="List CALLS edges whose target is this symbol.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["symbol_id"],
        },
    ),
]


def build_server(project_id: uuid.UUID) -> Server:
    server: Server = Server("mnemos")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return _TOOLS

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        async with SessionLocal() as db:
            if name == "search_symbols":
                result = await search_symbols(
                    db,
                    project_id=project_id,
                    query=arguments.get("query", ""),
                    kind=arguments.get("kind"),
                    top_k=int(arguments.get("top_k", 20)),
                )
            elif name == "get_symbol":
                result = await get_symbol(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                )
                if result is None:
                    result = {"error": "not_found"}
            elif name == "find_callers":
                result = await find_callers(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                    limit=int(arguments.get("limit", 100)),
                )
            else:
                result = {"error": f"unknown tool: {name}"}
            await audit_record(
                actor="claude_code:mcp",
                action=f"mcp.tool.{name}",
                target=str(project_id),
                project_id=project_id,
                details={"arguments": arguments},
            )

        import json

        return [TextContent(type="text", text=json.dumps(result, default=str))]

    return server


async def _run(project_id: uuid.UUID) -> None:
    server = build_server(project_id)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project UUID")
    args = parser.parse_args()
    asyncio.run(_run(uuid.UUID(args.project)))


if __name__ == "__main__":
    main()
