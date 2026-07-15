"""PR-190 — AI agent context artifacts.

The product target is not a human report. Mnemos must expose a persistent
analysis knowledge base plus small task packs Claude Code/Codex can request
before making edits. These tests pin the machine-readable output contract.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr190")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.artifacts.agent_context import (  # noqa: E402
    DEFAULT_AGENT_CONTEXT_MAX_BYTES,
    SCHEMA_PROJECT_INDEX,
    SCHEMA_TASK_PACK,
    _summary_ref,
    build_project_index,
    build_task_context_pack,
)
from app.artifacts.agents_md import build_agents_md  # noqa: E402
from app.extractor.agent_flow import (  # noqa: E402
    FLOW_RESULT_CONTRACT,
    build_flow_summary_content,
)
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.finding_identity import finding_identity_key  # noqa: E402
from app.models.findings import Finding, Summary  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.models.stages import AnalysisStage  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


def _flow_summary_sections(run_id: uuid.UUID) -> list[dict]:
    flow = {
        "contract": FLOW_RESULT_CONTRACT,
        "summary": "Create-order flow crosses API validation and persistence.",
        "detailed": "POST /orders validates the payload and writes orders.",
        "steps": [
            {
                "order": 1,
                "tier": "backend",
                "component": "orders.py",
                "action": "create order",
                "signal": {
                    "kind": "function_call",
                    "name": "create_order",
                    "fields": [],
                },
            }
        ],
        "flags": [],
        "data_touched": [],
        "open_questions": [],
    }
    content = build_flow_summary_content(
        flow,
        analysis_run_id=run_id,
        revision="a" * 40,
        source_scope={
            "provided_files": ["orders.py"],
            "files": [
                {
                    "label": "orders.py",
                    "tier": "backend",
                    "language": "python",
                    "provided_chars": 27,
                    "included_chars": 27,
                    "prompt_truncated": False,
                    "input_truncated": False,
                }
            ],
            "omitted_files": [],
            "included_source_chars": 27,
            "max_source_chars": 28_000,
            "truncated": False,
        },
    )
    return content.sections


@pytest.fixture()
async def seeded_session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as session:
        pid = uuid.uuid4()
        project = Project(
            id=pid,
            name="agent-context-demo",
            gitlab_project_id=9001,
            gitlab_url="https://demo.local/agent-context-demo",
            default_branch="main",
            languages=["python", "typescript"],
        )
        session.add(project)
        now = datetime.now(tz=timezone.utc)
        run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=pid,
                status="completed",
                triggered_by="test",
                git_sha="abc123",
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(
                    generation=1,
                    published_at=now,
                    stats={"symbols": 2, "edges": 3},
                ),
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=pid,
                current_run_id=run_id,
                generation=1,
                state="ready",
                published_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                Node(
                    id="sym:orders:create_order",
                    project_id=pid,
                    kind="Symbol",
                    data={
                        "name": "create_order",
                        "signature": (
                            "def create_order(repo, payload):\n"
                            "    leaked_body_call(payload)\n"
                            "    return repo.save(payload)"
                        ),
                        "location": {"file": "orders.py", "line": 10},
                        "content": "def leaked_raw_source(): pass\n" * 100,
                        "large_note": "x" * 700,
                    },
                    certainty="verified",
                    created_by=["ggoss-py"],
                ),
                Node(
                    id="sym:orders:validate_order",
                    project_id=pid,
                    kind="Symbol",
                    data={
                        "name": "validate_order",
                        "signature": "def validate_order(payload)",
                        "location": {"file": "orders.py", "line": 3},
                    },
                    certainty="verified",
                    created_by=["ggoss-py"],
                ),
                Node(
                    id="contract:POST /orders",
                    project_id=pid,
                    kind="Contract",
                    data={"method": "POST", "path": "/orders", "name": "POST /orders"},
                    certainty="verified",
                    created_by=["ggoss-py"],
                ),
                Node(
                    id="data:orders",
                    project_id=pid,
                    kind="DataEntity",
                    data={"name": "orders", "columns": ["id", "status"]},
                    certainty="verified",
                    created_by=["ggoss-py"],
                ),
            ]
        )
        edge_call = Edge(
            id=uuid.uuid4(),
            project_id=pid,
            source_id="sym:orders:create_order",
            target_id="sym:orders:validate_order",
            kind="CALLS",
            data={
                "invocation_site": {"file": "orders.py", "line": 12},
                "raw": "create_order(validate_order(payload))\n" * 100,
            },
            certainty="verified",
            created_by=["ggoss-py"],
        )
        edge_write = Edge(
            id=uuid.uuid4(),
            project_id=pid,
            source_id="sym:orders:create_order",
            target_id="data:orders",
            kind="WRITES",
            data={"access_site": {"file": "orders.py", "line": 14}},
            certainty="verified",
            created_by=["ggoss-py"],
        )
        edge_exposes = Edge(
            id=uuid.uuid4(),
            project_id=pid,
            source_id="sym:orders:create_order",
            target_id="contract:POST /orders",
            kind="EXPOSES",
            data={},
            certainty="verified",
            created_by=["ggoss-py"],
        )
        session.add_all([edge_call, edge_write, edge_exposes])
        finding = Finding(
            id=uuid.uuid4(),
            project_id=pid,
            kind="schema_mismatch",
            identity_key=finding_identity_key(
                kind="schema_mismatch",
                subject_node_id="sym:orders:create_order",
            ),
            severity="warning",
            status="open",
            subject_node_id="sym:orders:create_order",
            detail={
                "missing_target": "data:legacy_orders",
                "snippet": "repo.write_raw_legacy_order(payload)\n" * 100,
            },
            risk_score=61,
            remediation="Align the write target with the live schema.",
            first_seen_at=now,
            last_seen_at=now,
            validated_graph_generation=1,
            validated_overlay_generation=0,
            validated_at=now,
        )
        session.add(finding)
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=pid,
                target_id="sym:orders:create_order",
                level=1,
                validated_graph_generation=1,
                validated_overlay_generation=0,
                validated_at=now,
                summary="Creates an order after validation and writes the orders table.",
                model_used="test",
            )
        )
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=pid,
                target_id="flow:create-order",
                level=4,
                validated_graph_generation=1,
                validated_overlay_generation=0,
                validated_at=now,
                summary="Create-order flow crosses API validation and order persistence.",
                detailed="POST /orders invokes create_order, validates payload, and writes orders.",
                claims=_flow_summary_sections(run_id),
                open_questions=[],
                model_used="test",
            )
        )
        await session.commit()
        yield session, pid, finding.id
    await eng.dispose()


@pytest.mark.asyncio
async def test_project_index_is_bounded_agent_map(seeded_session):
    session, pid, finding_id = seeded_session
    index = await build_project_index(session, project_id=pid, top_k=2)

    assert index["schema"] == SCHEMA_PROJECT_INDEX
    assert index["project"]["name"] == "agent-context-demo"
    assert index["agent_contract"]["do_not_load_whole_repo"] is True
    assert index["agent_contract"]["source_of_truth"] == "mnemos_graph"
    assert index["agent_contract"]["role"] == "source_reference_and_analysis_guide"
    assert index["agent_contract"]["ai_summaries_are_optional_narration"] is True
    assert index["analysis_snapshot"]["node_counts"]["Symbol"] == 2
    assert index["analysis_snapshot"]["edge_counts"]["WRITES"] == 1
    assert index["analysis_snapshot"]["graph_queries_safe"] is True
    assert index["analysis_snapshot"]["diagnostic_only"] is False
    assert index["analysis_snapshot"]["unpublished_refresh"] is None
    assert "get_task_context_pack" in index["mcp_workflows"]["modify_symbol"]
    assert index["entrypoints"]["contracts"][0]["id"] == "contract:POST /orders"
    assert index["risk_queue"]["findings"][0]["finding_id"] == str(finding_id)
    assert index["analysis_entrypoints"]["known_flows"][0]["target_id"] == "flow:create-order"
    assert {
        hint["tool"] for hint in index["analysis_entrypoints"]["search_hints"]
    } >= {"search_symbols", "list_flows", "impact_analysis"}
    assert index["truncation"]["top_k"] == 2


@pytest.mark.asyncio
async def test_project_index_default_keeps_orientation_surface_small(
    seeded_session,
):
    session, pid, _finding_id = seeded_session

    index = await build_project_index(session, project_id=pid)

    assert index["truncation"]["top_k"] == 10


@pytest.mark.asyncio
async def test_failed_staged_refresh_keeps_published_index_readable(
    seeded_session,
):
    session, pid, _finding_id = seeded_session
    baseline = (
        await session.execute(
            select(AnalysisRun)
            .where(
                AnalysisRun.project_id == pid,
                AnalysisRun.status == "completed",
            )
        )
    ).scalar_one()
    failed_started = baseline.completed_at + timedelta(seconds=1)
    failed = AnalysisRun(
        id=uuid.uuid4(),
        project_id=pid,
        status="failed",
        triggered_by="test",
        git_sha="def456",
        scope="incremental",
        started_at=failed_started,
        completed_at=failed_started + timedelta(seconds=1),
        error_log="analyzer_failed",
        graph_base_generation=1,
    )
    # Staged runs never mutate current Node/Edge rows. A failed attempt and a
    # newer queued request therefore leave the published head readable.
    queued = AnalysisRun(
        id=uuid.uuid4(),
        project_id=pid,
        status="queued",
        triggered_by="test",
        git_sha="fedcba",
        scope="incremental",
    )
    session.add_all([failed, queued])
    await session.commit()

    index = await build_project_index(session, project_id=pid, top_k=2)

    snapshot = index["analysis_snapshot"]
    assert snapshot["snapshot_consistency"] == "refreshing"
    assert snapshot["graph_queries_safe"] is True
    assert snapshot["diagnostic_only"] is False
    assert snapshot["graph_publication"]["run_id"] == str(baseline.id)
    assert index["entrypoints"]["contracts"]


@pytest.mark.asyncio
async def test_task_context_pack_for_symbol_carries_evidence_and_next_queries(
    seeded_session,
):
    session, pid, _finding_id = seeded_session
    pack = await build_task_context_pack(
        session,
        project_id=pid,
        target_id="sym:orders:create_order",
        intent="validate order",
        budget_items=10,
    )

    assert pack["schema"] == SCHEMA_TASK_PACK
    assert pack["project"]["name"] == "agent-context-demo"
    assert pack["budget"]["raw_source_included"] is False
    assert pack["target_node"]["id"] == "sym:orders:create_order"
    assert pack["target_node"]["signature"] == "def create_order(repo, payload):"
    assert pack["summary_rollups"][0]["target_id"] == "sym:orders:create_order"
    assert pack["precomputed_mcp_context"]["symbol"]["neighbors"]["callees_count"] == 1
    assert (
        pack["precomputed_mcp_context"]["transitive_callees"]["edges"][0]["callee_id"]
        == "sym:orders:validate_order"
    )
    assert (
        pack["precomputed_mcp_context"]["data_access"]["writes"][0]["entity_id"]
        == "data:orders"
    )
    assert pack["precomputed_mcp_context"]["intent_symbol_matches"]
    assert pack["target_node"]["data"]["content"]["omitted"] == "raw_payload"
    assert pack["target_node"]["data"]["large_note"]["omitted"] == "large_string"
    assert pack["graph_slice"]["data_access"]["writes"][0]["target_id"] == "data:orders"
    assert pack["graph_slice"]["callees"][0]["data"]["raw"]["omitted"] == "raw_payload"
    assert pack["impact"]["affected_data_entities"] == ["data:orders"]
    assert pack["related_findings"][0]["kind"] == "schema_mismatch"
    assert any(q["tool"] == "impact_analysis" for q in pack["next_mcp_queries"])
    assert {ev["type"] for ev in pack["evidence_refs"]} == {"node", "edge"}
    serialized = json.dumps(pack)
    assert "leaked_raw_source" not in serialized
    assert "create_order(validate_order" not in serialized
    assert "leaked_body_call" not in serialized


@pytest.mark.asyncio
async def test_task_context_pack_for_finding_points_back_to_subject(seeded_session):
    session, pid, finding_id = seeded_session
    pack = await build_task_context_pack(
        session,
        project_id=pid,
        target_id=str(finding_id),
        target_kind="finding",
    )

    assert pack["schema"] == SCHEMA_TASK_PACK
    assert pack["project_id"] == str(pid)
    assert pack["finding"]["kind"] == "schema_mismatch"
    assert pack["finding"]["finding_id"] == str(finding_id)
    assert pack["finding"]["detail"]["snippet"]["omitted"] == "raw_payload"
    assert pack["subject"]["node"]["id"] == "sym:orders:create_order"
    assert pack["precomputed_mcp_context"]["symbol"]["l1_summary"].startswith("Creates an order")
    assert pack["graph_slice"]["subject"]["node"]["id"] == "sym:orders:create_order"
    assert pack["impact"]["affected_data_entities"] == ["data:orders"]
    assert pack["next_mcp_queries"][0]["tool"] == "get_task_context_pack"
    assert "repo.write_raw_legacy_order" not in json.dumps(pack)


def test_historical_summary_fields_are_bounded_before_artifact_packing():
    summary = Summary(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        target_id="sym:oversized",
        level=2,
        summary="요약" * 5_000,
        detailed="detail " * 10_000,
        claims=[{"claim": "x" * 700} for _ in range(100)],
        open_questions=["question " * 200 for _ in range(100)],
        model_used="historical-test",
    )

    ref = _summary_ref(summary)

    assert len(ref["summary"]) <= 1_200
    assert len(ref["detailed"]) <= 4_000
    assert len(ref["claims"]) == 20
    assert len(ref["open_questions"]) == 12
    assert ref["truncation"]["claims"] == {"kept": 20, "total": 100}
    assert ref["truncation"]["open_questions"] == {
        "kept": 12,
        "total": 100,
    }


@pytest.mark.asyncio
async def test_task_context_pack_has_hard_serialized_byte_ceiling(seeded_session):
    session, pid, _finding_id = seeded_session
    oversized_claim = {f"field_{i}": "한" * 700 for i in range(80)}
    session.add_all(
        [
            Summary(
                id=uuid.uuid4(),
                project_id=pid,
                    target_id="sym:orders:create_order",
                    level=level,
                    validated_graph_generation=1,
                    validated_overlay_generation=0,
                    validated_at=datetime.now(tz=timezone.utc),
                summary="요약" * 5_000,
                detailed="detail " * 20_000,
                claims=[dict(oversized_claim) for _ in range(100)],
                open_questions=["question " * 500 for _ in range(100)],
                model_used="hostile-history",
            )
            for level in range(10, 18)
        ]
    )
    await session.commit()

    pack = await build_task_context_pack(
        session,
        project_id=pid,
        target_id="sym:orders:create_order",
        budget_items=200,
    )
    serialized_bytes = len(json.dumps(pack, default=str).encode("utf-8"))
    budget = pack["truncation"]["serialized_budget"]

    assert serialized_bytes <= DEFAULT_AGENT_CONTEXT_MAX_BYTES
    assert budget["actual_bytes"] == serialized_bytes
    assert budget["max_bytes"] == DEFAULT_AGENT_CONTEXT_MAX_BYTES
    assert budget["truncated"] is True
    assert budget["reason"] == "serialized_byte_ceiling"
    assert pack["truncation"]["summaries"] == {
        "kept": 8,
        "more_available": True,
    }
    assert pack["schema"] == SCHEMA_TASK_PACK
    assert pack["target"] == {
        "id": "sym:orders:create_order",
        "kind": "Symbol",
        "intent": None,
    }
    assert pack["evidence_refs"][0]["id"] == "sym:orders:create_order"

    # The artifact contract fits the MCP transport contract without invoking
    # its independent emergency truncation/fallback shape.
    from app.mcp.server import _cap_response

    transported = json.loads(_cap_response(pack))
    assert "response_truncated" not in transported
    assert transported["schema"] == SCHEMA_TASK_PACK


@pytest.mark.asyncio
async def test_project_index_has_hard_serialized_byte_ceiling(seeded_session):
    session, pid, _finding_id = seeded_session
    hostile_data = {
        "name": "hostile-contract",
        **{f"field_{i}": "界" * 700 for i in range(80)},
    }
    session.add_all(
        [
            Node(
                id=f"contract:hostile:{i:03d}",
                project_id=pid,
                kind="Contract",
                data=dict(hostile_data),
                certainty="inferred",
                created_by=["historical-import"],
            )
            for i in range(100)
        ]
    )
    await session.commit()

    index = await build_project_index(session, project_id=pid, top_k=100)
    serialized_bytes = len(json.dumps(index, default=str).encode("utf-8"))
    budget = index["truncation"]["serialized_budget"]

    assert serialized_bytes <= DEFAULT_AGENT_CONTEXT_MAX_BYTES
    assert budget["actual_bytes"] == serialized_bytes
    assert budget["max_bytes"] == DEFAULT_AGENT_CONTEXT_MAX_BYTES
    assert budget["truncated"] is True
    assert index["schema"] == SCHEMA_PROJECT_INDEX
    assert index["project"]["id"] == str(pid)
    assert index["entrypoints"]["contracts"][0]["evidence"]["type"] == "node"


@pytest.mark.asyncio
async def test_direct_mcp_symbol_outputs_are_agent_safe(seeded_session):
    from app.mcp.queries import get_symbol, search_symbols

    session, pid, _finding_id = seeded_session
    session.add_all(
        [
            Node(
                id="sym:src:callTool",
                project_id=pid,
                kind="Symbol",
                data={
                    "name": "callTool",
                    "signature": "export function callTool(name) {\n  return fetch(name);\n}",
                    "location": {"file": "src/rpc.ts", "line": 1},
                },
                certainty="asserted",
                created_by=["test"],
            ),
            Node(
                id="sym:tests:call_tool",
                project_id=pid,
                kind="Symbol",
                data={
                    "name": "call_tool",
                    "signature": "def call_tool(name):\n    return client.request(name)",
                    "location": {"file": "tests/test_mcp.py", "line": 1},
                },
                certainty="asserted",
                created_by=["test"],
            ),
        ]
    )
    await session.commit()

    results = await search_symbols(
        session, project_id=pid, query="create order", top_k=5
    )
    assert results[0]["excerpt"] == "def create_order(repo, payload):"
    assert "leaked_body_call" not in json.dumps(results)

    symbol = await get_symbol(
        session, project_id=pid, symbol_id="sym:orders:create_order"
    )
    assert symbol is not None
    assert symbol["symbol"]["data"]["signature"] == "def create_order(repo, payload):"
    assert symbol["symbol"]["data"]["content"]["omitted"] == "raw_payload"
    assert "leaked_body_call" not in json.dumps(symbol)

    ranked = await search_symbols(session, project_id=pid, query="call tool", top_k=2)
    assert ranked[0]["symbol_id"] == "sym:src:callTool"
    assert ranked[0]["excerpt"] == "export function callTool(name) {"


@pytest.mark.asyncio
async def test_agents_md_is_agent_contract_not_human_report(seeded_session):
    session, pid, _finding_id = seeded_session
    md = await build_agents_md(session, project_id=pid)

    assert "## Agent Contract" in md
    assert "Never load the whole repository into context" in md
    assert "`get_project_index`" in md
    assert "`get_task_context_pack`" in md


@pytest.fixture()
async def cpp_gap_session():
    """A C-dominant project whose ``*:cpp`` stages all skipped — the exact
    shape of the 2026-07-07 real-repo eval. The project index must say so."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as session:
        pid = uuid.uuid4()
        rid = uuid.uuid4()
        session.add(
            Project(
                id=pid,
                name="c-dominant-repo",
                gitlab_project_id=9002,
                gitlab_url="https://demo.local/c-dominant-repo",
                default_branch="main",
                languages=["cpp", "typescript", "javascript"],
            )
        )
        now = datetime.now(tz=timezone.utc)
        session.add(
            AnalysisRun(
                id=rid,
                project_id=pid,
                status="completed",
                triggered_by="test",
                git_sha="def456",
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(
                    generation=1,
                    published_at=now,
                    stats={"symbols": 2},
                ),
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=pid,
                current_run_id=rid,
                generation=1,
                state="ready",
                published_at=now,
                updated_at=now,
            )
        )
        session.add_all(
            [
                AnalysisStage(
                    id=uuid.uuid4(), run_id=rid, project_id=pid,
                    name="symbols:cpp", language="cpp", status="completed",
                    position=1, stats={"skipped": True, "reason": "no_analyzer"},
                ),
                AnalysisStage(
                    id=uuid.uuid4(), run_id=rid, project_id=pid,
                    name="symbols:typescript", language="typescript",
                    status="completed", position=2, items_done=140,
                    stats={"symbols": 140},
                ),
                AnalysisStage(
                    id=uuid.uuid4(), run_id=rid, project_id=pid,
                    name="symbols:javascript", language="javascript",
                    status="completed", position=3,
                    stats={"skipped": True, "reason": "covered_by:typescript"},
                ),
                AnalysisStage(
                    id=uuid.uuid4(), run_id=rid, project_id=pid,
                    name="agent_extract:cpp", language="cpp",
                    status="completed", position=4,
                    stats={"skipped": True, "reason": "agent_sdk_unavailable"},
                ),
            ]
        )
        session.add_all(
            [
                Node(
                    id="ts:rpc.ts:callTool@15:1",
                    project_id=pid,
                    kind="Symbol",
                    data={
                        "name": "callTool",
                        "signature": "export async function callTool<T>(",
                        "location": {
                            "file": "E:/repos/c-dominant-repo/graph-ui/src/api/rpc.ts",
                            "line": 15,
                        },
                    },
                    certainty="asserted",
                    created_by=["ggoss-ts"],
                ),
                Node(
                    id="py:mcp_stdio.py:call_tool@30",
                    project_id=pid,
                    kind="Symbol",
                    data={
                        "name": "call_tool",
                        "signature": "def call_tool(self, name):",
                        "location": {
                            "file": "E:\\repos\\c-dominant-repo\\tests\\windows\\mcp_stdio.py",
                            "line": 30,
                        },
                    },
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
                Node(
                    id="contract:http:POST:/rpc",
                    project_id=pid,
                    kind="Contract",
                    data={"name": "POST /rpc", "method": "POST", "path": "/rpc"},
                    certainty="inferred",
                    created_by=["ggoss-ts"],
                ),
            ]
        )
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=pid,
                target_id="ts:rpc.ts:callTool@15:1",
                level=1,
                validated_graph_generation=1,
                validated_overlay_generation=0,
                validated_at=now,
                summary="structural stub",
                model_used="stub",
                fallback_reason="no_backend",
            )
        )
        await session.commit()
        yield session, pid
    await eng.dispose()


