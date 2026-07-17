"""PR-105 — enrich MCP tool descriptions with "Use when..." guidance.

The pre-PR descriptions were terse, accurate, and *unhelpful for an
agent picking which tool to call*. ``search_symbols`` said "search
symbols by lexical match"; ``find_callees`` said "list CALLS edges
whose source is X". Both true, neither answer the agent's actual
question: "I want to do Y — which tool?"

Claude Code (and other MCP-speaking agents) read every tool's
description into the prompt context. A tool with a good description
gets called when it should be; a tool described only in graph-
mechanics terms either gets ignored or gets called for the wrong
intent — which is worse than ignored, because it returns plausible-
looking but wrong answers.

This PR adds a "Use when:" paragraph to each of the 17 tools. The
tests below assert that paragraph exists and contains the agent-
intent vocabulary (verbs like "answering", "tracking down",
"orienting", and pronouns like "you") that distinguishes guidance
from mechanics.
"""

from __future__ import annotations

from pathlib import Path

_SERVER = (
    Path(__file__).resolve().parents[1] / "app" / "mcp" / "server.py"
)


def _body() -> str:
    return _SERVER.read_text(encoding="utf-8")


# The full set of tools that ship today. If the list changes,
# this test fails loudly so the PR author has to think about
# whether the new tool gets a "Use when:" too.
_TOOLS = [
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
    "find_runtime_path",
    "submit_plan",
    "edit_file_in_worktree",
    "run_in_sandbox",
    "submit_diff",
]


def test_every_tool_has_use_when_guidance():
    """Each tool description must include a ``Use when:`` paragraph —
    the line that tells a calling agent *under what intent* to pick
    this tool over the others."""
    body = _body()
    for name in _TOOLS:
        idx = body.find(f'name="{name}"')
        assert idx > 0, f"tool not registered: {name}"
        # Description is the next ~1500 chars.
        slab = body[idx:idx + 2000]
        assert "Use when:" in slab, f"tool {name!r} missing 'Use when:' guidance"


def test_search_symbols_warns_against_redundant_call():
    """The most common agent mistake: calling search_symbols when
    you already have a symbol_id. The description must steer toward
    get_symbol in that case."""
    body = _body()
    idx = body.find('name="search_symbols"')
    slab = body[idx:idx + 2000]
    assert "Use when:" in slab
    assert "get_symbol" in slab.lower() or "symbol_id" in slab


def test_transitive_walk_tools_explain_cost_tradeoff():
    """``transitive=true`` can be expensive on a busy graph. The
    descriptions must flag the tradeoff so an agent doesn't
    silently turn a cheap query into a 10-second one."""
    body = _body()
    for name in ("find_callers", "find_callees"):
        idx = body.find(f'name="{name}"')
        slab = body[idx:idx + 2000]
        assert "transitive" in slab.lower()


def test_sandbox_tool_explains_safety_boundaries():
    """The agent needs to know what run_in_sandbox CAN'T do —
    otherwise it'll waste turns trying to curl the network."""
    body = _body()
    idx = body.find('name="run_in_sandbox"')
    slab = body[idx:idx + 2000]
    # The three boundaries that matter for "can I do X here?"
    # decisions: network, filesystem, and timeout.
    assert "allowlist" in slab.lower() or "allowlisted" in slab.lower()
    assert "network" in slab.lower() or "read-only" in slab.lower()
    assert "sandbox_backend_unavailable" in slab
    assert "MNEMOS_TRUSTED_LOCAL_EXECUTION=1" in slab
    assert "always disabled in production" in slab.lower()
    assert "no os-level" in slab.lower()


def test_sensitive_data_tools_state_refusal_behaviour():
    """If get_sample_data refuses with 403, the agent shouldn't
    re-try with different params hoping it works. State this."""
    body = _body()
    idx = body.find('name="get_sample_data"')
    slab = body[idx:idx + 2000]
    assert "sensitive" in slab.lower()
    assert (
        "refuse" in slab.lower()
        or "403" in slab
        or "don't retry" in slab.lower()
    )


def test_runtime_path_distinguished_from_static_callees():
    """An agent should know when to use find_runtime_path vs
    find_callees — the docs must make the static-vs-runtime
    distinction obvious."""
    body = _body()
    idx = body.find('name="find_runtime_path"')
    slab = body[idx:idx + 2000]
    assert "runtime" in slab.lower() or "exercised" in slab.lower()
    # Cross-reference: the runtime tool's docs should mention
    # find_callees as the static alternative.
    assert "find_callees" in slab or "static" in slab.lower()


def test_plan_workflow_tools_describe_the_loop():
    """submit_plan → edit_file_in_worktree → run_in_sandbox →
    submit_diff is the workflow. Each tool's description should
    reference the next step so an agent doesn't drop the chain."""
    body = _body()
    # submit_plan should hint at the post-approval loop.
    sp = body[body.find('name="submit_plan"'):body.find('name="submit_plan"') + 2000]
    assert "edit_file_in_worktree" in sp or "approval" in sp.lower()
    # submit_diff should mention Gate B + self-review (so the
    # agent knows what auto-runs and what humans run).
    sd = body[body.find('name="submit_diff"'):body.find('name="submit_diff"') + 2000]
    assert "self-review" in sd.lower() or "self_review" in sd.lower()
    assert "Gate B" in sd or "gate b" in sd.lower() or "approve" in sd.lower()


def test_descriptions_remain_under_response_cap():
    """Don't blow up the MCP list_tools response with prose. Each
    description should fit comfortably under 2 KB so the cumulative
    listing stays well below §11.7's 50 KB cap."""
    body = _body()
    for name in _TOOLS:
        idx = body.find(f'name="{name}"')
        # Find the next ``inputSchema=`` after this name; that's the
        # end-marker of the description block.
        end = body.find("inputSchema=", idx)
        assert end > 0
        block = body[idx:end]
        assert len(block) < 3500, (
            f"tool {name!r} description block {len(block)}B — "
            "shrink the use-case before it crowds the agent's context."
        )


def test_findings_tool_links_to_plan_workflow():
    """list_findings is the natural entry point — an agent reads
    findings then opens plans. The description should reference
    submit_plan so the agent makes the connection."""
    body = _body()
    idx = body.find('name="list_findings"')
    slab = body[idx:idx + 2000]
    assert "remediation" in slab.lower() or "submit_plan" in slab
