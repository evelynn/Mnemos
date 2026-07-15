"""Strict provider boundary and graph-grounding tests for agent extraction."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-agent-extract-contract")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.extractor.agent_extract import (
    AgentExtractContractError,
    extract_file_via_agent_sdk,
    normalize_agent_extract_payload,
    to_envelopes,
)
from app.models.graph import Edge, Node
from app.orchestrator.jobs import (
    AnalyzerStageOutcome,
    _authoritative_agent_refresh_sources,
    _record_agent_llm_call,
    _record_agent_envelopes,
    _restore_agent_totals_after_failure,
    _supersede_stale_graph_summaries,
)


def test_agent_refresh_sweep_requires_complete_manifest_coverage() -> None:
    from app.orchestrator.source_manifest import SourceManifest

    manifest = SourceManifest(
        fingerprints={"agent:cpp": "digest"},
        file_counts={"agent:cpp": 2},
        total_bytes={"agent:cpp": 100},
    )
    partial = AnalyzerStageOutcome(
        "agent:cpp", "extract", True, records=1
    )
    complete = AnalyzerStageOutcome(
        "agent:cpp", "extract", True, records=2
    )

    assert _authoritative_agent_refresh_sources(
        manifest, {"agent:cpp": [partial]}
    ) == set()
    assert _authoritative_agent_refresh_sources(
        manifest, {"agent:cpp": [complete]}
    ) == {"agent:cpp"}
    assert _authoritative_agent_refresh_sources(
        manifest, {"agent:cpp": [complete, complete]}
    ) == set()


def _code_payload() -> dict:
    return {
        "symbols": [
            {
                "id": "python:a.py::run",
                "name": "run",
                "qualified_name": "run",
                "kind": "function",
                "line": 3,
                "signature": "def run():",
                "calls": ["persist"],
                "summary": "Runs the operation.",
            }
        ],
        "edges": [],
        "data_access": [],
    }


def _db_entity(model_id: str, name: str) -> dict:
    return {
        "id": model_id,
        "name": name,
        "kind": "table",
        "columns": [
            {
                "name": "id",
                "type": "integer",
                "pk": True,
                "nullable": False,
                "sensitive": False,
            }
        ],
        "is_sensitive": False,
        "summary": "Stores users.",
    }


def test_agent_extract_normalizer_is_pure_idempotent_and_accepts_explicit_empty() -> None:
    raw = _code_payload()
    before = json.loads(json.dumps(raw))

    canonical = normalize_agent_extract_payload(raw, language="python")

    assert raw == before
    assert canonical == normalize_agent_extract_payload(canonical, language="python")
    assert canonical["symbols"][0]["qualified_name"] == "run"
    assert normalize_agent_extract_payload(
        {"symbols": [], "edges": [], "data_access": []}, language="python"
    ) == {"symbols": [], "edges": [], "data_access": []}
    assert normalize_agent_extract_payload(
        {"entities": [], "edges": []}, language="sql"
    ) == {"entities": [], "edges": []}


@pytest.mark.parametrize(
    ("payload", "path"),
    [
        ({}, "$.data_access"),
        ({"symbols": [], "edges": [], "data_access": [], "extra": []}, "$"),
        ({"symbols": {}, "edges": [], "data_access": []}, "$.symbols"),
        (
            {
                "symbols": [
                    _code_payload()["symbols"][0],
                    _code_payload()["symbols"][0],
                ],
                "edges": [],
                "data_access": [],
            },
            "$.symbols[1].id",
        ),
        (
            {
                **_code_payload(),
                "edges": [
                    {
                        "source_id": "python:a.py::run",
                        "target_id": "python:missing.py::ghost",
                        "kind": "CALLS",
                    }
                ],
            },
            "$.edges[0].target_id",
        ),
    ],
)
def test_agent_extract_normalizer_rejects_malformed_or_ambiguous_payloads(
    payload: dict, path: str
) -> None:
    with pytest.raises(AgentExtractContractError) as error:
        normalize_agent_extract_payload(payload, language="python")

    assert error.value.path == path


def test_agent_extract_diagnostic_does_not_echo_model_values() -> None:
    payload = _code_payload()
    payload["symbols"][0]["name"] = "super-secret-source-value" * 100

    with pytest.raises(AgentExtractContractError) as error:
        normalize_agent_extract_payload(payload, language="python")

    assert "super-secret-source-value" not in str(error.value)
    assert "$.symbols[0].name" in str(error.value)
    assert "str(len=" in str(error.value)


def test_schema_qualified_db_entities_with_same_short_name_do_not_collapse() -> None:
    payload = {
        "entities": [
            _db_entity("data:public.users", "users"),
            _db_entity("data:audit.users", "users"),
        ],
        "edges": [],
    }
    canonical = normalize_agent_extract_payload(payload, language="sql")

    envelopes = to_envelopes("sql", "schema.sql", canonical)
    ids = {
        envelope["data"]["id"]
        for envelope in envelopes
        if envelope["record_type"] == "data_entity"
    }

    assert len(ids) == 2
    assert all(node_id.startswith("agent-data:sql:") for node_id in ids)


def _fake_agent_sdk(payload: dict):
    class TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class AssistantMessage:
        def __init__(self, content: list[TextBlock]) -> None:
            self.content = content

    class ResultMessage:
        def __init__(self, is_error: bool = False) -> None:
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs) -> None:  # noqa: ANN003
            pass

    async def query(**_kwargs):  # noqa: ANN003
        yield AssistantMessage([TextBlock(json.dumps(payload))])
        yield ResultMessage(False)

    return SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )


@pytest.mark.asyncio
async def test_mock_provider_runs_through_real_parser_and_strict_normalizer(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": _fake_agent_sdk(_code_payload())}):
        accepted = await extract_file_via_agent_sdk(
            language="python", file_rel="a.py", code="def run(): pass"
        )
    with patch.dict(sys.modules, {"claude_agent_sdk": _fake_agent_sdk({})}):
        rejected = await extract_file_via_agent_sdk(
            language="python", file_rel="a.py", code="def run(): pass"
        )

    assert accepted is not None
    assert accepted == normalize_agent_extract_payload(accepted, language="python")
    assert rejected is None


@pytest.mark.asyncio
async def test_provider_start_callback_failure_stops_before_dispatch(
    monkeypatch, caplog
) -> None:
    sdk = _fake_agent_sdk(_code_payload())
    original_query = sdk.query
    query_calls = 0

    async def counting_query(**kwargs):
        nonlocal query_calls
        query_calls += 1
        async for message in original_query(**kwargs):
            yield message

    def reject_start() -> None:
        raise RuntimeError("sensitive-callback-detail")

    sdk.query = counting_query
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            on_provider_start=reject_start,
        )

    assert result is None
    assert query_calls == 0
    assert "RuntimeError" in caplog.text
    assert "sensitive-callback-detail" not in caplog.text


@pytest.mark.asyncio
async def test_agent_edge_persistence_requires_both_current_project_nodes() -> None:
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)
    Session = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    project_id = uuid.uuid4()
    other_project_id = uuid.uuid4()
    totals = {
        "symbols": 0,
        "contracts": 0,
        "edges": 0,
        "findings": 0,
        "data_entities": 0,
        "errors": 0,
    }
    extracted = {
        "symbols": [
            {"id": "python:a.py::run", "name": "run", "kind": "function"}
        ],
        "edges": [],
        "data_access": [
            {
                "symbol_id": "python:a.py::run",
                "table": "orders",
                "access": "READS",
            },
            {
                "symbol_id": "python:a.py::run",
                "table": "other_only",
                "access": "WRITES",
            },
            {
                "symbol_id": "python:a.py::run",
                "table": "missing",
                "access": "READS",
            },
            {
                "symbol_id": "python:a.py::run",
                "table": "not_an_entity",
                "access": "READS",
            },
        ],
    }
    async with Session() as session:
        session.add_all(
            [
                Node(
                    id="data:orders",
                    project_id=project_id,
                    kind="DataEntity",
                    data={"name": "orders"},
                    certainty="verified",
                    created_by=["ggoss-sql"],
                ),
                Node(
                    id="data:other_only",
                    project_id=other_project_id,
                    kind="DataEntity",
                    data={"name": "other_only"},
                    certainty="verified",
                    created_by=["ggoss-sql"],
                ),
                Node(
                    id="data:not_an_entity",
                    project_id=project_id,
                    kind="Symbol",
                    data={"name": "not_an_entity"},
                    certainty="verified",
                    created_by=["ggoss-py"],
                ),
            ]
        )
        await session.commit()

        added, dropped = await _record_agent_envelopes(
            session,
            project_id=project_id,
            envelopes=to_envelopes("python", "a.py", extracted),
            accept_kinds={"symbol", "data_entity", "edge"},
            totals=totals,
            expected_source_name="agent:python",
        )
        await session.commit()
        edges = (await session.execute(select(Edge))).scalars().all()

    await engine.dispose()
    assert added == 2  # one Symbol plus the one fully grounded READS edge
    assert dropped == 3
    assert [(edge.target_id, edge.kind) for edge in edges] == [
        ("data:orders", "READS")
    ]


def test_failed_agent_file_restores_totals_without_logging_raw_exception(
    caplog,
) -> None:
    totals = {"symbols": 9, "edges": 4, "errors": 1}
    snapshot = dict(totals)
    totals["symbols"] += 3
    totals["edges"] += 2

    _restore_agent_totals_after_failure(
        totals,
        snapshot,
        RuntimeError("super-secret-source-fragment"),
    )

    assert totals == snapshot
    assert "RuntimeError" in caplog.text
    assert "super-secret-source-fragment" not in caplog.text


@pytest.mark.asyncio
async def test_agent_physical_call_ledger_keeps_unknown_usage(monkeypatch) -> None:
    from app.models.findings import LLMCall
    from app.orchestrator import jobs
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMCall.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()

    await _record_agent_llm_call(
        project_id=project_id,
        run_id=run_id,
        language="python",
        file_rel="src/secret.py",
        status="fallback",
        fallback_reason="agent_extract_failed",
    )

    async with Session() as session:
        row = (await session.execute(select(LLMCall))).scalar_one()
    await engine.dispose()
    assert row.analysis_run_id == run_id
    assert row.target_id == "agent:python:src/secret.py"
    assert row.level == 0
    assert row.tokens_used is None
    assert row.status == "fallback"
    assert row.fallback_reason == "agent_extract_failed"


@pytest.mark.asyncio
async def test_successful_graph_refresh_retires_only_prior_summaries() -> None:
    from app.models.findings import Summary
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Summary.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    old_run_id = uuid.uuid4()
    current_run_id = uuid.uuid4()
    old_id = uuid.uuid4()
    current_id = uuid.uuid4()
    async with Session() as session:
        session.add_all(
            [
                Summary(
                    id=old_id,
                    project_id=project_id,
                    analysis_run_id=old_run_id,
                    target_id="old",
                    level=1,
                    summary="old",
                    claims=[],
                    model_used="provider",
                ),
                Summary(
                    id=current_id,
                    project_id=project_id,
                    analysis_run_id=current_run_id,
                    target_id="current",
                    level=1,
                    summary="current",
                    claims=[],
                    model_used="provider",
                ),
            ]
        )
        await session.commit()
        retired = await _supersede_stale_graph_summaries(
            session,
            project_id=project_id,
            run_id=current_run_id,
        )
        await session.commit()
        old = await session.get(Summary, old_id)
        current = await session.get(Summary, current_id)

    await engine.dispose()
    assert retired == 1
    assert old is not None and old.superseded_by is not None
    assert current is not None and current.superseded_by is None


@pytest.mark.asyncio
async def test_agent_stage_reserves_budget_and_ledgers_one_physical_call(
    tmp_path, monkeypatch
) -> None:
    from app.extractor.cost import LLMRunBudget
    from app.extractor import agent_extract
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_calls: list[dict] = []
    ledger_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def commit(self):
            return None

    class FakeStage:
        def __init__(self, *_args, **_kwargs):
            self.stats = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def increment(self, _count=1):
            return None

        def set_stats(self, stats):
            self.stats = stats

        def mark_partial(self, _reason):
            return None

    async def fake_extract(**kwargs):
        provider_calls.append(kwargs)
        return _code_payload()

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)

    async def budget_ok(_session, _project_id):
        return None

    async def record_ok(*_args, **_kwargs):
        return 1, 0

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_extract, "discover_source_files", lambda *_a, **_k: [source])
    monkeypatch.setattr(agent_extract, "extract_file_via_agent_sdk", fake_extract)
    monkeypatch.setattr(jobs, "_record_agent_llm_call", fake_ledger)
    monkeypatch.setattr(jobs, "_record_agent_envelopes", record_ok)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "require_budget", budget_ok)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(max_calls=2, max_input_tokens=10_000, wall_time_sec=30)

    outcome = await jobs._run_agent_extraction_stage(
        object(),
        uuid.uuid4(),
        uuid.uuid4(),
        "python",
        str(tmp_path),
        1,
        {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
        10,
        run_budget=budget,
        seen_nodes=set(),
        seen_edges=set(),
    )

    assert outcome.authoritative is True
    assert len(provider_calls) == 1
    assert len(ledger_calls) == 1
    assert ledger_calls[0]["status"] == "completed"
    assert ledger_calls[0]["fallback_reason"] is None
    assert budget.calls_started == 1
    assert budget.input_tokens_reserved > 0


@pytest.mark.asyncio
async def test_agent_stage_budget_rejection_makes_no_call_or_ledger(
    tmp_path, monkeypatch
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import BudgetExceeded, LLMRunBudget
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_calls: list[dict] = []
    ledger_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeStage:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def set_stats(self, _stats):
            return None

        def mark_partial(self, _reason):
            return None

    async def reject_budget(_session, _project_id):
        raise BudgetExceeded

    async def fake_extract(**kwargs):
        provider_calls.append(kwargs)
        return _code_payload()

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_extract, "discover_source_files", lambda *_a, **_k: [source])
    monkeypatch.setattr(agent_extract, "extract_file_via_agent_sdk", fake_extract)
    monkeypatch.setattr(jobs, "_record_agent_llm_call", fake_ledger)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "require_budget", reject_budget)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(max_calls=2, max_input_tokens=10_000, wall_time_sec=30)

    outcome = await jobs._run_agent_extraction_stage(
        object(),
        uuid.uuid4(),
        uuid.uuid4(),
        "python",
        str(tmp_path),
        1,
        {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
        10,
        run_budget=budget,
    )

    assert outcome.authoritative is False
    assert provider_calls == []
    assert ledger_calls == []
    assert budget.calls_started == 0
    assert budget.exhausted_reason == "budget_exceeded"


@pytest.mark.asyncio
async def test_agent_stage_run_deadline_owns_timeout_and_ledgers_once(
    tmp_path, monkeypatch
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    sdk_timeouts: list[int] = []
    ledger_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeStage:
        def __init__(self, *_args, **_kwargs):
            self.stats = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def increment(self, _count=1):
            return None

        def set_stats(self, stats):
            self.stats = stats

        def mark_partial(self, _reason):
            return None

    async def slow_extract(**kwargs):
        sdk_timeouts.append(kwargs["timeout_s"])
        kwargs["on_provider_start"]()
        await asyncio.sleep(10)
        return _code_payload()

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)

    async def budget_ok(_session, _project_id):
        return None

    class DeadlineBudget(LLMRunBudget):
        def reserve(self, estimated_input_tokens: int) -> float:
            self.calls_started += 1
            self.input_tokens_reserved += estimated_input_tokens
            return 0.05

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", slow_extract
    )
    monkeypatch.setattr(jobs, "_record_agent_llm_call", fake_ledger)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "require_budget", budget_ok)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = DeadlineBudget(
        max_calls=2, max_input_tokens=10_000, wall_time_sec=30
    )

    outcome = await jobs._run_agent_extraction_stage(
        object(),
        uuid.uuid4(),
        uuid.uuid4(),
        "python",
        str(tmp_path),
        1,
        {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
        10,
        run_budget=budget,
    )

    assert outcome.authoritative is False
    assert sdk_timeouts == [1]
    assert len(ledger_calls) == 1
    assert ledger_calls[0]["status"] == "timeout"
    assert ledger_calls[0]["fallback_reason"] == "run_deadline_exceeded"
    assert budget.calls_started == 1
    assert budget.exhausted_reason == "run_deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("ledger_fails", [False, True])
async def test_agent_stage_cancellation_ledgers_started_call_once_and_reraises(
    tmp_path, monkeypatch, caplog, ledger_fails
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_started = asyncio.Event()
    ledger_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeStage:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def set_stats(self, _stats):
            return None

        def mark_partial(self, _reason):
            return None

    async def cancellable_extract(**kwargs):
        kwargs["on_provider_start"]()
        provider_started.set()
        await asyncio.sleep(10)
        return _code_payload()

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)
        if ledger_fails:
            raise RuntimeError("sensitive-accounting-detail")

    async def budget_ok(_session, _project_id):
        return None

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", cancellable_extract
    )
    monkeypatch.setattr(jobs, "_record_agent_llm_call", fake_ledger)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "require_budget", budget_ok)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2, max_input_tokens=10_000, wall_time_sec=30
    )

    task = asyncio.create_task(
        jobs._run_agent_extraction_stage(
            object(),
            uuid.uuid4(),
            uuid.uuid4(),
            "python",
            str(tmp_path),
            1,
            {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
            10,
            run_budget=budget,
        )
    )
    await asyncio.wait_for(provider_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(ledger_calls) == 1
    assert ledger_calls[0]["status"] == "cancelled"
    assert ledger_calls[0]["fallback_reason"] == "agent_extract_cancelled"
    assert budget.calls_started == 1
    if ledger_fails:
        assert "RuntimeError" in caplog.text
        assert "sensitive-accounting-detail" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_started", [False, True])
async def test_agent_stage_adapter_exception_ledgers_only_after_dispatch(
    tmp_path, monkeypatch, provider_started
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    ledger_calls: list[dict] = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class FakeStage:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def set_stats(self, _stats):
            return None

        def mark_partial(self, _reason):
            return None

    async def broken_adapter(**kwargs):
        if provider_started:
            kwargs["on_provider_start"]()
        raise RuntimeError("sensitive-provider-detail")

    async def fake_ledger(**kwargs):
        ledger_calls.append(kwargs)

    async def budget_ok(_session, _project_id):
        return None

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", broken_adapter
    )
    monkeypatch.setattr(jobs, "_record_agent_llm_call", fake_ledger)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "require_budget", budget_ok)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2, max_input_tokens=10_000, wall_time_sec=30
    )

    outcome = await jobs._run_agent_extraction_stage(
        object(),
        uuid.uuid4(),
        uuid.uuid4(),
        "python",
        str(tmp_path),
        1,
        {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
        10,
        run_budget=budget,
    )

    assert outcome.authoritative is False
    if provider_started:
        assert len(ledger_calls) == 1
        assert ledger_calls[0]["status"] == "failed"
        assert ledger_calls[0]["fallback_reason"] == "agent_extract_failed"
    else:
        assert ledger_calls == []
    assert budget.calls_started == 1


async def _async_none() -> None:
    return None