@pytest.mark.asyncio
async def test_coverage_report_flags_missing_dominant_language(cpp_gap_session):
    session, pid = cpp_gap_session
    index = await build_project_index(session, project_id=pid, top_k=5)
    cov = index["coverage_report"]

    assert cov["status"] == "partial"
    assert cov["requested_languages"] == ["cpp", "typescript", "javascript"]
    gap_langs = {g["language"] for g in cov["critical_gaps"]}
    assert gap_langs == {"cpp"}
    assert "no_analyzer" in cov["critical_gaps"][0]["reason"]
    # javascript skipped as covered_by:typescript is NOT a gap.
    assert not any(g["language"] == "javascript" for g in cov["critical_gaps"])
    skipped_stages = {s["stage"] for s in cov["skipped"]}
    assert "symbols:cpp" in skipped_stages
    # Stub-only summaries carry an explicit low-signal warning.
    assert cov["summary_quality"]["stub_only"] is True
    assert "warning" in cov["summary_quality"]
    assert any("cpp" in r for r in cov["recommendation"])


@pytest.mark.asyncio
async def test_relative_file_normalization_across_path_styles(cpp_gap_session):
    session, pid = cpp_gap_session
    index = await build_project_index(session, project_id=pid, top_k=5)

    by_id = {s["symbol_id"]: s for s in index["entrypoints"]["hot_symbols"]}
    ts = by_id["ts:rpc.ts:callTool@15:1"]
    py = by_id["py:mcp_stdio.py:call_tool@30"]
    # POSIX-absolute and Windows-absolute both normalise to one stable
    # project-relative path.  Temporary checkout paths must not leak through
    # the AI-facing context after normalisation.
    assert ts["location"]["relative_file"] == "graph-ui/src/api/rpc.ts"
    assert py["location"]["relative_file"] == "tests/windows/mcp_stdio.py"
    assert ts["location"]["file"] == "graph-ui/src/api/rpc.ts"

    pack = await build_task_context_pack(
        session, project_id=pid, target_id="ts:rpc.ts:callTool@15:1"
    )
    assert (
        pack["target_node"]["location"]["relative_file"]
        == "graph-ui/src/api/rpc.ts"
    )


