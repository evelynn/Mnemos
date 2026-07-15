"""PR-143 — cross-tier flow / process analysis.

trace_flow hands FE/BE/DB source slices to Claude Code and returns a
structured end-to-end trace (steps, per-boundary signals, flag values +
meanings, rows touched), persisted as a level-4 Summary.

Deterministic tests (no live LLM): tier/language classification, slug,
flow normalisation, prompt assembly, and route registration. The live
Claude Code trace is exercised by the 3-tier demo, not CI.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr143")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


def _valid_flow_payload():
    return {
        "summary": "Order creation crosses the API and database boundary.",
        "detailed": "The handler validates the request and inserts one order.",
        "steps": [
            {
                "order": 2,
                "tier": "DATABASE",
                "component": "orders",
                "action": "insert the row",
                "signal": {
                    "kind": "SQL",
                    "name": "INSERT orders",
                    "fields": [],
                },
            },
            {
                "order": 1,
                "tier": "BACKEND",
                "component": "orders.handler",
                "action": "validate the request",
                "signal": {
                    "kind": "HTTP_REQUEST",
                    "name": "POST /orders",
                    "fields": [
                        {
                            "name": "status",
                            "type": "string",
                            "values": ["new", "confirmed"],
                            "meaning": "Order lifecycle state.",
                        }
                    ],
                },
            },
        ],
        "flags": [
            {
                "name": "dry_run",
                "type": "boolean",
                "values": [
                    {"value": True, "meaning": "Validate without inserting."},
                    {"value": False, "meaning": "Persist the order."},
                ],
                "set_at": "backend/orders.handler",
                "used_at": "backend/orders.handler",
            }
        ],
        "data_touched": [
            {"entity": "orders", "op": "insert", "columns": ["id", "status"]}
        ],
        "open_questions": ["Where is retry policy configured?"],
    }


def test_classify_tiers_and_slug():
    from app.api.flow import _classify, _slug

    assert _classify(Path("/x/frontend/checkout.js")) == ("frontend", "javascript")
    assert _classify(Path("/x/backend/orders_handler.py")) == ("backend", "python")
    assert _classify(Path("/x/db/schema.sql")) == ("database", "sql")
    # extension-only fallbacks
    assert _classify(Path("/svc/Handler.cs"))[0] == "backend"
    assert _classify(Path("/q/migrations/001.sql")) == ("database", "sql")
    assert _slug("Place an order (checkout)!") == "place-an-order-checkout"


def test_flow_normalise_is_strict_bounded_idempotent_and_preserves_order_metadata():
    from app.extractor.agent_flow import (
        FLOW_RESULT_CONTRACT,
        _normalise,
    )

    out = _normalise(_valid_flow_payload())

    assert out["contract"] == FLOW_RESULT_CONTRACT
    assert [step["order"] for step in out["steps"]] == [1, 2]
    assert out["steps"][0]["tier"] == "backend"
    assert out["steps"][1]["tier"] == "database"
    assert out["steps"][0]["signal"]["kind"] == "http_request"
    assert out["data_touched"][0]["op"] == "INSERT"
    assert "source_scope" not in out
    assert _normalise(out) == out


def test_flow_normalise_rejects_instead_of_silently_dropping_or_renumbering():
    from app.extractor.agent_flow import (
        MAX_FLAGS,
        FlowContractError,
        _normalise,
    )

    invalid_payloads = []

    missing = _valid_flow_payload()
    missing.pop("detailed")
    invalid_payloads.append(missing)

    unknown = _valid_flow_payload()
    unknown["steps"][0]["signal"]["source-secret-sentinel"] = "not logged"
    invalid_payloads.append(unknown)

    gapped = _valid_flow_payload()
    gapped["steps"][0]["order"] = 3
    invalid_payloads.append(gapped)

    bad_enum = _valid_flow_payload()
    bad_enum["data_touched"][0]["op"] = "UPSERT"
    invalid_payloads.append(bad_enum)

    nested_text = _valid_flow_payload()
    nested_text["summary"] = {"source-secret-sentinel": "must not stringify"}
    invalid_payloads.append(nested_text)

    oversized = _valid_flow_payload()
    oversized["flags"] = [deepcopy(oversized["flags"][0]) for _ in range(MAX_FLAGS + 1)]
    invalid_payloads.append(oversized)

    for payload in invalid_payloads:
        with pytest.raises(FlowContractError) as exc_info:
            _normalise(payload)
        assert "source-secret-sentinel" not in str(exc_info.value)


def test_flow_summary_contract_normalizes_once_and_rejects_mixed_content():
    from app.extractor import agent_flow

    canonical_flow = agent_flow.normalize_flow_payload(_valid_flow_payload())
    run_id = uuid.uuid4()
    source_scope = {
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
    }

    # The provider adapter owns model dialect normalization.  Persistence is
    # a deterministic serializer and must not run that compatibility boundary
    # a second time.
    with patch.object(
        agent_flow,
        "normalize_flow_payload",
        side_effect=AssertionError("model payload normalized twice"),
    ):
        content = agent_flow.build_flow_summary_content(
            canonical_flow,
            analysis_run_id=run_id,
            revision="A" * 40,
            source_scope=source_scope,
        )

    original_sections = deepcopy(content.sections)
    normalized = agent_flow.normalize_flow_summary_content(
        list(reversed(content.sections)),
        summary=canonical_flow["summary"],
        detailed=canonical_flow["detailed"],
        open_questions=canonical_flow["open_questions"],
    )

    assert content.flow == canonical_flow
    assert content.source_snapshot["analysis_run_id"] == str(run_id)
    assert content.source_snapshot["revision"] == "a" * 40
    assert normalized.flow == canonical_flow
    assert normalized.source_snapshot == content.source_snapshot
    assert normalized.sections == content.sections
    assert content.sections == original_sections
    assert [section["section"] for section in normalized.sections] == list(
        agent_flow.FLOW_SUMMARY_SECTION_ORDER
    )

    invalid_cases = []
    duplicate = deepcopy(content.sections)
    duplicate[-1]["section"] = "flags"
    invalid_cases.append((duplicate, "$.sections[3].section"))

    mixed_claim = deepcopy(content.sections)
    mixed_claim[-1] = {
        "claim": "must not be treated as a flow section",
        "evidence": [],
    }
    invalid_cases.append((mixed_claim, "$.sections[3].contract"))

    mismatched_window = deepcopy(content.sections)
    mismatched_window[0]["data"]["file_windows"][0]["label"] = (
        "source-secret-sentinel.py"
    )
    invalid_cases.append(
        (mismatched_window, "$.source_snapshot.file_windows")
    )

    for raw, expected_path in invalid_cases:
        with pytest.raises(agent_flow.FlowContractError) as exc_info:
            agent_flow.normalize_flow_summary_content(
                raw,
                summary=canonical_flow["summary"],
                detailed=canonical_flow["detailed"],
                open_questions=canonical_flow["open_questions"],
            )
        assert exc_info.value.path == expected_path
        assert "source-secret-sentinel" not in str(exc_info.value)


def test_flow_prompt_includes_all_tiers_and_entry():
    from app.extractor.agent_flow import _build_flow_prompt

    prompt = _build_flow_prompt(
        "place order",
        [
            {"tier": "frontend", "label": "a.js", "language": "javascript", "code": "FE"},
            {"tier": "backend", "label": "b.py", "language": "python", "code": "BE"},
            {"tier": "database", "label": "c.sql", "language": "sql", "code": "DDL"},
        ],
    )
    assert "place order" in prompt
    for marker in ("tier=frontend", "tier=backend", "tier=database", "FE", "BE", "DDL"):
        assert marker in prompt
    # asks for flags + meanings + data_touched — the user's requirement
    assert '"flags"' in prompt and '"meaning"' in prompt and '"data_touched"' in prompt


@pytest.mark.asyncio
async def test_flow_agent_sdk_rejects_output_over_hard_cap(monkeypatch):
    from app.extractor import agent_flow

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs

    async def query(**_kwargs):  # noqa: ANN003
        yield AssistantMessage([TextBlock("x" * 33)])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_flow, "MAX_AGENT_OUTPUT_CHARS", 32)

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="checkout",
            sources=[{"tier": "backend", "label": "a.py", "code": "pass"}],
        )

    assert result is None


@pytest.mark.asyncio
async def test_flow_agent_sdk_mock_passes_parse_and_v1_normalization(monkeypatch):
    from app.extractor import agent_flow

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs

    payload = {
        "summary": "Checkout calls the order API.",
        "detailed": "Frontend to backend.",
        "steps": [
            {
                "order": 1,
                "tier": "FRONTEND",
                "component": "checkout.ts",
                "action": "submit order",
                "signal": {
                    "kind": "HTTP_REQUEST",
                    "name": "POST /orders",
                    "fields": [],
                },
            }
        ],
        "flags": [],
        "data_touched": [],
        "open_questions": [],
    }

    async def query(**_kwargs):  # noqa: ANN003
        yield AssistantMessage([TextBlock(f"```json\n{json.dumps(payload)}\n```")])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="checkout",
            sources=[{"tier": "frontend", "label": "a.ts", "code": "submit()"}],
        )

    assert result is not None
    assert set(result) == {"flow", "source_scope"}
    assert result["flow"]["contract"] == agent_flow.FLOW_RESULT_CONTRACT
    assert result["flow"]["steps"] == [
        {
            "order": 1,
            "tier": "frontend",
            "component": "checkout.ts",
            "action": "submit order",
            "signal": {
                "kind": "http_request",
                "name": "POST /orders",
                "fields": [],
            },
        }
    ]
    assert result["source_scope"]["provided_files"] == ["a.ts"]
    assert result["source_scope"]["files"] == [
        {
            "label": "a.ts",
            "tier": "frontend",
            "language": "unknown",
            "provided_chars": 8,
            "included_chars": 8,
            "prompt_truncated": False,
            "input_truncated": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_reason"),
    [
        ("completed", "completed", None),
        ("contract_rejected", "rejected", "flow_contract_rejected"),
        ("timeout", "timeout", "run_deadline_exceeded"),
    ],
)
async def test_flow_adapter_reserves_fresh_budget_and_reports_physical_outcome(
    monkeypatch,
    case,
    expected_status,
    expected_reason,
):
    from app.extractor import agent_flow
    from app.extractor.cost import LLMRunBudget

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    async def query(**_kwargs):  # noqa: ANN003
        if case == "timeout":
            await asyncio.sleep(1)
            return
        payload = _valid_flow_payload() if case == "completed" else {}
        yield AssistantMessage([TextBlock(json.dumps(payload))])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=100_000,
        wall_time_sec=60,
    )
    if case == "timeout":
        monkeypatch.setattr(budget, "remaining_seconds", lambda: 0.01)
    events = []

    async def before_provider_call():
        assert budget.calls_started == 0
        events.append("budget_checked")

    async def record_physical_call(status, reason, model):  # noqa: ANN001
        events.append((status, reason, model))

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="checkout",
            sources=[{"tier": "backend", "label": "a.py", "code": "pass"}],
            run_budget=budget,
            before_provider_call=before_provider_call,
            record_physical_call=record_physical_call,
        )

    assert (result is not None) is (case == "completed")
    assert budget.calls_started == 1
    assert budget.input_tokens_reserved > 0
    assert events[0] == "budget_checked"
    assert events[1][:2] == (expected_status, expected_reason)


@pytest.mark.asyncio
async def test_flow_adapter_preflight_failure_is_not_a_physical_call(monkeypatch):
    from app.extractor import agent_flow
    from app.extractor.cost import LLMRunBudget

    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: False)
    budget = LLMRunBudget(max_calls=1, max_input_tokens=10_000, wall_time_sec=60)

    async def must_not_run(*_args):
        raise AssertionError("preflight failure was counted as a physical call")

    result = await agent_flow.analyze_flow_via_agent_sdk(
        entry="checkout",
        sources=[{"tier": "backend", "label": "a.py", "code": "pass"}],
        run_budget=budget,
        before_provider_call=must_not_run,
        record_physical_call=must_not_run,
    )

    assert result is None
    assert budget.calls_started == 0


@pytest.mark.asyncio
async def test_flow_agent_records_exact_prompt_scope_for_partial_source_windows(
    monkeypatch,
):
    from app.extractor import agent_flow

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    payload = {
        "summary": "Bounded flow.",
        "detailed": "Only the recorded windows were analyzed.",
        "steps": [
            {
                "order": 1,
                "tier": "backend",
                "component": "a.py",
                "action": "run",
                "signal": {
                    "kind": "function_call",
                    "name": "run",
                    "fields": [],
                },
            }
        ],
        "flags": [],
        "data_touched": [],
        "open_questions": [],
    }
    captured = {}

    async def query(**kwargs):  # noqa: ANN003
        captured["prompt"] = kwargs["prompt"]
        yield AssistantMessage([TextBlock(json.dumps(payload))])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    sources = [
        {
            "tier": "backend",
            "language": "python",
            "label": "a.py",
            "code": "a = 1\n" * 20,
        },
        {
            "tier": "database",
            "language": "sql",
            "label": "b.sql",
            "code": "SELECT 1;\n" * 20,
            "truncated": True,
        },
    ]

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="bounded",
            sources=sources,
            max_total_chars=80,
        )

    assert result is not None
    scope = result["source_scope"]
    assert [item["label"] for item in scope["files"]] == ["a.py", "b.sql"]
    assert scope["included_source_chars"] <= 80
    assert scope["truncated"] is True
    assert all(item["prompt_truncated"] for item in scope["files"])
    assert scope["files"][1]["input_truncated"] is True
    assert "source_window=truncated" in captured["prompt"]
    for item in scope["files"]:
        original = next(source for source in sources if source["label"] == item["label"])
        excerpt = original["code"][: item["included_chars"]]
        assert excerpt in captured["prompt"]


def test_trace_flow_route_registered():
    from app.api.flow import router

    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/api/v1/projects/{project_id}/trace_flow" in paths


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("physical_status", "fallback_reason", "expected_rows"),
    [
        ("completed", None, 1),
        ("rejected", "flow_contract_rejected", 1),
        ("timeout", "run_deadline_exceeded", 1),
        (None, None, 0),
    ],
)
async def test_flow_api_durably_ledgers_only_physical_attempts(
    monkeypatch,
    physical_status,
    fallback_reason,
    expected_rows,
):
    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api import flow as flow_api
    from app.extractor.agent_flow import normalize_flow_payload
    from app.models.findings import LLMCall
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMCall.__table__.create)

    source = {
        "tier": "backend",
        "language": "python",
        "label": "orders.py",
        "code": "def create_order(): pass",
    }
    envelope = {
        "flow": normalize_flow_payload(_valid_flow_payload()),
        "source_scope": {
            "provided_files": [source["label"]],
            "files": [
                {
                    "label": source["label"],
                    "tier": source["tier"],
                    "language": source["language"],
                    "provided_chars": len(source["code"]),
                    "included_chars": len(source["code"]),
                    "prompt_truncated": False,
                    "input_truncated": False,
                }
            ],
            "omitted_files": [],
            "included_source_chars": len(source["code"]),
            "max_source_chars": 28_000,
            "truncated": False,
        },
    }

    async def fake_analyze(**kwargs):  # noqa: ANN003
        if physical_status is None:
            return None
        await kwargs["before_provider_call"]()
        kwargs["run_budget"].reserve(100)
        await kwargs["record_physical_call"](
            physical_status,
            fallback_reason,
            "claude-sonnet-test",
        )
        return envelope if physical_status == "completed" else None

    budget_checks = []

    async def allow_budget(*_args, **_kwargs):  # noqa: ANN002, ANN003
        budget_checks.append("checked")
        return None

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", fake_analyze)
    monkeypatch.setattr(flow_api, "require_budget", allow_budget)
    monkeypatch.setattr(flow_api, "audit_record", fake_audit)
    snapshot = GitSourceSnapshot(
        run_id=uuid.uuid4(),
        revision="a" * 40,
        mirrors=(),
        tree_prefix="",
    )

    async with AsyncSession(engine, expire_on_commit=False) as session:
        call = flow_api._analyze_and_persist(
            session,
            uuid.uuid4(),
            SimpleNamespace(id=uuid.uuid4()),
            "create order",
            [source],
            False,
            [],
            snapshot,
        )
        if physical_status == "completed":
            await call
        else:
            with pytest.raises(HTTPException, match="flow_analysis_failed"):
                await call
        rows = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()
    assert len(rows) == expected_rows
    assert len(budget_checks) == (0 if physical_status is None else 1)
    if rows:
        assert rows[0].status == physical_status
        assert rows[0].fallback_reason == fallback_reason
        assert rows[0].tokens_used is None
        assert rows[0].analysis_run_id == snapshot.run_id
        assert rows[0].level == 4


@pytest.mark.asyncio
async def test_explicit_historical_flow_has_v1_contract_but_is_not_current(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api import flow as flow_api
    from app.extractor.agent_flow import FLOW_RESULT_CONTRACT
    from app.extractor.validator import current_summary_claim_views
    from app.models.findings import Summary
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Summary.__table__.create)

    project_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    sources = [
        {
            "tier": "backend",
            "language": "python",
            "label": "orders.py",
            "code": "def create_order(): pass",
        },
        {
            "tier": "database",
            "language": "sql",
            "label": "schema.sql",
            "code": "CREATE TABLE orders(id int);",
        },
    ]
    canonical_flow = {
        "flow": {
            "contract": FLOW_RESULT_CONTRACT,
            "summary": "Create an order.",
            "detailed": "The handler writes an order.",
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
        },
        "source_scope": {
            "provided_files": ["orders.py", "schema.sql"],
            "files": [
                {
                    "label": "orders.py",
                    "tier": "backend",
                    "language": "python",
                    "provided_chars": len(sources[0]["code"]),
                    "included_chars": len(sources[0]["code"]),
                    "prompt_truncated": False,
                    "input_truncated": False,
                }
            ],
            "omitted_files": [
                {"label": "schema.sql", "reason": "source_budget"}
            ],
            "included_source_chars": len(sources[0]["code"]),
            "max_source_chars": 28_000,
            "truncated": True,
        },
    }

    async def fake_analyze(**_kwargs):  # noqa: ANN003
        return canonical_flow

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", fake_analyze)
    monkeypatch.setattr(flow_api, "audit_record", fake_audit)

    async with AsyncSession(engine, expire_on_commit=False) as session:
        first_snapshot = GitSourceSnapshot(
            run_id=uuid.uuid4(),
            revision="a" * 40,
            mirrors=(),
            tree_prefix="",
        )
        first = await flow_api._analyze_and_persist(
            session,
            project_id,
            user,
            "create order",
            sources,
            True,
            [],
            first_snapshot,
        )
        second_snapshot = GitSourceSnapshot(
            run_id=uuid.uuid4(),
            revision="b" * 40,
            mirrors=(),
            tree_prefix="",
        )
        second = await flow_api._analyze_and_persist(
            session,
            project_id,
            user,
            "create order",
            sources,
            True,
            [],
            second_snapshot,
        )

        rows = (
            await session.execute(
                select(Summary)
                .where(
                    Summary.project_id == project_id,
                    Summary.target_id == "flow:create-order",
                    Summary.level == 4,
                )
                .order_by(Summary.analysis_run_id)
            )
        ).scalars().all()
        persisted_row = next(row for row in rows if str(row.id) == second["summary_id"])
        current_view = (
            await current_summary_claim_views(
                session,
                project_id=project_id,
                summaries=[persisted_row],
            )
        )[0]

    await engine.dispose()

    assert len(rows) == 2
    current = [row for row in rows if row.superseded_by is None]
    superseded = [row for row in rows if row.superseded_by is not None]
    assert len(current) == 2 and superseded == []
    assert {str(row.id) for row in current} == {
        first["summary_id"],
        second["summary_id"],
    }
    assert persisted_row.analysis_run_id == second_snapshot.run_id
    assert persisted_row.validated_graph_generation is None
    assert persisted_row.validated_overlay_generation is None
    assert persisted_row.validated_at is None
    assert persisted_row.model_used == f"claude_code:{FLOW_RESULT_CONTRACT}"
    assert current_view.grounding_status == "hypothesis"
    assert current_view.claims == []
    assert current_view.flow == canonical_flow["flow"]
    assert {claim["contract"] for claim in persisted_row.claims} == {
        FLOW_RESULT_CONTRACT
    }
    assert {claim["section"] for claim in persisted_row.claims} == {
        "source_snapshot",
        "steps",
        "flags",
        "data_touched",
    }
    source_claim = next(
        claim for claim in persisted_row.claims if claim["section"] == "source_snapshot"
    )
    assert source_claim["data"]["files"] == ["orders.py"]
    assert source_claim["data"]["file_windows"] == canonical_flow["source_scope"]["files"]
    assert source_claim["data"]["omitted_files"] == [
        {"label": "schema.sql", "reason": "source_budget"}
    ]
    assert second["files_analyzed"] == ["orders.py"]
    assert "schema.sql:source_budget" in second["skipped_paths"]


@pytest.mark.asyncio
async def test_mock_provider_json_round_trips_through_storage_and_mcp(monkeypatch):
    """E2: mock provider text uses the real parse/normalize/store/read path."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api import flow as flow_api
    from app.extractor import agent_flow
    from app.graph_publication import GraphReadStamp
    from app.mcp.queries import get_module_summary, list_flows
    from app.models.findings import LLMCall, Summary
    from app.models.graph import AnalysisRun, GraphHead
    from app.models.organization import Organization
    from app.models.projects import Project
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.graph_publication import published_run_fields
    from app.testing.sqlite_polyglot import install_polyglot

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    async def query(**_kwargs):  # noqa: ANN003
        yield AssistantMessage([TextBlock(json.dumps(_valid_flow_payload()))])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)

    async def real_adapter(**kwargs):  # noqa: ANN003
        return await agent_flow.analyze_flow_via_agent_sdk(**kwargs)

    async def allow_budget(*_args, **_kwargs):  # noqa: ANN002, ANN003
        return None

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", real_adapter)
    monkeypatch.setattr(flow_api, "require_budget", allow_budget)
    monkeypatch.setattr(flow_api, "audit_record", fake_audit)

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
        await connection.run_sync(GraphHead.__table__.create)
        await connection.run_sync(Summary.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    published_at = datetime.now(tz=timezone.utc)
    snapshot = GitSourceSnapshot(
        run_id=run_id,
        revision="c" * 40,
        mirrors=(),
        tree_prefix="",
        graph_stamp=GraphReadStamp(
            project_id=project_id,
            generation=1,
            overlay_generation=0,
            current_run_id=run_id,
            published_at=published_at,
        ),
    )
    sources = [
        {
            "tier": "backend",
            "language": "python",
            "label": "orders.py",
            "code": "def create_order(): pass",
        },
        {
            "tier": "database",
            "language": "sql",
            "label": "schema.sql",
            "code": "INSERT INTO orders(id) VALUES (1);",
        },
    ]

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with AsyncSession(engine, expire_on_commit=False) as session:
            session.add(
                Project(
                    id=project_id,
                    name="flow E2",
                    gitlab_project_id=1,
                    gitlab_url="https://example.invalid/flow",
                    default_branch="main",
                    languages=["python"],
                )
            )
            session.add(
                AnalysisRun(
                    id=run_id,
                    project_id=project_id,
                    status="completed",
                    triggered_by="test",
                    git_sha="c" * 40,
                    scope="full",
                    started_at=published_at,
                    completed_at=published_at,
                    **published_run_fields(
                        generation=1,
                        published_at=published_at,
                    ),
                )
            )
            await session.commit()
            session.add(
                GraphHead(
                    project_id=project_id,
                    current_run_id=run_id,
                    generation=1,
                    overlay_generation=0,
                    state="ready",
                    published_at=published_at,
                )
            )
            await session.commit()
            persisted = await flow_api._analyze_and_persist(
                session,
                project_id,
                SimpleNamespace(id=uuid.uuid4()),
                "create order",
                sources,
                True,
                [],
                snapshot,
            )
            module_summary = await get_module_summary(
                session,
                project_id=project_id,
                target_id="flow:create-order",
                level=4,
            )
            session.add(
                Summary(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    target_id="flow:malformed",
                    level=4,
                    analysis_run_id=snapshot.run_id,
                    validated_graph_generation=1,
                    validated_overlay_generation=0,
                    validated_at=published_at,
                    summary="Must not leak raw content.",
                    detailed="Must not leak raw content.",
                    claims=[
                        {
                            "claim": "wrong dialect",
                            "evidence": [],
                            "source-secret-sentinel": "not returned",
                        }
                    ],
                    open_questions=[],
                    model_used=f"claude_code:{agent_flow.FLOW_RESULT_CONTRACT}",
                )
            )
            await session.commit()
            malformed_summary = await get_module_summary(
                session,
                project_id=project_id,
                target_id="flow:malformed",
                level=4,
            )
            flows = await list_flows(session, project_id=project_id)
            calls = (await session.execute(select(LLMCall))).scalars().all()

    await engine.dispose()

    expected_flow = agent_flow.normalize_flow_payload(_valid_flow_payload())
    assert persisted["summary_id"] is not None
    assert module_summary is not None
    assert module_summary["flow"] == expected_flow
    assert module_summary["source_snapshot"]["analysis_run_id"] == str(
        snapshot.run_id
    )
    assert module_summary["grounding_status"] == "hypothesis"
    assert module_summary["claims"] == []
    assert malformed_summary is not None
    assert malformed_summary["grounding_status"] == "invalid"
    assert malformed_summary["flow"] is None
    assert "source-secret-sentinel" not in malformed_summary["contract_error"]
    assert len(flows) == 1
    assert flows[0]["flow"] == expected_flow
    assert flows[0]["sections"] == module_summary["sections"]
    assert [(call.status, call.fallback_reason) for call in calls] == [
        ("completed", None)
    ]


@pytest.mark.asyncio
async def test_snapshot_source_loader_rejects_host_paths_and_pins_run(monkeypatch):
    from app.api import flow as flow_api
    from app.source_snapshot import GitSourceSnapshot

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    revision = "a" * 40
    snapshot = GitSourceSnapshot(
        run_id=run_id,
        revision=revision,
        mirrors=(),
        tree_prefix="",
    )
    calls: list[dict] = []

    async def fake_read_project_file(db, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return {
            "run_id": str(run_id),
            "revision": revision,
            "path": kwargs["file_path"],
            "encoding": "utf-8",
            "content": "const message = '한글';\n",
        }

    def fail_host_read(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("flow endpoint must not read a host working-tree path")

    monkeypatch.setattr(flow_api, "read_project_file", fake_read_project_file)
    monkeypatch.setattr(Path, "read_text", fail_host_read)

    sources, skipped = await flow_api._load_snapshot_sources(
        object(),
        project_id=project_id,
        snapshot=snapshot,
        project_paths=[
            "C:\\Windows\\secret.txt",
            "/etc/passwd",
            "../outside.py",
            "frontend/app.ts",
        ],
        max_file_bytes=20,
    )

    assert [call["file_path"] for call in calls] == ["frontend/app.ts"]
    assert calls[0]["run_id"] == run_id
    assert calls[0]["project_id"] == project_id
    assert len(sources[0]["code"].encode("utf-8")) <= 20
    assert sources[0]["label"] == "frontend/app.ts"
    assert sources[0]["tier"] == "frontend"
    assert sources[0]["truncated"] is True
    assert skipped == [
        "input[0]:invalid_project_path",
        "input[1]:invalid_project_path",
        "input[2]:invalid_project_path",
    ]


def test_flow_level_is_above_l3():
    from app.extractor.agent_flow import FLOW_LEVEL

    assert FLOW_LEVEL == 4  # L1-L3 are symbol/module/component summaries


@pytest.mark.asyncio
async def test_fresh_persisted_flow_is_a_bounded_hypothesis_not_a_stale_claim():
    """L4 sections are structured hypotheses, not `{claim, evidence}` rows.

    Revalidating them through the generic claim-only path used to discard all
    four sections and label a freshly persisted flow ``stale``.  The canonical
    flow view must preserve the bounded structure while remaining honest that
    step/flag prose has no graph evidence yet.
    """
    from app.extractor.agent_flow import FLOW_RESULT_CONTRACT
    from app.extractor.validator import current_summary_claim_views

    source_snapshot = {
        "analysis_run_id": str(uuid.uuid4()),
        "revision": "a" * 40,
        "files": ["orders.py"],
        "file_windows": [
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
    }
    flow = {
        "contract": FLOW_RESULT_CONTRACT,
        "summary": "Create an order.",
        "detailed": "The handler writes an order.",
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
    sections = [
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "source_snapshot",
            "data": source_snapshot,
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "steps",
            "data": flow["steps"],
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "flags",
            "data": flow["flags"],
        },
        {
            "contract": FLOW_RESULT_CONTRACT,
            "section": "data_touched",
            "data": flow["data_touched"],
        },
    ]
    summary = SimpleNamespace(
        level=4,
        summary=flow["summary"],
        detailed=flow["detailed"],
        claims=sections,
        open_questions=flow["open_questions"],
        model_used=f"claude_code:{FLOW_RESULT_CONTRACT}",
        fallback_reason=None,
    )

    view = (
        await current_summary_claim_views(
            object(),  # no graph query is needed for a hypothesis-only flow
            project_id=uuid.uuid4(),
            summaries=[summary],
        )
    )[0]

    assert view.claims == []
    assert view.grounding_status == "hypothesis"
    assert view.flow == flow
    assert view.source_snapshot == source_snapshot
    assert view.sections == sections
