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

pytestmark = pytest.mark.usefixtures("fake_llm_attempt_callbacks")

os.environ.setdefault("MNEMOS_LOCAL_MODE", "1")
os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr143")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("SESSION_COOKIE_SECURE", "false")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

_FLOW_TEST_OPERATION_ID = uuid.UUID("7a123b27-758e-4eb7-a6f8-c043de83dfd7")
_FLOW_TEST_BINDING = "b" * 64


@pytest.fixture(autouse=True)
def _flow_opaque_budget_env(monkeypatch):
    """Isolate Flow product tests from the production dollar-policy gate.

    Opaque Agent SDK dispatch is production-disabled by the immutable price
    contract boundary. These fake-provider tests exercise the independent
    parser/replay/product contract through the generic lifecycle mode; the
    production callsite's required flag has a separate static regression.
    """

    monkeypatch.setenv("MNEMOS_LLM_MAX_INPUT_TOKENS_PER_RUN", "1000000")
    monkeypatch.setenv("MNEMOS_LLM_MAX_OUTPUT_TOKENS_PER_RUN", "128000")
    from app.api import flow as flow_api

    production_begin_attempt = flow_api.begin_attempt

    async def generic_test_begin_attempt(*args, **kwargs):  # noqa: ANN002, ANN003
        assert kwargs.pop("require_atomic_dollar_reservation") is True
        return await production_begin_attempt(
            *args,
            **kwargs,
            require_atomic_dollar_reservation=False,
        )

    monkeypatch.setattr(
        flow_api,
        "begin_attempt",
        generic_test_begin_attempt,
    )


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

    unpaired_surrogate = _valid_flow_payload()
    unpaired_surrogate["summary"] = "invalid-utf8-\ud800"
    invalid_payloads.append(unpaired_surrogate)

    oversized = _valid_flow_payload()
    oversized["flags"] = [deepcopy(oversized["flags"][0]) for _ in range(MAX_FLAGS + 1)]
    invalid_payloads.append(oversized)

    for payload in invalid_payloads:
        with pytest.raises(FlowContractError) as exc_info:
            _normalise(payload)
        assert "source-secret-sentinel" not in str(exc_info.value)


