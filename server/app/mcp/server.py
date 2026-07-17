"""MCP stdio server.

Runs via ``python -m app.mcp.server --project <uuid>``. Implements the
Phase-1 Week-3 query tools: search_symbols, get_symbol, find_callers
(spec §11.3). Additional tools land in Weeks 4-8 alongside their backing
data.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal

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
from app.graph_publication import (
    GraphGenerationChanged,
    GraphHeadMissing,
    GraphHeadNeedsRebuild,
    GraphPublicationInvariantError,
    GraphReadStamp,
    read_graph_stamp,
    revalidate_graph_stamp,
)
from app.mcp.data_queries import (
    get_column_stats,
    get_data_entity,
    get_sample_data,
    search_data,
)
from app.mcp.auth import (
    MCP_AUTHENTICATION_INVALID_CODE as MCP_AUTHENTICATION_INVALID_CODE,
    MCPAuthContext,
    MCPAuthenticationError,
    mcp_authentication_error,
    resolve_mcp_auth_context,
    revalidate_mcp_auth_context,
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
from app.safety.tokens import hash_token

GRAPH_SNAPSHOT_UNAVAILABLE_CODE = "graph_snapshot_unavailable"
GRAPH_SNAPSHOT_CHANGED_CODE = "graph_snapshot_changed_retry"
MCP_TOOL_INTERNAL_FAILURE_CODE = "mcp_tool_internal_failure"

log = logging.getLogger(__name__)

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
                "top_k": {"type": "integer", "default": 10},
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
            "Read a bounded file window from the immutable Git commit used by "
            "a completed analysis run. The default is the latest completed run; "
            "pass run_id to cite a specific historical run. Source bytes come "
            "from the project mirror configured by SOURCE_MIRROR_ROOT via git "
            "show, never from a mutable checkout.\n\n"
            "Use when: a graph query (search_symbols / get_symbol) gave "
            "you a file_path + line_range and you need the actual code. "
            "Pass a project-relative path and a narrow line range. Absolute "
            "paths and parent traversal are rejected. The response reports "
            "run_id, resolved revision, path, range, and truncation. Legacy "
            "manual/non-Git runs return an explicit provenance-unavailable "
            "error instead of silently reading drifted source."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "run_id": {"type": "string", "nullable": True},
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
            "deltas (CALLS/EXPOSES/READS/WRITES added & removed), plus bounded "
            "caller impact. Only terminal runs with a valid atomic publication "
            "receipt are accepted, and certainty is preserved per change. "
            "Finding deltas are explicitly unavailable until findings have "
            "immutable per-publication history.\n\n"
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
            "The worktree is pinned to the exact published source commit "
            "at submission; until approval no file edits land. After "
            "approval, edit_file_in_worktree + "
            "run_in_sandbox + submit_diff is the loop."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "spec": {"type": "object"},
                "tasks": {"type": "array"},
                "target_component_id": {"type": "string"},
            },
            "required": ["spec", "tasks", "target_component_id"],
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
            "Request a bounded allowlisted local verification command in the "
            "Plan worktree.\n\n"
            "Use when: verifying a change you just made with "
            "edit_file_in_worktree — running tests (\"pytest -k\"), a "
            "linter, or a build. The local argv backend is fail-disabled by "
            "default and always disabled in production. Unless a trusted-repo "
            "developer explicitly sets MNEMOS_TRUSTED_LOCAL_EXECUTION=1 in "
            "a local, development, or test environment, the tool returns the "
            "static failure code sandbox_backend_unavailable without spawning "
            "a process. Production cannot enable this backend with that flag. "
            "When explicitly enabled, the command is parsed into validated "
            "argv without a shell, receives a scrubbed environment, and is "
            "rejected if it contains shell syntax or an escaping path. "
            "Network-oriented and dependency-install commands are not "
            "authorized. The enabled local executor provides no OS-level "
            "filesystem or network containment, so it is only for repositories "
            "the developer already trusts. "
            "Timeout and byte caps bound execution; returned output is "
            "redacted. This is not a general-purpose shell."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string"},
                "command": {"type": "string", "minLength": 1, "maxLength": 4096},
                "timeout_sec": {
                    "type": "integer",
                    "default": 300,
                    "minimum": 5,
                    "maximum": 1800,
                },
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

_KNOWN_TOOL_NAMES = frozenset(tool.name for tool in _TOOLS)
_MUTATING_TOOL_NAMES = frozenset(
    {
        "submit_plan",
        "edit_file_in_worktree",
        "run_in_sandbox",
        "submit_diff",
    }
)
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "get_project_index",
        "get_task_context_pack",
        "search_symbols",
        "get_symbol",
        "find_callers",
        "find_callees",
        "impact_analysis",
        "get_contract",
        "get_data_access",
        "read_file",
        "get_data_entity",
        "get_sample_data",
        "get_column_stats",
        "search_data",
        "list_findings",
        "get_module_summary",
        "list_flows",
        "find_runtime_path",
        "compare_runs",
    }
)
# Existing tools must be explicitly classified.  Adding a future data refresh
# or write tool requires placing it in the mutation set before it is exposed.
assert _KNOWN_TOOL_NAMES == _READ_ONLY_TOOL_NAMES | _MUTATING_TOOL_NAMES
_REVISION_LOCKED_DEV_MUTATIONS = _MUTATING_TOOL_NAMES

MCP_AUTHORIZATION_DENIED_CODE = "mcp_authorization_denied"
MCP_PROJECT_MISMATCH_CODE = "mcp_project_mismatch"


def _can_call_tool(context: MCPAuthContext, name: str) -> bool:
    if name in _READ_ONLY_TOOL_NAMES:
        return context.role in {"viewer", "operator", "admin"}
    if name in _MUTATING_TOOL_NAMES:
        return context.role in {"operator", "admin"}
    return False


def _authorization_error() -> dict:
    return {
        "schema": "mnemos.error.v1",
        "error": MCP_AUTHORIZATION_DENIED_CODE,
        "retryable": False,
    }


def _project_argument_matches(
    context: MCPAuthContext,
    arguments: dict,
) -> bool:
    supplied = arguments.get("project_id")
    if supplied is None:
        return True
    try:
        return uuid.UUID(str(supplied)) == context.project_id
    except (TypeError, ValueError, AttributeError):
        return False


def _requires_published_graph(name: str, arguments: dict) -> bool:
    """Whether a tool would consume or act on the mutable current graph."""

    if name not in _KNOWN_TOOL_NAMES or name == "get_project_index":
        return False
    # A caller that pins read_file to a completed run knowingly requests one
    # historical source generation and does not consume the current graph.
    return not (name == "read_file" and bool(arguments.get("run_id")))


def _graph_snapshot_unavailable_error(exc: Exception) -> dict:
    return {
        "schema": "mnemos.error.v1",
        "error": GRAPH_SNAPSHOT_UNAVAILABLE_CODE,
        "reason": GRAPH_SNAPSHOT_UNAVAILABLE_CODE,
        "repair": {
            "required": True,
            "scope": "full",
            "action": "publish one successful staged full source analysis",
        },
    }


def _graph_snapshot_changed_error(
    stamp: GraphReadStamp, exc: GraphGenerationChanged
) -> dict:
    return {
        "schema": "mnemos.error.v1",
        "error": GRAPH_SNAPSHOT_CHANGED_CODE,
        "reason": GRAPH_SNAPSHOT_CHANGED_CODE,
        "retryable": True,
        "snapshot": {
            "generation": stamp.generation,
            "overlay_generation": stamp.overlay_generation,
            "run_id": str(stamp.current_run_id),
            "published_at": stamp.published_at.isoformat(),
        },
    }


def _audit_value_shape(value: object) -> dict[str, int | str]:
    """Describe an MCP argument without retaining any caller-owned content."""

    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) is int:
        return {"type": "integer"}
    if type(value) is float:
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string", "chars": len(value)}
    if isinstance(value, list):
        return {"type": "array", "items": len(value)}
    if isinstance(value, dict):
        return {"type": "object", "fields": len(value)}
    return {"type": "unsupported"}


def _tool_argument_names(name: str) -> frozenset[str]:
    for tool in _TOOLS:
        if tool.name != name:
            continue
        properties = tool.inputSchema.get("properties", {})
        if not isinstance(properties, dict):
            return frozenset()
        return frozenset(key for key in properties if isinstance(key, str))
    return frozenset()


def _mcp_audit_details(
    name: str,
    arguments: dict,
    *,
    blocked_by: str | None = None,
) -> dict[str, object]:
    """Return the canonical shape-only audit envelope for every MCP tool."""

    recognized = _tool_argument_names(name)
    shapes = {
        key: _audit_value_shape(arguments[key])
        for key in sorted(recognized.intersection(arguments))
    }
    details: dict[str, object] = {
        "schema": "mnemos.mcp_audit.v1",
        "argument_count": len(arguments),
        "recognized_argument_count": len(shapes),
        "unknown_argument_count": len(set(arguments).difference(recognized)),
        "argument_shapes": shapes,
    }
    if blocked_by is not None:
        details["blocked_by"] = blocked_by
    return details


def _sandbox_audit_details(arguments: dict) -> dict[str, object]:
    """Compatibility wrapper using the global shape-only MCP audit contract."""

    return _mcp_audit_details("run_in_sandbox", arguments)


def build_server(auth_context: MCPAuthContext) -> Server:
    project_id = auth_context.project_id
    server: Server = Server("mnemos")

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        async with SessionLocal() as db:
            try:
                await revalidate_mcp_auth_context(
                    db,
                    context=auth_context,
                )
            except MCPAuthenticationError:
                return []
        return [
            tool for tool in _TOOLS if _can_call_tool(auth_context, tool.name)
        ]

    async def _call_tool_impl(name: str, arguments: dict) -> list[TextContent]:
        async with SessionLocal() as db:
            try:
                await revalidate_mcp_auth_context(
                    db,
                    context=auth_context,
                    lock=name in _MUTATING_TOOL_NAMES,
                )
            except MCPAuthenticationError:
                return [
                    TextContent(
                        type="text",
                        text=_cap_response(mcp_authentication_error()),
                    )
                ]
            if not _can_call_tool(auth_context, name):
                return [
                    TextContent(
                        type="text",
                        text=_cap_response(_authorization_error()),
                    )
                ]
            if not _project_argument_matches(auth_context, arguments):
                return [
                    TextContent(
                        type="text",
                        text=_cap_response(
                            {
                                "schema": "mnemos.error.v1",
                                "error": MCP_PROJECT_MISMATCH_CODE,
                                "retryable": False,
                            }
                        ),
                    )
                ]
            stamp: GraphReadStamp | None = None
            # ``READ COMMITTED`` can cross a publication between statements.
            # Pin before dispatch and revalidate after the final graph query;
            # changed source or overlay revision discards the result instead
            # of returning a mixed tool payload. The project index and an
            # explicitly pinned historical file remain available for
            # diagnosis/recovery.
            if _requires_published_graph(name, arguments):
                try:
                    stamp = await read_graph_stamp(db, project_id=project_id)
                except (
                    GraphHeadMissing,
                    GraphHeadNeedsRebuild,
                    GraphPublicationInvariantError,
                ) as exc:
                    result = _graph_snapshot_unavailable_error(exc)
                    await audit_record(
                        actor=auth_context.actor,
                        action=f"mcp.tool.{name}",
                        target=str(project_id),
                        project_id=project_id,
                        details=_mcp_audit_details(
                            name,
                            arguments,
                            blocked_by=GRAPH_SNAPSHOT_UNAVAILABLE_CODE,
                        ),
                    )
                    return [TextContent(type="text", text=_cap_response(result))]
            if name == "get_project_index":
                result = await build_project_index(
                    db,
                    project_id=project_id,
                    top_k=int(arguments.get("top_k", 10)),
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
                    db,
                    project_id=project_id,
                    file_path=arguments["file_path"],
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
                    run_id=arguments.get("run_id"),
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
                    auth_context=auth_context,
                    project_id=project_id,
                    spec=arguments["spec"],
                    tasks=arguments["tasks"],
                    target_component_id=arguments["target_component_id"],
                    requester=auth_context.actor,
                )
            elif name == "edit_file_in_worktree":
                result = await edit_file_in_worktree(
                    db,
                    auth_context=auth_context,
                    expected_project_id=project_id,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    file_path=arguments["file_path"],
                    edits=arguments["edits"],
                )
            elif name == "run_in_sandbox":
                result = await run_in_sandbox_tool(
                    db,
                    auth_context=auth_context,
                    expected_project_id=project_id,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    command=arguments["command"],
                    timeout_sec=int(arguments.get("timeout_sec", 300)),
                )
            elif name == "submit_diff":
                result = await submit_diff_tool(
                    db,
                    auth_context=auth_context,
                    expected_project_id=project_id,
                    plan_id=uuid.UUID(arguments["plan_id"]),
                    task_id=arguments["task_id"],
                    diff=arguments.get("diff"),
                    test_results=arguments.get("test_results"),
                    self_review_notes=arguments.get("self_review_notes"),
                )
            else:
                result = {"error": "unknown_tool"}
            # Dev mutations lock GraphHead -> AnalysisRun -> Plan internally
            # and hold that boundary through their commit/external action.
            # Replacing an already-committed success with a generic retryable
            # read error would make an MCP client repeat the side effect.
            if stamp is not None and name not in _REVISION_LOCKED_DEV_MUTATIONS:
                try:
                    await revalidate_graph_stamp(db, stamp=stamp)
                except GraphGenerationChanged as exc:
                    result = _graph_snapshot_changed_error(stamp, exc)
            await audit_record(
                actor=auth_context.actor,
                action=f"mcp.tool.{name}",
                target=str(project_id),
                project_id=project_id,
                details=_mcp_audit_details(name, arguments),
            )

        return [TextContent(type="text", text=_cap_response(result))]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[TextContent]:
        try:
            return await _call_tool_impl(name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — MCP must emit only the static envelope
            log.error(
                "MCP tool call failed failure_code=%s",
                MCP_TOOL_INTERNAL_FAILURE_CODE,
            )
            return [
                TextContent(
                    type="text",
                    text=_cap_response(
                        {
                            "schema": "mnemos.error.v1",
                            "error": MCP_TOOL_INTERNAL_FAILURE_CODE,
                            "retryable": False,
                        }
                    ),
                )
            ]

    return server


# Spec §11.7 — every tool response must be capped so a god-object's
# 1000-row caller list doesn't blow the agent's context window. The
# old json.dumps was unconditional and uncapped.
_MAX_RESPONSE_BYTES = 50 * 1024


def _mcp_json_default(value: object) -> str:
    """Normalize only documented scalar dialects; never stringify objects."""

    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("unsupported_mcp_response_value")


def _serialize_mcp_response(value: object) -> str | None:
    try:
        return json.dumps(
            value,
            default=_mcp_json_default,
            allow_nan=False,
        )
    except Exception:  # noqa: BLE001 — hostile __iter__/mapping implementations
        return None


def _cap_response(result, max_bytes: int = _MAX_RESPONSE_BYTES) -> str:
    """Serialise ``result`` to JSON; if too large, halve the largest
    list field until it fits and append ``response_truncated`` markers
    so the agent knows what to ask for next.

    Single-pass on the original ``result`` so ``truncated_total``
    always reports the real input size (not the post-halve size from a
    naive recursion)."""
    text = _serialize_mcp_response(result)
    if text is None:
        text = json.dumps(
            {
                "schema": "mnemos.error.v1",
                "error": "mcp_response_contract_invalid",
                "retryable": False,
            }
        )
        if len(text.encode("utf-8")) <= max_bytes:
            return text
        return "{}" if max_bytes >= 2 else ""
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
                txt2 = _serialize_mcp_response(shrunk)
                if txt2 is not None and len(txt2.encode("utf-8")) <= max_bytes:
                    return txt2
            empty = json.dumps({
                biggest_key: [],
                "response_truncated": True,
                "truncated_field": biggest_key,
                "truncated_kept": 0,
                "truncated_total": biggest_len,
            })
            if len(empty.encode("utf-8")) <= max_bytes:
                return empty

    if isinstance(result, list):
        total = len(result)
        keep = total
        while keep > 0:
            keep //= 2
            txt2 = _serialize_mcp_response(
                {
                    "items": result[:keep],
                    "response_truncated": True,
                    "truncated_kept": keep,
                    "truncated_total": total,
                }
            )
            if txt2 is not None and len(txt2.encode("utf-8")) <= max_bytes:
                return txt2

    fallback = json.dumps({
        "response_truncated": True,
        "reason": "scalar_exceeds_cap",
    })
    if len(fallback.encode("utf-8")) <= max_bytes:
        return fallback
    # Production uses 50 KiB, so this branch exists only for a caller passing
    # an artificially tiny cap. Preserve the hard byte invariant even then.
    return "{}" if max_bytes >= 2 else ""


def _require_mcp_token() -> str:
    """Fail-closed startup gate (spec §11.6).

    A bare ``ggoss-mcp --project <uuid>`` over stdio used to grant any
    process that could spawn the binary full read of that project,
    including data tools. Stdio receives ``MNEMOS_MCP_TOKEN`` through
    the environment, hashes it immediately, and ``_run`` resolves that
    digest to an active project-scoped API key. An unset or unbound
    credential is a misconfiguration and the binary refuses to start.
    """
    # Read-once: remove the bearer from the process environment immediately so
    # later subprocesses, diagnostics, or libraries cannot inherit it.
    raw_token = os.environ.pop("MNEMOS_MCP_TOKEN", None)
    if not raw_token:
        sys.stderr.write(
            "{\"level\":\"error\",\"message\":\"MNEMOS_MCP_TOKEN_required\","
            "\"recoverable\":false}\n"
        )
        sys.exit(2)
    token_hash = hash_token(raw_token)
    del raw_token
    return token_hash


async def _run(project_id: uuid.UUID, *, token_hash: str) -> None:
    try:
        async with SessionLocal() as db:
            auth_context = await resolve_mcp_auth_context(
                db,
                token_hash=token_hash,
                expected_project_id=project_id,
            )
    except Exception:  # noqa: BLE001 - startup must fail closed on DB/auth faults
        # Do not distinguish random/revoked/cross-project credentials and never
        # include the raw token or digest in stderr.
        sys.stderr.write(
            "{\"level\":\"error\",\"message\":\"MNEMOS_MCP_AUTH_invalid\","
            "\"recoverable\":false}\n"
        )
        raise SystemExit(2) from None

    server = build_server(auth_context)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main() -> None:
    token_hash = _require_mcp_token()
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True, help="project UUID")
    args = parser.parse_args()
    asyncio.run(_run(uuid.UUID(args.project), token_hash=token_hash))


if __name__ == "__main__":
    main()
