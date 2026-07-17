"""Strict provider boundary and graph-grounding tests for agent extraction."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
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

from app.extractor.agent_extract import (  # noqa: E402
    AGENT_EXTRACT_CANDIDATE_CONTRACT,
    AgentExtractContractError,
    AgentExtractSemanticBinding,
    extract_file_via_agent_sdk,
    normalize_agent_extract_candidate,
    normalize_agent_extract_payload,
    to_envelopes,
)
from app.llm.contracts import AttemptStatus, ResultStatus  # noqa: E402
from app.models.graph import Edge, Node  # noqa: E402
from app.orchestrator.jobs import (  # noqa: E402
    AnalyzerStageOutcome,
    _authoritative_agent_refresh_sources,
    _record_agent_envelopes,
    _restore_agent_totals_after_failure,
    _supersede_stale_graph_summaries,
)

pytestmark = pytest.mark.usefixtures("fake_llm_attempt_callbacks")


@pytest.fixture(autouse=True)
def _agent_stage_contract_lifecycle_mode(monkeypatch):
    """Keep opaque-SDK parser/replay tests independent of paid authority."""

    from app.orchestrator import jobs

    production_begin_attempt = jobs.begin_attempt

    async def generic_test_begin_attempt(*args, **kwargs):  # noqa: ANN002, ANN003
        assert kwargs.pop("require_atomic_dollar_reservation") is True
        return await production_begin_attempt(
            *args,
            **kwargs,
            require_atomic_dollar_reservation=False,
        )

    monkeypatch.setattr(
        jobs,
        "begin_attempt",
        generic_test_begin_attempt,
    )


def _attempt_ticket(*, remaining_seconds: float = 30.0) -> object:
    from app.llm.lifecycle import AttemptTicket

    return AttemptTicket(
        attempt_id=uuid.uuid4(),
        operation_id=uuid.uuid4(),
        budget_scope_id=uuid.uuid4(),
        started_at=datetime.now(tz=timezone.utc),
        remaining_seconds=remaining_seconds,
    )


def _attempt_metadata() -> object:
    from app.llm.contracts import AttemptKind, UsageSource
    from app.llm.lifecycle import AttemptStartMetadata, fingerprint_input

    return AttemptStartMetadata(
        operation_id=uuid.uuid4(),
        attempt_no=1,
        attempt_kind=AttemptKind.PRIMARY,
        provider="anthropic",
        provider_mode="unknown",
        requested_model="claude-sonnet-4-6",
        input_fingerprint=fingerprint_input("system", "prompt"),
        estimated_input_tokens=10,
        input_estimate_method="approx_json_v1",
        requested_max_output_tokens=128_000,
        usage_source=UsageSource.AGENT_SDK,
    )


def _semantic_binding() -> AgentExtractSemanticBinding:
    return AgentExtractSemanticBinding(
        project_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
    )


def _attempt_callbacks(
    *,
    start,
    finish,
    mark_dispatch=None,
    candidates=None,
):
    from app.llm.lifecycle import (
        AttemptCallbacks,
        LLMSemanticCandidateUnavailable,
    )

    async def finish_candidate(ticket, outcome, candidate):
        if candidates is not None:
            candidates.append(candidate)
        await finish(ticket, outcome)

    async def replay_candidate(_request):
        raise LLMSemanticCandidateUnavailable(
            "test callback has no replay candidate"
        )

    return AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
        mark_provider_dispatch=mark_dispatch,
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


def test_agent_extract_candidate_normalizer_is_exact_and_idempotent() -> None:
    raw = {
        "contract": AGENT_EXTRACT_CANDIDATE_CONTRACT,
        "language": "python",
        "result": _code_payload(),
    }
    before = json.loads(json.dumps(raw))

    canonical = normalize_agent_extract_candidate(raw, language="python")

    assert raw == before
    assert canonical == normalize_agent_extract_candidate(
        canonical,
        language="python",
    )
    with pytest.raises(AgentExtractContractError):
        normalize_agent_extract_candidate(
            {**raw, "language": "sql"},
            language="python",
        )
    with pytest.raises(AgentExtractContractError):
        normalize_agent_extract_candidate(
            {**raw, "unexpected": True},
            language="python",
        )


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


def _fake_agent_sdk(
    payload: dict,
    *,
    usage: dict | None = None,
    stop_reason: str | None = "end_turn",
    is_error: bool = False,
    assistant_models: list[str] | None = None,
    assistant_error: str | None = None,
    api_key_sources: list[object] | None = None,
    options_seen: list[dict] | None = None,
):
    class TextBlock:
        def __init__(self, text: str) -> None:
            self.text = text

    class AssistantMessage:
        def __init__(
            self,
            content: list[TextBlock],
            *,
            model: str,
            error: str | None = None,
        ) -> None:
            self.content = content
            self.model = model
            self.error = error

    class ResultMessage:
        def __init__(self, is_error: bool = False) -> None:
            self.is_error = is_error
            self.subtype = "success"
            self.stop_reason = stop_reason
            self.session_id = "agent-extract-session"
            if usage is not None:
                self.usage = usage
                self.num_turns = 1

    class SystemMessage:
        def __init__(self, data) -> None:  # noqa: ANN001
            self.subtype = "init"
            self.data = data

    class ClaudeAgentOptions:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            if options_seen is not None:
                options_seen.append(kwargs)

    async def query(**_kwargs):  # noqa: ANN003
        for source in api_key_sources or []:
            data = (
                source
                if isinstance(source, dict)
                else {"apiKeySource": source}
            )
            yield SystemMessage(data)
        models = assistant_models or ["claude-sonnet-4-6"]
        for index, model in enumerate(models):
            content = [TextBlock(json.dumps(payload))] if index == 0 else []
            yield AssistantMessage(
                content,
                model=model,
                error=assistant_error if index == 0 else None,
            )
        yield ResultMessage(is_error)

    return SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        SystemMessage=SystemMessage,
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
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
        )
    with patch.dict(sys.modules, {"claude_agent_sdk": _fake_agent_sdk({})}):
        rejected = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
        )

    assert accepted is not None
    assert accepted == normalize_agent_extract_payload(accepted, language="python")
    assert rejected is None


@pytest.mark.asyncio
async def test_provider_start_callback_failure_stops_before_dispatch(
    monkeypatch,
) -> None:
    from app.llm.lifecycle import LLMLifecycleError

    sdk = _fake_agent_sdk(_code_payload())
    original_query = sdk.query
    query_calls = 0

    async def counting_query(**kwargs):
        nonlocal query_calls
        query_calls += 1
        async for message in original_query(**kwargs):
            yield message

    async def reject_start(_metadata):
        raise LLMLifecycleError("sensitive-callback-detail")

    async def unused_finish(_ticket, _outcome):
        raise AssertionError("provider must not be dispatched or finalized")

    sdk.query = counting_query
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        with pytest.raises(LLMLifecycleError, match="sensitive-callback-detail"):
            await extract_file_via_agent_sdk(
                language="python",
                file_rel="a.py",
                code="def run(): pass",
                semantic_binding=_semantic_binding(),
                attempt_callbacks=_attempt_callbacks(
                    start=reject_start,
                    finish=unused_finish,
                ),
            )

    assert query_calls == 0


@pytest.mark.asyncio
async def test_candidate_capability_is_required_before_start_or_dispatch(
    monkeypatch,
) -> None:
    from app.llm.lifecycle import AttemptCallbacks, LLMLifecycleError

    sdk = _fake_agent_sdk(_code_payload())
    original_query = sdk.query
    starts = 0
    query_calls = 0

    async def start(_metadata):
        nonlocal starts
        starts += 1
        return _attempt_ticket()

    async def finish(_ticket, _outcome):
        return None

    async def counting_query(**kwargs):
        nonlocal query_calls
        query_calls += 1
        async for message in original_query(**kwargs):
            yield message

    sdk.query = counting_query
    callbacks = AttemptCallbacks(start=start, finish=finish)
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )

    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        with pytest.raises(LLMLifecycleError, match="candidate capabilities"):
            await extract_file_via_agent_sdk(
                language="python",
                file_rel="a.py",
                code="def run(): pass",
                semantic_binding=_semantic_binding(),
                attempt_callbacks=callbacks,
            )

    assert starts == 0
    assert query_calls == 0


@pytest.mark.asyncio
async def test_provider_start_commit_precedes_mock_sdk_dispatch(monkeypatch) -> None:
    from app.extractor.agent_extract import _EXTRACT_SYSTEM, _build_extract_prompt
    from app.extractor.agent_sdk import (
        AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS,
        AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
        agent_sdk_isolation_options,
    )
    from app.llm.contracts import AttemptStatus, ResultStatus, UsageSource
    from app.llm.lifecycle import fingerprint_input

    options_seen: list[dict] = []
    sdk = _fake_agent_sdk(_code_payload(), options_seen=options_seen)
    original_query = sdk.query
    events: list[str] = []
    metadata_seen = []
    outcomes = []
    candidates = []
    ticket = _attempt_ticket()
    semantic_binding = _semantic_binding()

    async def record_query(**kwargs):
        events.append("provider_query")
        async for message in original_query(**kwargs):
            yield message

    async def commit_budget(metadata):
        metadata_seen.append(metadata)
        events.append("attempt_started")
        return ticket

    async def finish_attempt(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)
        events.append("attempt_finished")

    def mark_dispatch() -> None:
        events.append("dispatch_authorized")

    sdk.query = record_query
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=semantic_binding,
            attempt_callbacks=_attempt_callbacks(
                start=commit_budget,
                finish=finish_attempt,
                mark_dispatch=mark_dispatch,
                candidates=candidates,
            ),
        )

    assert result is not None
    assert events == [
        "attempt_started",
        "dispatch_authorized",
        "provider_query",
        "attempt_finished",
    ]
    metadata = metadata_seen[0]
    prompt = _build_extract_prompt("python", "a.py", "def run(): pass")
    assert metadata.provider == "anthropic"
    assert metadata.provider_mode == "unknown"
    assert metadata.requested_model == "claude-sonnet-4-6"
    assert metadata.input_fingerprint == fingerprint_input(_EXTRACT_SYSTEM, prompt)
    assert metadata.operation_id == uuid.uuid5(
        semantic_binding.run_id,
        "\0".join(
            (
                "agent_extract",
                "agent:python:a.py",
                metadata.input_fingerprint,
            )
        ),
    )
    assert (
        metadata.estimated_input_tokens
        == AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS
        == 1_000_000
    )
    assert metadata.input_estimate_method == "model_context_ceiling_v1"
    assert (
        metadata.requested_max_output_tokens
        == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )
    assert metadata.usage_source == UsageSource.AGENT_SDK
    assert outcomes[0].attempt_status == AttemptStatus.COMPLETED
    assert outcomes[0].result_status == ResultStatus.PENDING
    assert outcomes[0].usage.source == UsageSource.AGENT_SDK
    assert outcomes[0].usage.total_tokens is None
    assert outcomes[0].resolved_model == "claude-sonnet-4-6"
    assert outcomes[0].provider_request_id is None
    assert outcomes[0].finish_reason == "end_turn"
    assert outcomes[0].provider_mode == "unknown"
    assert len(candidates) == 1
    assert candidates[0].contract_name == AGENT_EXTRACT_CANDIDATE_CONTRACT
    assert candidates[0].payload == {
        "contract": AGENT_EXTRACT_CANDIDATE_CONTRACT,
        "language": "python",
        "result": _code_payload(),
    }
    assert "def run(): pass" not in repr(candidates[0])
    expected_options = agent_sdk_isolation_options()
    expected_options.update(
        {
            "disallowed_tools": ["Bash", "Edit", "Write", "Read", "Task"],
            "system_prompt": _EXTRACT_SYSTEM,
            "max_turns": 1,
            "permission_mode": "dontAsk",
            "cwd": os.environ.get("MNEMOS_AGENT_SDK_CWD", "/tmp"),
            "model": "claude-sonnet-4-6",
        }
    )
    assert options_seen == [expected_options]


@pytest.mark.asyncio
async def test_agent_adapter_normalizes_reported_usage_on_same_attempt(
    monkeypatch,
) -> None:
    from app.llm.contracts import UsageSource, UsageStatus

    outcomes = []
    ticket = _attempt_ticket()
    sdk = _fake_agent_sdk(
        _code_payload(),
        usage={
            "input_tokens": 10,
            "cache_read_input_tokens": 2,
            "output_tokens": 5,
        },
    )

    async def start(_metadata):
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is not None
    usage = outcomes[0].usage
    assert usage.status == UsageStatus.REPORTED_COMPLETE
    assert usage.source == UsageSource.AGENT_SDK
    assert usage.input_tokens == 12
    assert usage.output_tokens == 5
    assert usage.total_tokens == 17


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key_sources", "expected_mode"),
    [
        (["none"], "unknown"),
        (["ANTHROPIC_API_KEY"], "api"),
        (["none", "ANTHROPIC_API_KEY"], "unknown"),
        ([{}], "unknown"),
    ],
)
async def test_agent_extract_observes_auth_mode_only_at_terminal_finish(
    monkeypatch,
    api_key_sources,
    expected_mode,
) -> None:
    starts = []
    outcomes = []
    ticket = _attempt_ticket()

    async def start(metadata):
        starts.append(metadata)
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    sdk = _fake_agent_sdk(
        _code_payload(), api_key_sources=api_key_sources
    )
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is not None
    assert starts[0].provider_mode == "unknown"
    assert outcomes[0].provider_mode == expected_mode


@pytest.mark.asyncio
async def test_agent_extract_timeout_finalizes_observed_api_auth(
    monkeypatch,
) -> None:
    outcomes = []
    ticket = _attempt_ticket()
    sdk = _fake_agent_sdk(_code_payload())

    async def slow_query(**_kwargs):  # noqa: ANN003
        yield sdk.SystemMessage(
            {"apiKeySource": "ANTHROPIC_API_KEY"}
        )
        await asyncio.sleep(1)

    async def start(_metadata):
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    sdk.query = slow_query
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            timeout_s=0.01,
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is None
    assert len(outcomes) == 1
    assert outcomes[0].attempt_status == AttemptStatus.TIMEOUT
    assert outcomes[0].provider_mode == "api"


@pytest.mark.asyncio
async def test_agent_extract_stream_limit_counts_utf8_bytes(monkeypatch) -> None:
    from app.extractor import agent_extract

    outcomes = []
    ticket = _attempt_ticket()
    sdk = _fake_agent_sdk(_code_payload())

    async def multibyte_query(**_kwargs):  # noqa: ANN003
        # Nine characters occupy 36 UTF-8 bytes and must breach a 32-byte cap.
        yield sdk.AssistantMessage(
            [sdk.TextBlock("😀" * 9)],
            model="claude-sonnet-4-6",
        )
        yield sdk.ResultMessage()

    async def start(_metadata):
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    sdk.query = multibyte_query
    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_extract, "MAX_AGENT_EXTRACT_OUTPUT_BYTES", 32)
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is None
    assert len(outcomes) == 1
    assert outcomes[0].attempt_status == AttemptStatus.FAILED
    assert outcomes[0].result_status == ResultStatus.OUTPUT_LIMIT_REJECTED
    assert outcomes[0].failure_code == "agent_extract_output_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "sdk_kwargs",
        "expected_attempt",
        "expected_result",
        "expected_failure",
        "expected_finish_reason",
    ),
    [
        (
            {"stop_reason": "max_tokens"},
            AttemptStatus.COMPLETED,
            ResultStatus.OUTPUT_LIMIT_REJECTED,
            "agent_extract_output_limit",
            "max_tokens",
        ),
        (
            {"stop_reason": None},
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            "agent_extract_finish_reason_invalid",
            None,
        ),
        (
            {"assistant_error": "server_error"},
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            "agent_extract_assistant_contract_invalid",
            "end_turn",
        ),
        (
            {"assistant_models": ["claude-sonnet-4-6", "claude-other"]},
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            "agent_extract_assistant_contract_invalid",
            "end_turn",
        ),
    ],
)
async def test_agent_adapter_rejects_non_authoritative_terminal_or_assistant(
    monkeypatch,
    sdk_kwargs,
    expected_attempt,
    expected_result,
    expected_failure,
    expected_finish_reason,
) -> None:
    outcomes = []
    ticket = _attempt_ticket()

    async def start(_metadata):
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    sdk = _fake_agent_sdk(_code_payload(), **sdk_kwargs)
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="private.py",
            code="def secret(): pass",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is None
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.attempt_status == expected_attempt
    assert outcome.result_status == expected_result
    assert outcome.failure_code == expected_failure
    assert outcome.finish_reason == expected_finish_reason
    assert "secret" not in repr(outcome)


@pytest.mark.asyncio
async def test_agent_adapter_finalizes_schema_fallback(monkeypatch) -> None:
    from app.llm.contracts import AttemptStatus, ResultStatus, UsageSource

    outcomes = []
    ticket = _attempt_ticket()

    async def start(_metadata):
        return ticket

    async def finish(finished_ticket, outcome):
        assert finished_ticket is ticket
        outcomes.append(outcome)

    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": _fake_agent_sdk({})}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(start=start, finish=finish),
        )

    assert result is None
    assert len(outcomes) == 1
    assert outcomes[0].attempt_status == AttemptStatus.COMPLETED
    assert outcomes[0].result_status == ResultStatus.SCHEMA_REJECTED
    assert outcomes[0].failure_code == "agent_extract_schema_rejected"
    assert outcomes[0].usage.source == UsageSource.AGENT_SDK


@pytest.mark.asyncio
async def test_unsupported_agent_model_is_refused_before_budget_or_provider(
    monkeypatch,
) -> None:
    sdk = _fake_agent_sdk(_code_payload())
    original_query = sdk.query
    query_calls = 0
    reservation_calls = 0

    async def counting_query(**kwargs):
        nonlocal query_calls
        query_calls += 1
        async for message in original_query(**kwargs):
            yield message

    async def reserve_budget(_metadata):
        nonlocal reservation_calls
        reservation_calls += 1
        return _attempt_ticket()

    async def finish_budget(_ticket, _outcome):
        raise AssertionError("unsupported model must not be finalized")

    sdk.query = counting_query
    monkeypatch.setattr(
        "app.extractor.agent_extract.is_agent_sdk_available", lambda: True
    )
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        result = await extract_file_via_agent_sdk(
            language="python",
            file_rel="a.py",
            code="def run(): pass",
            model="claude-unknown-output-ceiling",
            semantic_binding=_semantic_binding(),
            attempt_callbacks=_attempt_callbacks(
                start=reserve_budget,
                finish=finish_budget,
            ),
        )

    assert result is None
    assert reservation_calls == 0
    assert query_calls == 0


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
    assert "analysis_internal_failure" in caplog.text
    assert "RuntimeError" not in caplog.text
    assert "super-secret-source-fragment" not in caplog.text


@pytest.mark.asyncio
async def test_successful_graph_refresh_retires_only_prior_summaries() -> None:
    from app.models.findings import LLMCall, Summary
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMCall.__table__.create)
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
@pytest.mark.parametrize(
    ("crash_before_staging", "tamper"),
    [
        (False, None),
        (True, None),
        (False, "missing"),
        (False, "binding"),
        (False, "ciphertext"),
    ],
)
async def test_agent_stage_replays_terminal_candidate_without_redispatch(
    tmp_path, monkeypatch, caplog, crash_before_staging, tamper
) -> None:
    from app.extractor import agent_extract
    from app.extractor.agent_sdk import AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    from app.extractor.cost import LLMRunBudget, project_budget_exposure
    from app.llm.contracts import (
        AttemptStatus,
        LLMPurpose,
        ResultStatus,
        UsageSource,
        UsageStatus,
    )
    from app.models.findings import (
        LLMCall,
        LLMBudgetScope,
        LLMProjectBudgetAccount,
    )
    from app.models.llm import LLMSemanticCandidate
    from app.orchestrator import jobs
    from app.testing.sqlite_polyglot import install_polyglot

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    provider_calls: list[dict] = []
    ingest_calls = 0

    install_polyglot()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(LLMBudgetScope.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
        await connection.run_sync(LLMProjectBudgetAccount.__table__.create)
        await connection.run_sync(LLMSemanticCandidate.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

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

    sdk = _fake_agent_sdk(_code_payload())
    original_query = sdk.query

    async def observed_query(**kwargs):
        # A separate session must observe both the reservation and STARTED row
        # before provider body execution.  This is the durable E2 boundary.
        async with Session() as observer:
            scope = await observer.get(LLMBudgetScope, run_id)
            rows = (await observer.execute(select(LLMCall))).scalars().all()
        assert scope is not None
        assert scope.calls_reserved == 1
        assert len(rows) == 1
        assert rows[0].contract_version == 1
        assert rows[0].attempt_status == AttemptStatus.STARTED.value
        assert rows[0].result_status == ResultStatus.PENDING.value
        assert rows[0].usage_source == UsageSource.AGENT_SDK.value
        provider_calls.append(kwargs)
        async for message in original_query(**kwargs):
            yield message

    sdk.query = observed_query

    async def record_ok(*_args, **_kwargs):
        nonlocal ingest_calls
        ingest_calls += 1
        if crash_before_staging and ingest_calls == 1:
            raise RuntimeError("sensitive-post-candidate-crash")
        return 1, 0

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_extract, "discover_source_files", lambda *_a, **_k: [source])
    monkeypatch.setattr(jobs, "_record_agent_envelopes", record_ok)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        wall_time_sec=30,
        scope_id=run_id,
    )

    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        first_outcome = await jobs._run_agent_extraction_stage(
            object(),
            project_id,
            run_id,
            "python",
            str(tmp_path),
            1,
            {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
            10,
            run_budget=budget,
            seen_nodes=set(),
            seen_edges=set(),
        )
    async with Session() as observer:
        scope = await observer.get(LLMBudgetScope, run_id)
        first_rows = (await observer.execute(select(LLMCall))).scalars().all()
        first_candidates = (
            await observer.execute(select(LLMSemanticCandidate))
        ).scalars().all()

    if tamper is not None:
        async with Session() as tamper_session:
            stored = (
                await tamper_session.execute(select(LLMSemanticCandidate))
            ).scalar_one()
            if tamper == "missing":
                await tamper_session.delete(stored)
            elif tamper == "binding":
                stored.binding_fingerprint = "0" * 64
            elif tamper == "ciphertext":
                stored.payload_ciphertext = b"x" * len(
                    stored.payload_ciphertext
                )
            else:  # pragma: no cover - parameter contract
                raise AssertionError("unknown candidate tamper")
            await tamper_session.commit()

    # Even an already-exhausted restarted worker derives the same operation,
    # replays the terminal candidate, and traverses the same ingest path
    # without consulting the fresh-call budget or reserving anything.
    restarted_budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        max_output_tokens=128_000,
        wall_time_sec=600,
        scope_id=run_id,
    )
    restarted_budget.stop("run_call_limit_exceeded")
    with patch.dict(sys.modules, {"claude_agent_sdk": sdk}):
        restarted_outcome = await jobs._run_agent_extraction_stage(
            object(),
            project_id,
            run_id,
            "python",
            str(tmp_path),
            1,
            {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0},
            10,
            run_budget=restarted_budget,
            seen_nodes=set(),
            seen_edges=set(),
        )
    async with Session() as restart_session:
        restarted_scope = await restart_session.get(LLMBudgetScope, run_id)
        restarted_rows = (
            await restart_session.execute(select(LLMCall))
        ).scalars().all()
        restarted_candidates = (
            await restart_session.execute(select(LLMSemanticCandidate))
        ).scalars().all()
        exposure = await project_budget_exposure(
            restart_session, project_id
        )
    await engine.dispose()

    assert first_outcome.authoritative is (not crash_before_staging)
    assert scope is not None
    assert scope.calls_reserved == 1
    assert scope.input_tokens_reserved == budget.input_tokens_reserved
    assert (
        scope.output_tokens_reserved
        == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )
    assert len(provider_calls) == 1
    assert len(first_rows) == 1
    assert len(first_candidates) == 1
    row = first_rows[0]
    candidate = first_candidates[0]
    assert row.contract_version == 1
    assert row.analysis_run_id == run_id
    assert row.purpose == LLMPurpose.AGENT_EXTRACT.value
    assert row.target_id == "agent:python:source.py"
    assert "\\" not in row.target_id
    assert row.provider == "anthropic"
    assert row.provider_mode == "unknown"
    assert row.requested_max_output_tokens == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    assert row.attempt_status == AttemptStatus.COMPLETED.value
    assert row.result_status == (
        ResultStatus.PENDING.value
        if crash_before_staging
        else ResultStatus.ACCEPTED.value
    )
    assert row.usage_status == UsageStatus.UNAVAILABLE.value
    assert row.usage_source == UsageSource.AGENT_SDK.value
    assert row.input_tokens is None
    assert row.uncached_input_tokens is None
    assert row.output_tokens is None
    assert row.visible_output_tokens is None
    assert row.total_tokens is None
    assert row.cache_read_input_tokens is None
    assert row.cache_write_input_tokens is None
    assert row.reasoning_output_tokens is None
    assert row.tool_input_tokens is None
    assert row.provider_total_tokens is None
    assert row.provider_turn_count is None
    assert row.reported_cost_usd is None
    assert row.tokens_used is None
    assert candidate.attempt_id == row.id
    assert candidate.contract_name == AGENT_EXTRACT_CANDIDATE_CONTRACT
    assert candidate.candidate_status == (
        "candidate" if crash_before_staging else "accepted"
    )
    assert b"def run" not in candidate.payload_ciphertext
    assert exposure.unknown_provider_calls == 1
    assert exposure.unknown_provider_exposure_usd > 0
    assert budget.calls_started == 1
    assert budget.input_tokens_reserved > 0
    assert (
        budget.output_tokens_reserved
        == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )
    assert restarted_scope is not None
    assert restarted_scope.calls_reserved == 1
    assert (
        restarted_scope.output_tokens_reserved
        == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )
    assert restarted_scope.exhausted_reason is None
    assert restarted_outcome.authoritative is (tamper is None)
    assert restarted_outcome.records == (1 if tamper is None else 0)
    if tamper is None:
        assert "agent_attempt_replay_blocked" not in restarted_outcome.errors
    else:
        assert "agent_attempt_replay_blocked" in restarted_outcome.errors
    assert len(restarted_rows) == 1
    assert restarted_rows[0].id == row.id
    assert len(restarted_candidates) == (0 if tamper == "missing" else 1)
    if tamper is None:
        assert restarted_candidates[0].candidate_status == "accepted"
    assert ingest_calls == (2 if tamper is None else 1)
    assert restarted_budget.calls_started == 0
    assert restarted_budget.input_tokens_reserved == 0
    assert restarted_budget.output_tokens_reserved == 0
    assert restarted_budget.exhausted_reason == "run_call_limit_exceeded"
    assert "sensitive-post-candidate-crash" not in caplog.text


@pytest.mark.asyncio
async def test_agent_stage_run_budget_rejection_makes_no_provider_or_attempt(
    tmp_path, monkeypatch
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_calls: list[dict] = []

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

    async def fake_extract(**kwargs):
        callbacks = kwargs["attempt_callbacks"]
        await callbacks.start(_attempt_metadata())
        callbacks.mark_provider_dispatch()
        provider_calls.append(kwargs)
        return _code_payload()

    async def reject_begin(_session, **_kwargs):
        budget.stop("run_input_token_limit_exceeded")
        raise RunBudgetExceeded("run_input_token_limit_exceeded")

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(agent_extract, "discover_source_files", lambda *_a, **_k: [source])
    monkeypatch.setattr(agent_extract, "extract_file_via_agent_sdk", fake_extract)
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "begin_attempt", reject_begin)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2, max_input_tokens=2_000_000, wall_time_sec=30
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
    assert provider_calls == []
    assert budget.calls_started == 0
    assert budget.exhausted_reason == "run_input_token_limit_exceeded"


@pytest.mark.asyncio
async def test_agent_stage_durable_reservation_failure_dispatches_zero_calls(
    tmp_path, monkeypatch
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.llm.lifecycle import LLMLifecycleError
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_calls: list[dict] = []

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

    async def fake_extract(**kwargs):
        callbacks = kwargs["attempt_callbacks"]
        await callbacks.start(_attempt_metadata())
        callbacks.mark_provider_dispatch()
        provider_calls.append(kwargs)
        return _code_payload()

    async def reject_durable_reservation(_session, **_kwargs):
        raise LLMLifecycleError("sensitive-database-detail")

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", fake_extract
    )
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(
        jobs,
        "begin_attempt",
        reject_durable_reservation,
    )
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2,
        max_input_tokens=2_000_000,
        wall_time_sec=30,
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
    assert provider_calls == []
    assert budget.calls_started == 0
    assert budget.input_tokens_reserved == 0
    assert budget.output_tokens_reserved == 0
    assert budget.exhausted_reason == "durable_budget_reservation_failed"


@pytest.mark.asyncio
async def test_agent_stage_run_deadline_finalizes_started_attempt_once(
    tmp_path, monkeypatch
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.llm.contracts import AttemptStatus, ResultStatus, UsageStatus
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    sdk_timeouts: list[int] = []
    finalized = []
    ticket = _attempt_ticket(remaining_seconds=0.01)

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
        callbacks = kwargs["attempt_callbacks"]
        await callbacks.start(_attempt_metadata())
        callbacks.mark_provider_dispatch()
        await asyncio.sleep(10)
        return _code_payload()

    async def begin_with_tighter_deadline(_session, **_kwargs):
        budget.reserve(1_000_000, 128_000)
        return ticket

    async def capture_finalize(_session, *, ticket, outcome, **_kwargs):
        finalized.append((ticket, outcome))

    class DeadlineBudget(LLMRunBudget):
        def reserve(
            self,
            estimated_input_tokens: int,
            requested_output_tokens: int = 0,
        ) -> float:
            self.calls_started += 1
            self.input_tokens_reserved += estimated_input_tokens
            self.output_tokens_reserved += requested_output_tokens
            return 0.05

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", slow_extract
    )
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(
        jobs,
        "begin_attempt",
        begin_with_tighter_deadline,
    )
    monkeypatch.setattr(jobs, "finalize_attempt", capture_finalize)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = DeadlineBudget(
        max_calls=2, max_input_tokens=2_000_000, wall_time_sec=30
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
    # The outer timeout is rescheduled after durable start returns the DB
    # deadline; the adapter's direct-call fallback remains the stage ceiling.
    assert len(sdk_timeouts) == 1
    assert sdk_timeouts[0] >= 3_600
    assert len(finalized) == 1
    assert finalized[0][0] is ticket
    terminal = finalized[0][1]
    assert terminal.attempt_status == AttemptStatus.TIMEOUT
    assert terminal.result_status == ResultStatus.NOT_APPLICABLE
    assert terminal.failure_code == "run_deadline_exceeded"
    assert terminal.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    assert budget.calls_started == 1
    assert budget.exhausted_reason == "run_deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_fails", [False, True])
async def test_agent_stage_cancellation_finalizes_started_attempt_and_reraises(
    tmp_path, monkeypatch, caplog, finish_fails
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.llm.contracts import AttemptStatus, ResultStatus, UsageStatus
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    provider_started = asyncio.Event()
    finalized = []
    ticket = _attempt_ticket()

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
        callbacks = kwargs["attempt_callbacks"]
        await callbacks.start(_attempt_metadata())
        callbacks.mark_provider_dispatch()
        provider_started.set()
        await asyncio.sleep(10)
        return _code_payload()

    async def begin_ok(_session, **_kwargs):
        budget.reserve(1_000_000, 128_000)
        return ticket

    async def capture_finalize(_session, *, ticket, outcome, **_kwargs):
        finalized.append((ticket, outcome))
        if finish_fails:
            raise RuntimeError("sensitive-accounting-detail")

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", cancellable_extract
    )
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "begin_attempt", begin_ok)
    monkeypatch.setattr(jobs, "finalize_attempt", capture_finalize)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2, max_input_tokens=2_000_000, wall_time_sec=30
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

    assert len(finalized) == 1
    assert finalized[0][0] is ticket
    terminal = finalized[0][1]
    assert terminal.attempt_status == AttemptStatus.CANCELLED
    assert terminal.result_status == ResultStatus.NOT_APPLICABLE
    assert terminal.failure_code == "agent_extract_cancelled"
    assert terminal.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    assert budget.calls_started == 1
    if finish_fails:
        assert "analysis_internal_failure" in caplog.text
        assert "RuntimeError" not in caplog.text
        assert "sensitive-accounting-detail" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_started", [False, True])
async def test_agent_stage_adapter_exception_finalizes_only_owned_attempt(
    tmp_path, monkeypatch, provider_started
) -> None:
    from app.extractor import agent_extract
    from app.extractor.cost import LLMRunBudget
    from app.llm.contracts import AttemptStatus, ResultStatus, UsageStatus
    from app.orchestrator import jobs

    source = tmp_path / "source.py"
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    finalized = []
    ticket = _attempt_ticket()

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
            callbacks = kwargs["attempt_callbacks"]
            await callbacks.start(_attempt_metadata())
            callbacks.mark_provider_dispatch()
        raise RuntimeError("sensitive-provider-detail")

    async def begin_ok(_session, **_kwargs):
        budget.reserve(1_000_000, 128_000)
        return ticket

    async def capture_finalize(_session, *, ticket, outcome, **_kwargs):
        finalized.append((ticket, outcome))

    monkeypatch.setattr(agent_extract, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(
        agent_extract, "discover_source_files", lambda *_a, **_k: [source]
    )
    monkeypatch.setattr(
        agent_extract, "extract_file_via_agent_sdk", broken_adapter
    )
    monkeypatch.setattr(jobs, "_ensure_run_active", lambda _run_id: _async_none())
    monkeypatch.setattr(jobs, "begin_attempt", begin_ok)
    monkeypatch.setattr(jobs, "finalize_attempt", capture_finalize)
    monkeypatch.setattr(jobs, "SessionLocal", FakeSession)
    monkeypatch.setattr(jobs, "StageTracker", FakeStage)
    budget = LLMRunBudget(
        max_calls=2, max_input_tokens=2_000_000, wall_time_sec=30
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
        assert len(finalized) == 1
        assert finalized[0][0] is ticket
        terminal = finalized[0][1]
        assert terminal.attempt_status == AttemptStatus.FAILED
        assert terminal.result_status == ResultStatus.NOT_APPLICABLE
        assert terminal.failure_code == "agent_extract_failed"
        assert terminal.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    else:
        assert finalized == []
    assert budget.calls_started == (1 if provider_started else 0)
    assert budget.output_tokens_reserved == (128_000 if provider_started else 0)


async def _async_none() -> None:
    return None