def test_flow_normalise_rejects_fractional_scalar_with_actionable_path():
    """JSONB-normalized floats cannot authorize a replay digest."""

    from app.extractor.agent_flow import FlowContractError, _normalise

    fractional = _valid_flow_payload()
    fractional["flags"][0]["values"][0]["value"] = -0.0

    with pytest.raises(FlowContractError) as exc_info:
        _normalise(fractional)

    error = exc_info.value
    assert error.path == "$.flags[0].values[0].value"
    assert error.expected == "bounded string, boolean, or signed 64-bit integer"
    assert error.actual == "float"
    assert "fractional values as bounded strings" in error.hint


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
    from app.extractor.cost import LLMRunBudget

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs

    async def query(**_kwargs):  # noqa: ANN003
        # Nine code points are only nine Python characters but 36 UTF-8 bytes.
        yield AssistantMessage([TextBlock("😀" * 9)])
        yield ResultMessage(False)

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_flow, "MAX_AGENT_OUTPUT_BYTES", 32)

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="checkout",
            sources=[{"tier": "backend", "label": "a.py", "code": "pass"}],
            operation_id=_FLOW_TEST_OPERATION_ID,
            semantic_binding_identity=_FLOW_TEST_BINDING,
            run_budget=LLMRunBudget(
                max_calls=1,
                max_input_tokens=1_000_000,
                max_output_tokens=128_000,
            ),
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
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

    options_seen = []

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):  # noqa: ANN003
            self.kwargs = kwargs
            options_seen.append(kwargs)

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
            operation_id=_FLOW_TEST_OPERATION_ID,
            semantic_binding_identity=_FLOW_TEST_BINDING,
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
    assert options_seen[0]["tools"] == []
    assert options_seen[0]["allowed_tools"] == []
    assert options_seen[0]["setting_sources"] == []
    assert options_seen[0]["skills"] == []
    assert options_seen[0]["mcp_servers"] == {}
    assert options_seen[0]["agents"] == {}
    assert options_seen[0]["plugins"] == []
    assert options_seen[0]["permission_mode"] == "dontAsk"
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
    (
        "stop_reason",
        "is_error",
        "emit_result",
        "assistant_error",
        "assistant_model",
        "second_model",
        "expected_reason",
    ),
    [
        (
            "max_tokens",
            False,
            True,
            None,
            "claude-sonnet-4-6",
            None,
            "flow_output_limit_exceeded",
        ),
        (None, False, True, None, "claude-sonnet-4-6", None, "flow_provider_rejected"),
        ("tool_use", False, True, None, "claude-sonnet-4-6", None, "flow_provider_rejected"),
        ("end_turn", None, True, None, "claude-sonnet-4-6", None, "flow_provider_rejected"),
        ("end_turn", False, False, None, "claude-sonnet-4-6", None, "flow_provider_rejected"),
        ("end_turn", False, True, "server_error", "claude-sonnet-4-6", None, "flow_provider_rejected"),
        ("end_turn", False, True, None, "claude-sonnet-4-6", "claude-other", "flow_provider_rejected"),
        ("end_turn", False, True, None, "claude-other", None, "flow_provider_rejected"),
    ],
)
async def test_flow_agent_requires_authoritative_end_turn_result(
    monkeypatch,
    stop_reason,
    is_error,
    emit_result,
    assistant_error,
    assistant_model,
    second_model,
    expected_reason,
):
    from app.extractor import agent_flow

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(
            self,
            content,  # noqa: ANN001
            *,
            model="claude-sonnet-4-6",
            error=None,
        ):
            self.content = content
            self.model = model
            self.error = error

    class ResultMessage:
        def __init__(self):
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = stop_reason

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    async def query(**_kwargs):  # noqa: ANN003
        yield AssistantMessage(
            [TextBlock(json.dumps(_valid_flow_payload()))],
            model=assistant_model,
            error=assistant_error,
        )
        if second_model is not None:
            yield AssistantMessage([], model=second_model)
        if emit_result:
            yield ResultMessage()

    sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    outcomes = []

    async def record(status, reason, model):  # noqa: ANN001
        outcomes.append((status, reason, model))

    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await agent_flow.analyze_flow_via_agent_sdk(
            entry="checkout",
            sources=[{"tier": "backend", "label": "a.py", "code": "pass"}],
            operation_id=_FLOW_TEST_OPERATION_ID,
            semantic_binding_identity=_FLOW_TEST_BINDING,
            record_physical_call=record,
        )

    assert result is None
    recorded_model = (
        assistant_model
        if assistant_model != "claude-sonnet-4-6" and second_model is None
        else "claude-sonnet-4-6"
    )
    assert outcomes == [("rejected", expected_reason, recorded_model)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_status", "expected_reason"),
    [
        ("completed", "completed", None),
        ("contract_rejected", "rejected", "flow_contract_rejected"),
        ("timeout", "timeout", "flow_provider_timeout"),
    ],
)
async def test_flow_adapter_delegates_budget_reservation_and_reports_physical_outcome(
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
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

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
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
    )
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
            operation_id=_FLOW_TEST_OPERATION_ID,
            semantic_binding_identity=_FLOW_TEST_BINDING,
            run_budget=budget,
            timeout_s=0.01 if case == "timeout" else 300,
            before_provider_call=before_provider_call,
            record_physical_call=record_physical_call,
        )

    assert (result is not None) is (case == "completed")
    # The adapter must not reserve locally. The installed lifecycle owner is
    # solely responsible for a genuinely new durable attempt, which keeps an
    # exact replay possible under a fresh exhausted request budget.
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0
    assert events[0] == "budget_checked"
    assert events[1][:2] == (expected_status, expected_reason)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("durable_remaining", "predispatch_delay", "expected_dispatch"),
    [(0.01, 0.0, True), (0.0, 0.0, False), (0.01, 0.02, False)],
)
async def test_flow_adapter_uses_durable_remaining_time_after_start_delay(
    monkeypatch,
    durable_remaining,
    predispatch_delay,
    expected_dispatch,
):
    from app.extractor import agent_flow
    from app.extractor.cost import LLMRunBudget
    from app.llm.contracts import AttemptStatus, UsageStatus
    from app.llm.lifecycle import (
        AttemptCallbacks,
        AttemptTicket,
        use_attempt_callbacks,
    )

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        pass

    class ResultMessage:
        pass

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    dispatched = asyncio.Event()

    async def query(**_kwargs):  # noqa: ANN003
        dispatched.set()
        await asyncio.sleep(1)
        if False:  # pragma: no cover - preserves the async-generator shape
            yield None

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    outcomes = []

    async def start(metadata):  # noqa: ANN001
        # Simulate durable lock/commit latency.  The returned DB-owned
        # remaining time, not the stale pre-start local sample, must govern
        # the provider wait.
        await asyncio.sleep(0.01)
        return AttemptTicket(
            attempt_id=uuid.uuid4(),
            operation_id=metadata.operation_id,
            budget_scope_id=uuid.uuid4(),
            started_at=datetime.now(tz=timezone.utc),
            remaining_seconds=durable_remaining,
        )

    async def finish(_ticket, outcome):  # noqa: ANN001
        outcomes.append(outcome)

    async def finish_candidate(_ticket, outcome, _candidate):  # noqa: ANN001
        outcomes.append(outcome)

    async def replay_candidate(_request):  # noqa: ANN001
        from app.llm.lifecycle import LLMSemanticCandidateUnavailable

        raise LLMSemanticCandidateUnavailable("test candidate unavailable")

    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
        mark_provider_dispatch=lambda: None,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=1_000_000,
        max_output_tokens=128_000,
        wall_time_sec=60,
    )

    async def before_provider_call():
        await asyncio.sleep(predispatch_delay)

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with use_attempt_callbacks(callbacks):
            result = await asyncio.wait_for(
                agent_flow.analyze_flow_via_agent_sdk(
                    entry="checkout",
                    sources=[
                        {"tier": "backend", "label": "a.py", "code": "pass"}
                    ],
                    operation_id=_FLOW_TEST_OPERATION_ID,
                    semantic_binding_identity=_FLOW_TEST_BINDING,
                    run_budget=budget,
                    before_provider_call=before_provider_call,
                ),
                timeout=0.25,
            )

    assert dispatched.is_set() is expected_dispatch
    assert result is None
    assert len(outcomes) == 1
    assert outcomes[0].attempt_status == AttemptStatus.TIMEOUT
    assert outcomes[0].failure_code == agent_flow.FLOW_FALLBACK_RUN_DEADLINE
    assert outcomes[0].usage.status == (
        UsageStatus.LOST_AFTER_DISPATCH
        if expected_dispatch
        else UsageStatus.UNAVAILABLE
    )


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
        operation_id=_FLOW_TEST_OPERATION_ID,
        semantic_binding_identity=_FLOW_TEST_BINDING,
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
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

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
            operation_id=_FLOW_TEST_OPERATION_ID,
            semantic_binding_identity=_FLOW_TEST_BINDING,
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
    (
        "physical_status",
        "fallback_reason",
        "expected_rows",
        "expected_legacy_status",
    ),
    [
        ("completed", None, 1, "completed"),
        ("rejected", "flow_contract_rejected", 1, "fallback"),
        ("timeout", "run_deadline_exceeded", 1, "timeout"),
        (None, None, 0, None),
    ],
)
async def test_flow_api_durably_ledgers_only_physical_attempts(
    monkeypatch,
    physical_status,
    fallback_reason,
    expected_rows,
    expected_legacy_status,
):
    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api import flow as flow_api
    from app.extractor.agent_flow import (
        flow_candidate_binding_fingerprint,
        normalize_flow_payload,
    )
    from app.llm.contracts import (
        AttemptKind,
        AttemptStatus,
        ResultStatus,
        UsageSource,
        unavailable_usage,
    )
    from app.llm.lifecycle import (
        AttemptOutcome,
        AttemptStartMetadata,
        SemanticCandidate,
        current_attempt_callbacks,
    )
    from app.models.findings import LLMBudgetScope, LLMCall, LLMProjectBudgetAccount
    from app.models.llm import LLMSemanticCandidate
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)

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
        callbacks = current_attempt_callbacks()
        assert callbacks is not None
        ticket = await callbacks.start(
            AttemptStartMetadata(
                operation_id=uuid.uuid5(
                    kwargs["operation_id"],
                    "mnemos.flow.fake-provider.v1",
                ),
                attempt_no=1,
                attempt_kind=AttemptKind.PRIMARY,
                provider="anthropic",
                provider_mode="unknown",
                requested_model="claude-sonnet-4-6",
                input_fingerprint="d" * 64,
                estimated_input_tokens=100,
                input_estimate_method="test_v1",
                requested_max_output_tokens=128_000,
                usage_source=UsageSource.AGENT_SDK,
            )
        )
        kwargs["attempt_state"].attempt_id = ticket.attempt_id
        if physical_status == "completed":
            attempt_status = AttemptStatus.COMPLETED
            result_status = ResultStatus.PENDING
        elif physical_status == "timeout":
            attempt_status = AttemptStatus.TIMEOUT
            result_status = ResultStatus.NOT_APPLICABLE
        else:
            attempt_status = AttemptStatus.COMPLETED
            result_status = ResultStatus.SCHEMA_REJECTED
        outcome = AttemptOutcome(
                attempt_status=attempt_status,
                result_status=result_status,
                usage=unavailable_usage(
                    lost_after_dispatch=physical_status == "timeout",
                    source=UsageSource.AGENT_SDK,
                ),
                failure_code=fallback_reason,
                provider_mode="unknown",
            )
        if physical_status == "completed":
            assert callbacks.finish_candidate is not None
            await callbacks.finish_candidate(
                ticket,
                outcome,
                SemanticCandidate(
                    contract_name=flow_api.FLOW_RESULT_CONTRACT,
                    binding_fingerprint=flow_candidate_binding_fingerprint(
                        kwargs["semantic_binding_identity"],
                        envelope["source_scope"],
                    ),
                    payload=envelope["flow"],
                ),
            )
        else:
            await callbacks.finish(ticket, outcome)
        return envelope if physical_status == "completed" else None

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", fake_analyze)
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
        scopes = (
            await session.execute(select(LLMBudgetScope))
        ).scalars().all()

    await engine.dispose()

    assert len(rows) == expected_rows
    assert len(scopes) == expected_rows
    if rows:
        assert scopes[0].calls_reserved == 1
        assert scopes[0].input_tokens_reserved == 100
        assert scopes[0].output_tokens_reserved == 128_000
        assert rows[0].status == expected_legacy_status
        assert rows[0].fallback_reason == fallback_reason
        assert rows[0].tokens_used is None
        assert rows[0].analysis_run_id is None
        assert rows[0].contract_version == 1
        assert rows[0].purpose == "flow"
        assert rows[0].level == 4