@pytest.mark.asyncio
async def test_client_inferred_contract_without_exposer_is_flagged(cpp_gap_session):
    session, pid = cpp_gap_session
    index = await build_project_index(session, project_id=pid, top_k=5)
    contract = index["entrypoints"]["contracts"][0]

    assert contract["id"] == "contract:http:POST:/rpc"
    assert contract["has_exposer"] is False
    assert contract["warning"] == "client_inferred_no_server_exposer"

    pack = await build_task_context_pack(
        session, project_id=pid, target_id="contract:http:POST:/rpc"
    )
    assert pack["graph_slice"]["contract"]["server_exposer_missing"] is True


@pytest.mark.asyncio
async def test_search_scope_and_source_role(cpp_gap_session):
    from app.mcp.queries import search_symbols

    session, pid = cpp_gap_session
    all_hits = await search_symbols(
        session, project_id=pid, query="call tool", top_k=10
    )
    roles = {r["symbol_id"]: r["source_role"] for r in all_hits}
    assert roles["ts:rpc.ts:callTool@15:1"] == "product"
    assert roles["py:mcp_stdio.py:call_tool@30"] == "test"
    assert all_hits[0]["location"]["relative_file"] == "graph-ui/src/api/rpc.ts"

    product_only = await search_symbols(
        session, project_id=pid, query="call tool", top_k=10, scope="product"
    )
    assert {r["symbol_id"] for r in product_only} == {"ts:rpc.ts:callTool@15:1"}

    tests_only = await search_symbols(
        session, project_id=pid, query="call tool", top_k=10, scope="tests"
    )
    assert {r["symbol_id"] for r in tests_only} == {"py:mcp_stdio.py:call_tool@30"}

    prefixed = await search_symbols(
        session, project_id=pid, query="call tool", top_k=10,
        path_prefix="graph-ui/src",
    )
    assert {r["symbol_id"] for r in prefixed} == {"ts:rpc.ts:callTool@15:1"}
