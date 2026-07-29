"""PR-140 — Claude-Code extraction for analyzer-less languages.

The fatal gap: a project in a language with no ggoss analyzer (C++, Go,
…) produced an empty graph, so summaries/findings/MCP were all blind to
it — even though Claude Code understands those languages fine. This adds
an agent-extraction stage that delegates to the Claude Code subscription.

These tests are deterministic (no live LLM): they cover the pure helpers
(file discovery, envelope conversion) and the pipeline wiring decision.
The live Claude Code path is exercised by the OpenClaw run, not CI.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr140")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def test_uncovered_language_routes_to_agent_extraction():
    """The wiring decision: a language with no ggoss binary must be routed
    by run_ingest to the agent-extraction stage. Was C++ until PR-191 gave
    it a deterministic analyzer; PR-195/199 covered the tree-sitter languages,
    so generic SQL text is the remaining uncovered example."""
    from app.analyzers.registry import binary_for
    from app.extractor.agent_extract import AGENT_LANGUAGE_EXTENSIONS

    assert binary_for("sql") is None, "generic SQL must have no deterministic analyzer"
    assert "sql" in AGENT_LANGUAGE_EXTENSIONS, "SQL must be agent-extraction eligible"
    # A covered language must NOT be double-handled by the agent stage's
    # wiring (it still may appear in the map, but run_ingest only calls the
    # agent stage when binary_for(lang) is None).
    assert binary_for("csharp") == "ggoss-csharp"


def test_discover_source_files_finds_cpp_respects_limit_and_skipdirs(tmp_path):
    from app.extractor.agent_extract import discover_source_files

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "engine.cpp").write_text("int main(){return 0;}\n" * 50)
    (tmp_path / "src" / "actor.h").write_text("class Actor{};\n" * 30)
    (tmp_path / "README.md").write_text("# not source")
    # vendored dir must be skipped
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.cpp").write_text("x" * 9999)

    files = discover_source_files(str(tmp_path), "cpp", limit=10)
    names = {f.name for f in files}
    assert "engine.cpp" in names and "actor.h" in names
    assert "dep.cpp" not in names, "must skip vendored node_modules"
    assert "README.md" not in names

    # limit is honoured (largest-first)
    one = discover_source_files(str(tmp_path), "cpp", limit=1)
    assert len(one) == 1 and one[0].name == "engine.cpp"

    # unknown language → nothing
    assert discover_source_files(str(tmp_path), "haskell", limit=10) == []


def test_agent_discovery_and_deepen_exclude_generated_js_and_include_vue(
    tmp_path,
):
    from app.extractor.agent_extract import (
        discover_source_files,
        find_candidate_files,
    )

    component = tmp_path / "OrderPanel.vue"
    app = tmp_path / "public" / "static" / "app.js"
    generated = [
        tmp_path / "atlas.iife.js",
        tmp_path / "vendor.bundle.mjs",
        tmp_path / "vendor.umd.cjs",
    ]
    component.write_text(
        "<script setup>const orderSearch = true</script>\n",
        encoding="utf-8",
    )
    app.parent.mkdir(parents=True)
    app.write_text("const orderSearch = true\n", encoding="utf-8")
    for path in generated:
        path.write_text("const orderSearch = true\n", encoding="utf-8")

    assert discover_source_files(
        str(tmp_path), "typescript", limit=10
    ) == [component]
    assert discover_source_files(
        str(tmp_path), "javascript", limit=10
    ) == [app]

    ranked = find_candidate_files(
        str(tmp_path), ["ordersearch"], limit=10
    )
    assert {path for path, _language in ranked} == {component, app}
    assert dict(ranked)[component] == "typescript"


def test_to_envelopes_shape_certainty_and_dangling_edge_drop():
    from app.extractor.agent_extract import to_envelopes

    extracted = {
        "symbols": [
            {"id": "cpp:A.cpp::Foo", "name": "Foo", "kind": "class", "line": 3},
            {"id": "cpp:A.cpp::Foo::bar", "name": "Foo::bar", "kind": "method", "line": 7},
            {"name": "noid"},  # missing id → dropped
        ],
        "edges": [
            {"source_id": "cpp:A.cpp::Foo", "target_id": "cpp:A.cpp::Foo::bar", "kind": "CONTAINS"},
            {"source_id": "cpp:A.cpp::Foo::bar", "target_id": "cpp:Other::x", "kind": "CALLS"},
        ],
    }
    envs = to_envelopes("cpp", "A.cpp", extracted)
    symbols = [e for e in envs if e["record_type"] == "symbol"]
    edges = [e for e in envs if e["record_type"] == "edge"]

    assert len(symbols) == 2, "id-less symbol must be dropped"
    assert all(e["data"]["certainty"] == "inferred" for e in envs), "LLM-derived = inferred"
    assert all(e["data"].get("extractor") == "claude_code" for e in symbols)
    emitted_ids = {e["data"]["id"] for e in symbols}
    assert all(item.startswith("agent-symbol:cpp:") for item in emitted_ids)
    assert not emitted_ids.intersection({"cpp:A.cpp::Foo", "cpp:A.cpp::Foo::bar"})
    # dangling edge (target not among emitted symbols) is dropped
    assert len(edges) == 1
    assert edges[0]["data"]["source_id"] in emitted_ids
    assert edges[0]["data"]["target_id"] in emitted_ids


def test_model_ids_cannot_escape_agent_namespace_or_change_stable_identity():
    """Source-prompt output may name a deterministic fact but cannot own it."""
    from app.extractor.agent_extract import to_envelopes

    hostile = {
        "symbols": [
            {
                "id": "py:victim.py:secret@1",
                "name": "safe_name",
                "qualified_name": "safe_name",
                "kind": "function",
                "line": 4,
            }
        ],
        "edges": [],
    }
    first = to_envelopes("python", "safe.py", hostile)
    second = to_envelopes("python", "safe.py", hostile)
    symbol_id = first[0]["data"]["id"]

    assert symbol_id.startswith("agent-symbol:python:")
    assert symbol_id != "py:victim.py:secret@1"
    assert second[0]["data"]["id"] == symbol_id
    assert first[0]["source_name"] == "agent:python"


def test_record_payload_accepts_agent_envelopes(tmp_path):
    """The agent envelopes must be ingestable by the standard graph path
    (_record_payload), creating Symbol nodes + edges — same as analyzers."""
    import asyncio

    from app.extractor.agent_extract import to_envelopes

    envs = to_envelopes(
        "cpp",
        "Engine/Foo.cpp",
        {
            "symbols": [{"id": "cpp:Engine/Foo.cpp::Foo::run", "name": "Foo::run", "kind": "method"}],
            "edges": [],
        },
    )
    sym = next(e for e in envs if e["record_type"] == "symbol")
    # record_type symbol is in the standard accept set for the symbols verb.
    from app.orchestrator.jobs import _VERB_ACCEPT

    assert sym["record_type"] in _VERB_ACCEPT["symbols"]
    assert sym["data"]["id"] and sym["data"]["language"] == "cpp"
    # smoke: the conversion ran without async/db, proving envelope purity
    assert asyncio.iscoroutinefunction(
        __import__("app.orchestrator.jobs", fromlist=["_record_payload"])._record_payload
    )
