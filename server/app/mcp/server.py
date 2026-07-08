"""MCP stdio server.

Runs via ``python -m app.mcp.server --project <uuid>``. Implements the
Phase-1 Week-3 query tools: search_symbols, get_symbol, find_callers
(spec §11.3). Additional tools land in Weeks 4-8 alongside their backing
data.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import uuid

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# Docker-free local mode runs MCP against SQLite too. Install the same
# PostgreSQL-type shims serve_local installs before app.db/models are imported,
# otherwise UUID/JSONB/ARRAY comparisons can silently miss rows.
if os.environ.get("MNEMOS_LOCAL_MODE") == "1" or os.environ.get(
    "DATABASE_URL", ""
).startswith("sqlite"):
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()

from app.artifacts import build_project_index, build_task_context_pack
from app.audit.logger import record as audit_record
from app.db import SessionLocal
from app.mcp.data_queries import (
    get_column_stats,
    get_data_entity,
    get_sample_data,
    search_data,
)
from app.mcp.dev_tools import (
    edit_file_in_worktree,
    run_in_sandbox_tool,
    submit_diff as submit_diff_tool,
    submit_plan as submit_plan_tool,
)
from app.mcp.queries import (
    compare_runs,
    find_callees,
    find_callers,
    find_runtime_path,
    get_contract,
    get_data_access,
    get_module_summary,
    get_symbol,
    impact_analysis,
    list_findings,
    list_flows,
    search_symbols,
)

_TOOLS = [
    Tool(
        name="get_project_index",
        description=(
            "Return the compact AI index for this project: graph counts, "
            "certainty breakdown, latest run, top contracts, hot symbols, "
            "data entities, risk queue, and recommended MCP workflows.\n\n"
            "Use when: starting work in a large repository or after switching "
            "projects. Do not ask for the whole graph or read the whole repo; "
            "use IDs from this index to request get_task_context_pack or "
            "targeted graph queries."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "top_k": {"type": "integer", "default": 25},
            },
        },
    ),
    Tool(
        name="get_task_context_pack",
        description=(
            "Return a bounded, machine-readable context pack for one task "
            "target (Symbol, Contract, DataEntity, Edge, or Finding). The "
            "pack contains target facts, caller/callee/data-access slices, "
            "related findings, summaries, evidence refs, and next MCP "
            "queries. Raw source is intentionally excluded.\n\n"
            "Use when: preparing to edit, debug, or review one concrete "
            "target. This is the standard handoff from Mnemos analysis to "
            "Claude Code/Codex implementation work."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "target_kind": {
                    "type": "string",
                    "default": "auto",
                    "enum": [
                        "auto",
                        "node",
                        "symbol",
                        "contract",
                        "data_entity",
                        "edge",
                        "finding",
                    ],
                },
                "intent": {"type": "string", "nullable": True},
                "budget_items": {"type": "integer", "default": 50},
            },
            "required": ["target_id"],
        },
    ),
    Tool(
        name="search_symbols",
        description=(
            "Search symbols by ranked multi-term lexical match over id, "
            "name and signature (PR-80 BM25-ish; PR-90 fuses vector "
            "when MNEMOS_EMBEDDING_PROVIDER is configured). Optional "
            "kind / component_id filters per spec §11.3. scope narrows by "
            "source role (product = product+support code, tests = test "
            "helpers, all = everything); path_prefix keeps only symbols "
            "under that project-relative path. Results carry location "
            "(file, relative_file, line) and source_role so you can read "
            "the file narrowly without a second lookup.\n\n"
            "Use when: starting from a fuzzy name (\"the order-processing "
            "function\") and you need a symbol_id to feed into the other "
            "tools. Don't call this if you already have a fully-qualified "
            "symbol_id — go straight to get_symbol."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "kind": {"type": "string", "nullable": True},
                "component_id": {"type": "string", "nullable": True},
                "top_k": {"type": "integer", "default": 20},
                "scope": {
                    "type": "string",
                    "enum": ["all", "product", "tests"],
                    "default": "all",
                },
                "path_prefix": {"type": "string", "nullable": True},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="get_symbol",
        description=(
            "Fetch a symbol plus caller/callee counts. Body source is not "
            "returned — Claude Code reads files directly.\n\n"
            "Use when: you have a symbol_id and want the high-level shape "
            "(signature, file path, callsite counts, L1 summary). The "
            "right next step before find_callers/find_callees, since the "
            "caller/callee counts tell you whether transitive=true is "
            "going to be cheap or huge."
        ),
        inputSchema={
            "type": "object",
            "properties": {"symbol_id": {"type": "string"}},
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="find_callers",
        description=(
            "List CALLS edges whose target is this symbol. With "
            "transitive=true, walks the caller graph up to max_depth. "
            "Each edge surfaces certainty and the OTLP exercised flag; "
            "the response carries truncated + depth_reached.\n\n"
            "Use when: \"who calls X?\" — answering a change-impact "
            "question. transitive=false (default) is the cheap direct-"
            "caller list; flip transitive=true only when you need the "
            "full upstream tree (e.g. \"would removing X break "
            "anything?\"). Combine with the exercised flag to focus on "
            "callers actually hit in production."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
                "transitive": {"type": "boolean", "default": False},
                "max_depth": {"type": "integer", "default": 3},
            },
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="find_callees",
        description=(
            "List CALLS edges whose source is this symbol. With "
            "transitive=true, walks downstream up to max_depth. Each "
            "edge carries exercised; response has truncated + "
            "depth_reached.\n\n"
            "Use when: \"what does X depend on?\" — understanding what a "
            "function actually does before changing it. transitive=true "
            "is useful for \"what's the blast radius of X's side "
            "effects?\". For data dependencies specifically, prefer "
            "get_data_access — it filters to READS/WRITES edges."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
                "transitive": {"type": "boolean", "default": False},
                "max_depth": {"type": "integer", "default": 3},
            },
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="impact_analysis",
        description=(
            "Transitive caller walk. Returns directly + transitively affected "
            "symbols. Test/data impacts arrive in later phases.\n\n"
            "Use when: a Plan proposes changing X and you need the "
            "\"things that might break\" list for the diff's risk "
            "summary. This is a higher-level wrapper over "
            "find_callers(transitive=true) — prefer impact_analysis for "
            "the standard \"impact report\" output shape; use find_"
            "callers when you need the raw edge list with certainties."
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
        description=(
            "Fetch a Contract node plus its exposers and callers.\n\n"
            "Use when: you have an HTTP path / gRPC method / message-bus "
            "topic id and want to know who serves it AND who consumes "
            "it. The exposers list is the answer to \"which service "
            "owns this endpoint?\"; callers is \"who would I break by "
            "changing the contract?\". For the implementation behind "
            "an exposer, follow up with get_symbol on the returned "
            "node ids."
        ),
        inputSchema={
            "type": "object",
            "properties": {"contract_id": {"type": "string"}},
            "required": ["contract_id"],
        },
    ),
    Tool(
        name="get_data_access",
        description=(
            "List DataEntities this symbol reads / writes — spec §11.3. "
            "Returns {reads, writes, truncated}; each item carries "
            "certainty, exercised flag and the access site.\n\n"
            "Use when: writing a data-flow doc or assessing a privacy "
            "review (\"does this function touch user PII?\"). The "
            "writes list is the most useful side — anything that "
            "*mutates* a DataEntity is where data-quality regressions "
            "start. For the entity's schema and sample data, follow up "
            "with get_data_entity + get_sample_data."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "symbol_id": {"type": "string"},
                "limit": {"type": "integer", "default": 200},
            },
            "required": ["symbol_id"],
        },
    ),
    Tool(
        name="read_file",
        description=(
            "Read a file from the platform's repo mirror at /var/lib/mnemos/repos. "
            "Supply start_line + end_line to stream a window of a huge file; "
            "omitted range returns the first 2000 lines plus truncated=true.\n\n"
            "Use when: a graph query (search_symbols / get_symbol) gave "
            "you a file_path + line_range and you need the actual code. "
            "This reads the platform's snapshot, NOT a live filesystem, "
            "so what you see is the mirror at the last analysis run's "
            "git_sha. Pin to a narrow line range to fit in the response "
            "cap (§11.7, 50 KB)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "file_path": {"type": "string"},
                "start_line": {"type": "integer", "nullable": True},
                "end_line": {"type": "integer", "nullable": True},
            },
            "required": ["file_path"],
        },
    ),
    Tool(
        name="get_data_entity",
        description=(
            "Fetch a DataEntity node plus sample availability.\n\n"
            "Use when: you have a DataEntity id (\"db.schema.table\") "
            "from a graph query and need its column list, "
            "sensitivity flag, and whether a sample exists. The "
            "sensitivity flag matters: if true, get_sample_data will "
            "refuse — escalate via the dashboard's data tab instead."
        ),
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
            "Refuses sensitive entities.\n\n"
            "Use when: you need to see the *shape* of real data (a few "
            "representative rows) to write a correct query or "
            "transformation. Masked per the platform's masking_rules "
            "+ baseline PII regex (RRN/phone/card/email replaced). "
            "Sensitive entities return 403 — that's intentional, "
            "don't retry with different params."
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
        description=(
            "Return stored stats for a single column from the latest sample.\n\n"
            "Use when: you need null-rate / distinct-count / min-max for "
            "a column — to decide \"is this column safe to use as a "
            "join key?\" or \"is this nullable in practice?\". Cheaper "
            "than get_sample_data when you only need aggregates, not "
            "individual rows."
        ),
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
        description=(
            "Scan stored samples for values matching a regex.\n\n"
            "Use when: \"which table holds this specific value?\" — "
            "tracking down where a specific reference id, error code, "
            "or magic constant lives in data. Scans masked samples only "
            "(so PII won't match) — use a value you'd expect to see "
            "verbatim (status code, sentinel string, foreign key prefix)."
        ),
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
        description=(
            "List current Findings; filter by severity/status.\n\n"
            "Use when: prioritising work — \"what should I fix this "
            "sprint?\". Default sort is risk score descending, so the "
            "first results are the P1s. Pass status=\"open\" to "
            "exclude acknowledged/resolved noise. Each finding "
            "carries a remediation hint + cwe_id — feed those into "
            "submit_plan when starting a fix."
        ),
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
        description=(
            "Return the current L1/L2/L3 summary for a target node.\n\n"
            "Use when: orienting in unfamiliar code — \"what does this "
            "module do, at a glance?\". L1 ≈ a sentence per symbol, "
            "L2 ≈ a paragraph per module, L3 ≈ system-level narrative. "
            "Start at level=2 for most tasks; L3 is for cross-system "
            "context and L1 for quick lookups. Falls back to a stub "
            "when ANTHROPIC_API_KEY is unset — that's signalled by "
            "certainty=\"asserted\" in the response."
        ),
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
        name="list_flows",
        description=(
            "List the cross-tier process flows traced for this project "
            "(level-4 summaries from trace_flow). Each entry carries the "
            "one-line summary plus the step / flag / data sections, so this "
            "single call is enough to SHOW a traced process end-to-end "
            "(frontend → backend → database, the signals crossing each "
            "boundary, every flag value and its meaning, the rows touched).\n\n"
            "Use when: \"walk me through the <X> process\" or \"what flows "
            "have been analysed?\". For a specific one, follow up with "
            "get_module_summary(target_id, level=4)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="find_runtime_path",
        description=(
            "BFS over exercised CALLS edges starting at a contract. Returns "
            "common paths with the bottleneck OTLP hit count as frequency. "
            "Optional time_window (e.g. '7d') drops edges last seen before "
            "the window — per spec §11.3.\n\n"
            "Use when: \"what actually happens when this endpoint is "
            "hit?\" — answering with real production behaviour, not the "
            "static call graph. Pass time_window=\"7d\" to scope to "
            "recent activity (e.g. excluding deprecated paths). For the "
            "static graph alone, use find_callees instead."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "entry_contract_id": {"type": "string"},
                "max_depth": {"type": "integer", "default": 6},
                "time_window": {"type": "string", "nullable": True},
            },
            "required": ["entry_contract_id"],
        },
    ),
    Tool(
        name="compare_runs",
        description=(
            "Diff the graph between two analysis runs (bitemporal): symbols / "
            "contracts / data-entities added, removed, or modified; edge-kind "
            "deltas (CALLS/EXPOSES/READS/WRITES added & removed); and findings "
            "first seen between the runs. Certainty is preserved per change.\n\n"
            "Use when: \"what changed between these two commits/analyses and "
            "what's the blast radius?\" — code review, regression triage, "
            "release notes. This is history-aware analysis: a re-indexing tool "
            "that keeps no snapshots cannot produce it. Get run ids from "
            "get_project_index (latest_run) or the runs list."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "run_a_id": {"type": "string"},
                "run_b_id": {"type": "string"},
                "limit": {"type": "integer", "default": 40},
            },
            "required": ["run_a_id", "run_b_id"],
        },
    ),
    Tool(
        name="submit_plan",
        description=(
            "Submit a Plan for Gate A approval. Creates a worktree at "
            "/var/lib/mnemos/worktrees/<plan>/.\n\n"
            "Use when: you've decided on a fix and want to start "
            "editing code. The plan must include the spec (the *why*), "
            "tasks (the *what*), and target_component_id (the *where*). "
            "Worktree is created on approval — until then no file "
            "edits land. After approval, edit_file_in_worktree + "
            "run_in_sandbox + submit_diff is the loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
                "tasks": {"type": "array"},
                "target_component_id": {"type": "string"},
                "requester": {"type": "string"},
            },
            "required": ["spec", "tasks", "target_component_id", "requester"],
        },
    ),
    Tool(
        name="edit_file_in_worktree",
        description=(
            "Apply string-edits to a file inside the plan's worktree. Rejects "
            "unapproved plans and paths that escape the worktree root.\n\n"
            "Use when: making the code change a Gate-A-approved plan "
            "calls for. Operates strictly inside /var/lib/mnemos/"
            "worktrees/<plan>/; the production mirror is untouched "
            "until Gate B approves the resulting diff. Each edit is a "
            "{old_string, new_string} pair; old_string must match "
            "exactly once."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "file_path": {"type": "string"},
                "edits": {"type": "array"},
            },
            "required": ["plan_id", "file_path", "edits"],
        },
    ),
    Tool(
        name="run_in_sandbox",
        description=(
            "Run an allowlisted command inside the plan worktree.\n\n"
            "Use when: verifying a change you just made with "
            "edit_file_in_worktree — running tests (\"pytest -k\"), a "
            "linter, or a build. Allowlist is per-project; commands "
            "outside it are rejected with a list of allowed prefixes. "
            "Network is off, filesystem is read-only outside /scratch, "
            "and the timeout caps long runs. Output is captured and "
            "returned for self-review."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "command": {"type": "string"},
                "timeout_sec": {"type": "integer", "default": 300},
            },
            "required": ["plan_id", "command"],
        },
    ),
    Tool(
        name="submit_diff",
        description=(
            "Submit the plan worktree's current diff for Gate B approval. "
            "Auto self-review runs and returns findings.\n\n"
            "Use when: edits + tests + lint look right and you want a "
            "human to approve the merge. Self-review (impact analysis, "
            "data-access check, rule set) runs before the submission "
            "is filed — fix any blocking findings before resubmitting. "
            "Attach test_results + self_review_notes so the approver "
            "doesn't have to re-derive your reasoning."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "task_id": {"type": "string"},
                "diff": {"type": "string", "nullable": True},
                "test_results": {"type": "object", "nullable": True},
                "self_review_notes": {"type": "string", "nullable": True},
            },
            "required": ["plan_id", "task_id"],
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
            if name == "get_project_index":
                result = await build_project_index(
                    db,
                    project_id=project_id,
                    top_k=int(arguments.get("top_k", 25)),
                )
            elif name == "get_task_context_pack":
                result = await build_task_context_pack(
                    db,
                    project_id=project_id,
                    target_id=arguments["target_id"],
                    target_kind=arguments.get("target_kind", "auto"),
                    intent=arguments.get("intent"),
                    budget_items=int(arguments.get("budget_items", 50)),
                )
            elif name == "search_symbols":
                result = await search_symbols(
                    db,
                    project_id=project_id,
                    query=arguments.get("query", ""),
                    kind=arguments.get("kind"),
                    component_id=arguments.get("component_id"),
                    top_k=int(arguments.get("top_k", 20)),
                    scope=arguments.get("scope", "all"),
                    path_prefix=arguments.get("path_prefix"),
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
                    transitive=bool(arguments.get("transitive", False)),
                    max_depth=int(arguments.get("max_depth", 3)),
                )
            elif name == "find_callees":
                result = await find_callees(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                    limit=int(arguments.get("limit", 100)),
                    transitive=bool(arguments.get("transitive", False)),
                    max_depth=int(arguments.get("max_depth", 3)),
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
            elif name == "get_data_access":
                result = await get_data_access(
                    db,
                    project_id=project_id,
                    symbol_id=arguments["symbol_id"],
                    limit=arguments.get("limit", 200),
                )
            elif name == "read_file":
                from app.mcp.file_read import read_project_file

                result = await read_project_file(
                    project_id=project_id,
                    file_path=arguments["file_path"],
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
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
            elif name == "list_flows":
                result = await list_flows(
                    db,
                    project_id=project_id,
                    limit=int(arguments.get("limit", 50)),
                )
            elif name == "find_runtime_path":
                result = await find_runtime_path(
                    db,
                    project_id=project_id,
                    entry_contract_id=arguments["entry_contract_id"],
                    max_depth=int(arguments.get("max_depth", 6)),
                    time_window=arguments.get("time_window"),
                )
            elif name == "compare_runs":
                result = await compare_runs(
                    db,
                    project_id=project_id,
                    run_a_id=uuid.UUID(arguments["run_a_id"]),
                    run_b_id=uuid.UUID(arguments["run_b_id"]),
                    limit=int(arguments.get("limit", 40)),
                )
            elif name == "submit_plan":
                result = await submit_plan_tool(
                    db,
                    project_id=project_id,
                    spec=arguments["spec"],
                    tasks=arguments["tasks"],
                    target_component_id=arguments["target_component_id"],
                    requester=arguments["requester"],
                )
            elif name == "edit_file_in_worktree":
                result = await edit_file_in_worktree(
                    db,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    file_path=arguments["file_path"],
                    edits=arguments["edits"],
                )
            elif name == "run_in_sandbox":
                result = await run_in_sandbox_tool(
                    db,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    command=arguments["command"],
                    timeout_sec=int(arguments.get("timeout_sec", 300)),
                )
            elif name == "submit_diff":
                result = await submit_diff_tool(
                    db,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    task_id=arguments["task_id"],
                    diff=arguments.get("diff"),
                    test_results=arguments.get("test_results"),
                    self_review_notes=arguments.get("self_review_notes"),
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

        return [TextContent(type="text", text=_cap_response(result))]

    return server


# Spec §11.7 — every tool response must be capped so a god-object's
# 1000-row caller list doesn't blow the agent's context window. The
# old json.dumps was unconditional and uncapped.
_MAX_RESPONSE_BYTES = 50 * 1024


def _cap_response(result, max_bytes: int = _MAX_RESPONSE_BYTES) -> str:
    """Serialise ``result`` to JSON; if too large, halve the largest
    list field until it fits and append ``response_truncated`` markers
    so the agent knows what to ask for next.

    Single-pass on the original ``result`` so ``truncated_total``
    always reports the real input size (not the post-halve size from a
    naive recursion)."""
    import json

    text = json.dumps(result, default=str)
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    if isinstance(result, dict):
        biggest_key, biggest_len = None, 0
        for k, v in result.items():
            if isinstance(v, list) and len(v) > biggest_len:
                biggest_key, biggest_len = k, len(v)
        if biggest_key:
            keep = biggest_len
            while keep > 0:
                keep //= 2
                shrunk = {
                    **result,
                    biggest_key: result[biggest_key][:keep],
                    "response_truncated": True,
                    "truncated_field": biggest_key,
                    "truncated_kept": keep,
                    "truncated_total": biggest_len,
                }
                txt2 = json.dumps(shrunk, default=str)
                if len(txt2.encode("utf-8")) <= max_bytes:
                    return txt2
            return json.dumps({
                biggest_key: [],
                "response_truncated": True,
                "truncated_field": biggest_key,
                "truncated_kept": 0,
                "truncated_total": biggest_len,
            })

    if isinstance(result, list):
        total = len(result)
        keep = total
        while keep > 0:
            keep //= 2
            txt2 = json.dumps(
                {
                    "items": result[:keep],
                    "response_truncated": True,
                    "truncated_kept": keep,
                    "truncated_total": total,
                },
                default=str,
            )
            if len(txt2.encode("utf-8")) <= max_bytes:
                return txt2

    return json.dumps({
        "response_truncated": True,
        "reason": "scalar_exceeds_cap",
    })


def _require_mcp_token() -> None:
    """Fail-closed startup gate (spec §11.6).

    A bare ``ggoss-mcp --project <uuid>`` over stdio used to grant any
    process that could spawn the binary full read of that project,
    including data tools. The server now requires
    ``MNEMOS_MCP_TOKEN`` to be set in the environment — the platform
    sets it when it intentionally launches the MCP server; an
    operator running it by hand sets it themselves. An unset env is a
    misconfiguration and the binary refuses to start.
    """
    import os
    import sys

    if not os.environ.get("MNEMOS_MCP_TOKEN"):
        sys.stderr.write(
            "{\"level\":\"error\",\"message\":\"MNEMOS_MCP_TOKEN_required\","
            "\"recoverable\":false}\n"
        )
        sys.exit(2)


async def _run(project_id: uuid.UUID) -> None:
    server = build_server(project_id)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    _require_mcp_token()
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project UUID")
    args = parser.parse_args()
    asyncio.run(_run(uuid.UUID(args.project)))


if __name__ == "__main__":
    main()