@pytest.mark.asyncio
async def test_explicit_historical_flow_has_v1_contract_but_is_not_current(monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from app.api import flow as flow_api
    from app.extractor.agent_flow import (
        FLOW_RESULT_CONTRACT,
        flow_candidate_binding_fingerprint,
    )
    from app.extractor.validator import current_summary_claim_views
    from app.llm.contracts import (
        AttemptKind,
        AttemptStatus,
        ResultStatus,
        UsageSource,
        unavailable_usage,
    )
    from app.llm.lifecycle import (
        AttemptOutcome,
        AttemptStartMetadata,
        SemanticCandidate,
        current_attempt_callbacks,
        fingerprint_input,
    )
    from app.models.findings import (
        LLMBudgetScope,
        LLMCall,
        LLMProjectBudgetAccount,
        Summary,
    )
    from app.models.graph import AnalysisRun
    from app.models.llm import LLMSemanticCandidate
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
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

    async def fake_analyze(**kwargs):  # noqa: ANN003
        callbacks = current_attempt_callbacks()
        assert callbacks is not None and callbacks.finish_candidate is not None
        input_fingerprint = fingerprint_input(
            "mnemos.flow.historical-test.v1",
            str(kwargs["operation_id"]),
        )
        ticket = await callbacks.start(
            AttemptStartMetadata(
                operation_id=uuid.uuid5(
                    kwargs["operation_id"],
                    "mnemos.flow.historical-test.v1",
                ),
                attempt_no=1,
                attempt_kind=AttemptKind.PRIMARY,
                provider="anthropic",
                provider_mode="unknown",
                requested_model="claude-sonnet-4-6",
                input_fingerprint=input_fingerprint,
                estimated_input_tokens=100,
                input_estimate_method="test_v1",
                requested_max_output_tokens=128_000,
                usage_source=UsageSource.AGENT_SDK,
                billing_route="claude_agent_sdk",
            )
        )
        kwargs["attempt_state"].attempt_id = ticket.attempt_id
        await callbacks.finish_candidate(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.PENDING,
                usage=unavailable_usage(source=UsageSource.AGENT_SDK),
                provider_mode="unknown",
            ),
            SemanticCandidate(
                contract_name=FLOW_RESULT_CONTRACT,
                binding_fingerprint=flow_candidate_binding_fingerprint(
                    kwargs["semantic_binding_identity"],
                    canonical_flow["source_scope"],
                ),
                payload=canonical_flow["flow"],
            ),
        )
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
        session.add(
            AnalysisRun(
                id=first_snapshot.run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha=first_snapshot.revision,
                scope="full",
            )
        )
        await session.commit()
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
        session.add(
            AnalysisRun(
                id=second_snapshot.run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha=second_snapshot.revision,
                scope="full",
            )
        )
        await session.commit()
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
    assert persisted_row.model_used == "claude-sonnet-4-6"
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
async def test_explicit_current_run_flow_is_validated_and_visible_to_mcp(monkeypatch):
    """An explicit current run remains a current-generation product."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    import app.source_snapshot as source_snapshot_module
    from app.api import flow as flow_api
    from app.extractor import agent_flow
    from app.mcp.queries import get_module_summary, list_flows
    from app.models.findings import (
        LLMBudgetScope,
        LLMCall,
        LLMProjectBudgetAccount,
        Summary,
    )
    from app.models.llm import LLMSemanticCandidate
    from app.models.graph import AnalysisRun, GraphHead
    from app.models.organization import Organization
    from app.models.projects import Project
    from app.orchestrator.source_manifest import INDEX_CONTRACT_VERSION
    from app.source_snapshot import resolve_git_source_snapshot
    from app.testing.graph_publication import published_run_fields
    from app.testing.sqlite_polyglot import install_polyglot

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self, is_error=False):  # noqa: FBT002
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    dispatches = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
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

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", real_adapter)
    monkeypatch.setattr(flow_api, "audit_record", fake_audit)
    monkeypatch.setattr(
        source_snapshot_module,
        "_configured_mirrors",
        lambda *_args, **_kwargs: (Path("."),),
    )

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
        await connection.run_sync(GraphHead.__table__.create)
        await connection.run_sync(Summary.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    published_at = datetime.now(tz=timezone.utc)
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
                    triggered_by="webhook:test",
                    git_sha="c" * 40,
                    scope="full",
                    started_at=published_at,
                    completed_at=published_at,
                    **published_run_fields(
                        generation=1,
                        published_at=published_at,
                        stats={
                            "refresh": {"authoritative_root": True},
                            "source_manifest": {
                                "contract_version": INDEX_CONTRACT_VERSION,
                            },
                        },
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
            snapshot = await resolve_git_source_snapshot(
                session,
                project_id=project_id,
                run_id=run_id,
            )
            assert snapshot.graph_stamp is not None
            assert snapshot.graph_stamp.current_run_id == run_id
            assert snapshot.graph_stamp.generation == 1
            assert snapshot.graph_stamp.overlay_generation == 0
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
            persisted_row = await session.get(
                Summary,
                uuid.UUID(persisted["summary_id"]),
            )
            head = await session.get(GraphHead, project_id)
            assert head is not None
            head.overlay_generation = 1
            await session.commit()
            replacement_snapshot = await resolve_git_source_snapshot(
                session,
                project_id=project_id,
                run_id=run_id,
            )
            assert replacement_snapshot.graph_stamp is not None
            assert replacement_snapshot.graph_stamp.overlay_generation == 1
            replacement = await flow_api._analyze_and_persist(
                session,
                project_id,
                SimpleNamespace(id=uuid.uuid4()),
                "create order",
                sources,
                True,
                [],
                replacement_snapshot,
            )
            replacement_row = await session.get(
                Summary,
                uuid.UUID(replacement["summary_id"]),
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
                    validated_overlay_generation=1,
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
            flow_rows = (
                await session.execute(
                    select(Summary)
                    .where(
                        Summary.project_id == project_id,
                        Summary.target_id == "flow:create-order",
                        Summary.level == 4,
                    )
                    .order_by(Summary.validated_overlay_generation)
                )
            ).scalars().all()
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
    assert dispatches == 2
    assert persisted["summary_id"] is not None
    assert replacement["summary_id"] is not None
    assert replacement["summary_id"] != persisted["summary_id"]
    assert persisted_row is not None
    assert replacement_row is not None
    assert persisted_row.analysis_run_id == run_id
    assert persisted_row.validated_graph_generation == 1
    assert persisted_row.validated_overlay_generation == 0
    assert persisted_row.validated_at is not None
    assert persisted_row.superseded_by == replacement_row.id
    assert replacement_row.analysis_run_id == run_id
    assert replacement_row.validated_graph_generation == 1
    assert replacement_row.validated_overlay_generation == 1
    assert replacement_row.validated_at is not None
    assert replacement_row.superseded_by is None
    assert len(flow_rows) == 2
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
        ("completed", None),
        ("completed", None),
    ]


@pytest.mark.asyncio
async def test_flow_retry_reuses_atomic_summary_product_without_redispatch(
    monkeypatch,
):
    """A crash after product commit reuses one attempt, candidate, and row."""

    from fastapi import HTTPException
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from app.api import flow as flow_api
    from app.extractor import agent_flow
    from app.llm.contracts import ResultStatus
    from app.models.findings import (
        LLMBudgetScope,
        LLMCall,
        LLMProjectBudgetAccount,
        Summary,
    )
    from app.models.graph import AnalysisRun
    from app.models.llm import LLMSemanticCandidate
    from app.source_snapshot import GitSourceSnapshot
    from app.testing.sqlite_polyglot import install_polyglot

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self):
            self.is_error = False
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    dispatches = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
        yield AssistantMessage([TextBlock(json.dumps(_valid_flow_payload()))])
        yield ResultMessage()

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)

    audit_calls = 0

    async def crash_once_after_product_commit(**_kwargs):  # noqa: ANN003
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 1:
            raise RuntimeError("simulated post-commit consumer crash")

    monkeypatch.setattr(
        flow_api,
        "audit_record",
        crash_once_after_product_commit,
    )

    install_polyglot()
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
        await connection.run_sync(Summary.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(flow_api.app_db, "SessionLocal", factory)

    project_id = uuid.uuid4()
    snapshot = GitSourceSnapshot(
        run_id=uuid.uuid4(),
        revision="d" * 40,
        mirrors=(),
        tree_prefix="",
    )
    sources = [
        {
            "tier": "backend",
            "language": "python",
            "label": "checkout.py",
            "code": "def checkout():\n    return True\n",
        }
    ]
    user = SimpleNamespace(id=uuid.uuid4())

    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with factory() as session:
            session.add(
                AnalysisRun(
                    id=snapshot.run_id,
                    project_id=project_id,
                    status="completed",
                    triggered_by="test",
                    git_sha=snapshot.revision,
                    scope="full",
                )
            )
            await session.commit()
            with pytest.raises(
                RuntimeError,
                match="post-commit consumer crash",
            ):
                await flow_api._analyze_and_persist(
                    session,
                    project_id,
                    user,
                    "checkout",
                    sources,
                    True,
                    [],
                    snapshot,
                )
            replayed = await flow_api._analyze_and_persist(
                session,
                project_id,
                user,
                "checkout",
                sources,
                True,
                [],
                snapshot,
            )
            summaries = (
                await session.execute(select(Summary))
            ).scalars().all()
            calls = (await session.execute(select(LLMCall))).scalars().all()
            candidates = (
                await session.execute(select(LLMSemanticCandidate))
            ).scalars().all()
            scopes = (
                await session.execute(select(LLMBudgetScope))
            ).scalars().all()
            summary_id = str(summaries[0].id)
            summary_attempt_id = summaries[0].llm_attempt_id
            call_id = calls[0].id
            call_result_status = calls[0].result_status
            candidate_status = candidates[0].candidate_status
            scope_calls_reserved = scopes[0].calls_reserved
            summaries[0].fallback_reason = "polluted-after-publication"
            await session.commit()
            with pytest.raises(HTTPException) as replay_error:
                await flow_api._analyze_and_persist(
                    session,
                    project_id,
                    user,
                    "checkout",
                    sources,
                    True,
                    [],
                    snapshot,
                )

    assert dispatches == 1
    assert audit_calls == 2
    assert replay_error.value.status_code == 503
    assert replay_error.value.detail == "flow_product_provenance_invalid"
    assert len(summaries) == len(calls) == len(candidates) == len(scopes) == 1
    assert replayed["summary_id"] == summary_id
    assert summary_attempt_id == call_id
    assert call_result_status == ResultStatus.ACCEPTED.value
    assert candidate_status == "accepted"
    assert scope_calls_reserved == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_flow_terminal_candidate_replays_after_consumer_crash(monkeypatch):
    """A terminal normalized flow survives a crash without another SDK call."""

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.extractor import agent_flow
    from app.extractor.cost import LLMRunBudget
    from app.llm.contracts import LLMPurpose, ResultStatus
    from app.llm.lifecycle import (
        AttemptCallbacks,
        BudgetScopeKind,
        LLMSemanticCandidateUnavailable,
        begin_attempt,
        classify_attempt_result,
        finalize_attempt,
        fingerprint_input,
        load_terminal_semantic_candidate,
        use_attempt_callbacks,
    )
    from app.models.findings import LLMBudgetScope, LLMCall, LLMProjectBudgetAccount
    from app.models.llm import LLMSemanticCandidate
    from app.testing.sqlite_polyglot import install_polyglot

    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content
            self.model = "claude-sonnet-4-6"
            self.error = None

    class ResultMessage:
        def __init__(self):
            self.is_error = False
            self.subtype = "success"
            self.stop_reason = "end_turn"

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    dispatches = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
        yield AssistantMessage([TextBlock(json.dumps(_valid_flow_payload()))])
        yield ResultMessage()

    fake_sdk = SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )
    monkeypatch.setattr(agent_flow, "is_agent_sdk_available", lambda: True)
    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid.uuid4()
    operation_namespace = uuid.uuid5(project_id, "flow:checkout")
    binding = fingerprint_input(
        "mnemos.flow.consumer.test.v1",
        str(project_id),
        "run-1",
        "revision-1",
        "checkout",
    )

    def callbacks_for(run_budget):  # noqa: ANN001
        async def start(metadata):  # noqa: ANN001
            async with factory() as session:
                return await begin_attempt(
                    session,
                    project_id=project_id,
                    analysis_run_id=None,
                    purpose=LLMPurpose.FLOW,
                    target_id="flow:checkout",
                    level=agent_flow.FLOW_LEVEL,
                        run_budget=run_budget,
                        scope_kind=BudgetScopeKind.REQUEST,
                        metadata=metadata,
                        pre_reserved=None,
                    )

        async def finish(ticket, outcome):  # noqa: ANN001
            async with factory() as session:
                await finalize_attempt(session, ticket=ticket, outcome=outcome)

        async def finish_candidate(ticket, outcome, candidate):  # noqa: ANN001
            async with factory() as session:
                await finalize_attempt(
                    session,
                    ticket=ticket,
                    outcome=outcome,
                    candidate=candidate,
                )

        async def replay_candidate(request):  # noqa: ANN001
            async with factory() as session:
                return await load_terminal_semantic_candidate(
                    session,
                    operation_id=request.operation_id,
                    attempt_no=request.attempt_no,
                    project_id=project_id,
                    purpose=LLMPurpose.FLOW,
                    input_fingerprint=request.input_fingerprint,
                    contract_name=request.contract_name,
                    binding_fingerprint=request.binding_fingerprint,
                    normalizer=request.normalizer,
                )

        return AttemptCallbacks(
            start=start,
            finish=finish,
            finish_candidate=finish_candidate,
            replay_candidate=replay_candidate,
            mark_provider_dispatch=lambda: None,
        )

    def budget() -> LLMRunBudget:
        return LLMRunBudget(
            max_calls=1,
            max_input_tokens=1_000_000,
            max_output_tokens=128_000,
            wall_time_sec=60,
        )

    sources = [
        {
            "tier": "backend",
            "language": "python",
            "label": "checkout.py",
            "code": "def checkout():\n    return True\n",
        }
    ]
    first_budget = budget()
    first_state = agent_flow.FlowAttemptState()
    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with use_attempt_callbacks(callbacks_for(first_budget)):
            first = await agent_flow.analyze_flow_via_agent_sdk(
                entry="checkout",
                sources=sources,
                run_budget=first_budget,
                attempt_state=first_state,
                operation_id=operation_namespace,
                semantic_binding_identity=binding,
            )
    assert first is not None
    assert first_state.result_status == ResultStatus.PENDING

    # No classify call: this is the exact crash window between terminal
    # candidate commit and consumer publication/classification.
    retry_budget = budget()
    retry_state = agent_flow.FlowAttemptState()
    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with use_attempt_callbacks(callbacks_for(retry_budget)):
            replayed = await agent_flow.analyze_flow_via_agent_sdk(
                entry="checkout",
                sources=sources,
                run_budget=retry_budget,
                attempt_state=retry_state,
                operation_id=operation_namespace,
                semantic_binding_identity=binding,
            )
    assert replayed == first
    assert dispatches == 1
    assert retry_budget.calls_started == 0
    assert retry_state.attempt_id == first_state.attempt_id
    assert retry_state.result_status == ResultStatus.PENDING
    assert retry_state.attempt_id is not None
    async with factory() as session:
        await classify_attempt_result(
            session,
            attempt_id=retry_state.attempt_id,
            result_status=ResultStatus.ACCEPTED,
        )

    changed_budget = budget()
    with patch.dict(sys.modules, {"claude_agent_sdk": fake_sdk}):
        async with use_attempt_callbacks(callbacks_for(changed_budget)):
            with pytest.raises(LLMSemanticCandidateUnavailable):
                await agent_flow.analyze_flow_via_agent_sdk(
                    entry="checkout",
                    sources=sources,
                    run_budget=changed_budget,
                    operation_id=operation_namespace,
                    semantic_binding_identity=fingerprint_input("changed-binding"),
                )
    assert dispatches == 1
    assert changed_budget.calls_started == 0
    async with factory() as session:
        rows = (await session.execute(select(LLMCall))).scalars().all()
        candidates = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalars().all()
    assert len(rows) == 1
    assert len(candidates) == 1
    assert rows[0].result_status == ResultStatus.ACCEPTED.value
    assert candidates[0].candidate_status == "accepted"
    await engine.dispose()

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


@pytest.mark.asyncio
async def test_snapshot_source_loader_dedupes_normalized_paths_before_dispatch(
    monkeypatch,
):
    from app.api import flow as flow_api
    from app.extractor.agent_flow import _pack_flow_sources
    from app.source_snapshot import GitSourceSnapshot

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    revision = "e" * 40
    snapshot = GitSourceSnapshot(
        run_id=run_id,
        revision=revision,
        mirrors=(),
        tree_prefix="",
    )
    reads: list[str] = []

    async def fake_read_project_file(_db, **kwargs):  # noqa: ANN001, ANN003
        reads.append(kwargs["file_path"])
        return {
            "run_id": str(run_id),
            "revision": revision,
            "path": kwargs["file_path"],
            "encoding": "utf-8",
            "content": "export function checkout() { return true; }\n",
        }

    monkeypatch.setattr(flow_api, "read_project_file", fake_read_project_file)
    sources, skipped = await flow_api._load_snapshot_sources(
        object(),
        project_id=project_id,
        snapshot=snapshot,
        project_paths=[
            "frontend/app.ts",
            "./frontend/app.ts",
            "frontend\\app.ts",
        ],
        max_file_bytes=1_000,
    )
    _packed, source_scope = _pack_flow_sources(
        sources,
        max_total_chars=1_000,
    )

    assert reads == ["frontend/app.ts"]
    assert [source["label"] for source in sources] == ["frontend/app.ts"]
    assert skipped == [
        "frontend/app.ts:duplicate_source_path",
        "frontend/app.ts:duplicate_source_path",
    ]
    assert source_scope["provided_files"] == ["frontend/app.ts"]
    assert [item["label"] for item in source_scope["files"]] == [
        "frontend/app.ts"
    ]
    assert len(set(source_scope["provided_files"])) == len(
        source_scope["provided_files"]
    )


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
