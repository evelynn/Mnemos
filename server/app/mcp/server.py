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
from app.mcp.data_queries import (
    get_column_stats,
    get_data_entity,
    get_sample_data,
    search_data,
)
from app.mcp.queries import (
    find_callees,
    find_callers,
    find_runtime_path,
    get_contract,
    get_module_summary,
    get_symbol,
    impact_analysis,
    list_findings,
    search_symbols,
)

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
    Tool(
        name="find_callees",
        description="List CALLS edges whose source is this symbol.",
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="impact_analysis",
        description=(
            "Transitive caller walk. Returns directly + transitively affected "
            "symbols. Test/data impacts arrive in later phases."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "max_depth": {"type": "integer", "default": 3},
            },
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="get_contract",
        description="Fetch a Contract node plus its exposers and callers.",
        inputSchema={
            "type": "object",
            "properties": {"contract_id": {"type": "string"}},
            "required": ["contract_id"],
        },
    ),
    Tool(
        name="read_file",
        description=(
            "Read a file from the platform's repo mirror at /var/lib/mnemos/repos. "
            "Paths are resolved below the project's checkout root."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "file_path": {"type": "string"},
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="get_data_entity",
        description="Fetch a DataEntity node plus sample availability.",
        inputSchema={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    ),
    Tool(
        name="get_sample_data",
        description=(
            "Return the most recent masked sample for a DataEntity. "
            "Refuses sensitive entities."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["entity_id"],
        },
    ),
    Tool(
        name="get_column_stats",
        description="Return stored stats for a single column from the latest sample.",
        inputSchema={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "column": {"type": "string"},
            },
            "required": ["entity_id", "column"],
        },
    ),
    Tool(
        name="search_data",
        description="Scan stored samples for values matching a regex.",
        inputSchema={
            "type": "object",
            "properties": {
                "value_pattern": {"type": "string"},
                "max_hits": {"type": "integer", "default": 50},
            },
            "required": ["value_pattern"],
        },
    ),
    Tool(
        name="list_findings",
        description="List current Findings; filter by severity/status.",
        inputSchema={
            "type": "object",
            "properties": {
                "severity": {"type": "string", "nullable": True},
                "status": {"type": "string", "nullable": True},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="get_module_summary",
        description="Return the current L1/L2/L3 summary for a target node.",
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "level": {"type": "integer"},
            },
            "required": ["target_id", "level"],
        },
    ),
    Tool(
        name="find_runtime_path",
        description=(
            "BFS over exercised CALLS edges starting at a contract. Shows "
            "chains that were observed in telemetry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entry_contract_id": {"type": "string"},
                "max_depth": {"type": "integer", "default": 6},
            },
            "required": ["entry_contract_id"],
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
            elif name == "find_callees":
                result = await find_callees(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                    limit=int(arguments.get("limit", 100)),
                )
            elif name == "impact_analysis":
                result = await impact_analysis(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                    max_depth=int(arguments.get("max_depth", 3)),
                )
            elif name == "get_contract":
                result = await get_contract(
                    db,
                    project_id=project_id,
                    contract_id=arguments["contract_id"],
                )
                if result is None:
                    result = {"error": "not_found"}
            elif name == "read_file":
                from app.mcp.file_read import read_project_file

                result = await read_project_file(
                    project_id=project_id,
                    file_path=arguments["file_path"],
                )
            elif name == "get_data_entity":
                result = await get_data_entity(
                    db, project_id=project_id, entity_id=arguments["entity_id"]
                )
                if result is None:
                    result = {"error": "not_found"}
            elif name == "get_sample_data":
                result = await get_sample_data(
                    db,
                    project_id=project_id,
                    entity_id=arguments["entity_id"],
                    limit=int(arguments.get("limit", 10)),
                )
            elif name == "get_column_stats":
                result = await get_column_stats(
                    db,
                    project_id=project_id,
                    entity_id=arguments["entity_id"],
                    column=arguments["column"],
                )
            elif name == "search_data":
                result = await search_data(
                    db,
                    project_id=project_id,
                    value_pattern=arguments["value_pattern"],
                    max_hits=int(arguments.get("max_hits", 50)),
                )
            elif name == "list_findings":
                result = await list_findings(
                    db,
                    project_id=project_id,
                    severity=arguments.get("severity"),
                    status=arguments.get("status"),
                    limit=int(arguments.get("limit", 50)),
                )
            elif name == "get_module_summary":
                result = await get_module_summary(
                    db,
                    project_id=project_id,
                    target_id=arguments["target_id"],
                    level=int(arguments["level"]),
                )
                if result is None:
                    result = {"error": "not_found"}
            elif name == "find_runtime_path":
                result = await find_runtime_path(
                    db,
                    project_id=project_id,
                    entry_contract_id=arguments["entry_contract_id"],
                    max_depth=int(arguments.get("max_depth", 6)),
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
