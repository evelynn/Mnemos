"""Claude Agent SDK summary lifecycle at the structured-output boundary."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import ModuleType

import pytest

from app.extractor.agent_sdk import (
    AGENT_SDK_BUDGETED_MODEL,
    AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS,
    AGENT_SDK_EMPTY_FAILURE,
    AGENT_SDK_ASSISTANT_FAILURE,
    AGENT_SDK_FINISH_FAILURE,
    AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS,
    AGENT_SDK_OUTPUT_FAILURE,
    AGENT_SDK_PARSE_FAILURE,
    AGENT_SDK_RESULT_FAILURE,
    AGENT_SDK_SCHEMA_FAILURE,
    AGENT_SDK_TIMEOUT_FAILURE,
    AGENT_SDK_TRANSPORT_FAILURE,
    AGENT_SDK_USAGE_FAILURE,
    MAX_AGENT_SUMMARY_OUTPUT_BYTES,
    AgentSDKAttemptState,
    AgentSDKAuthContract,
    AgentSDKOutputLimitError,
    AgentSDKPreDispatchError,
    AgentSDKResultError,
    AgentSDKTransportError,
    AgentSDKUsageError,
    _build_prompt,
    agent_sdk_isolation_options,
    summarize_via_agent_sdk,
)
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    ResultStatus,
    UsageSource,
    UsageStatus,
)
from app.llm.lifecycle import (
    AttemptCallbacks,
    AttemptTicket,
    fingerprint_input,
    use_attempt_callbacks,
)


class _TextBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _AssistantMessage:
    def __init__(
        self,
        content,  # noqa: ANN001
        *,
        model: str = AGENT_SDK_BUDGETED_MODEL,
        error: str | None = None,
    ) -> None:
        self.content = content
        self.model = model
        self.error = error


class _ResultMessage:
    def __init__(
        self,
        is_error: bool = False,
        *,
        stop_reason: str | None = "end_turn",
        subtype: str | None = "success",
    ) -> None:
        self.is_error = is_error
        self.usage = {
            "input_tokens": 7,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
            "output_tokens": 5,
        }
        self.total_cost_usd = 0.01
        self.num_turns = 1
        self.session_id = "session_adapter_1"
        self.subtype = subtype
        self.stop_reason = stop_reason


class _SystemMessage:
    def __init__(self, *, subtype: str = "init", data=None) -> None:  # noqa: ANN001
        self.subtype = subtype
        self.data = data


class _Options:
    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        self.kwargs = kwargs


def _sdk_module(query) -> ModuleType:  # noqa: ANN001
    module = ModuleType("claude_agent_sdk")
    module.TextBlock = _TextBlock  # type: ignore[attr-defined]
    module.AssistantMessage = _AssistantMessage  # type: ignore[attr-defined]
    module.ResultMessage = _ResultMessage  # type: ignore[attr-defined]
    module.SystemMessage = _SystemMessage  # type: ignore[attr-defined]
    module.ClaudeAgentOptions = _Options  # type: ignore[attr-defined]
    module.query = query  # type: ignore[attr-defined]
    return module


def _ticket(operation_id: uuid.UUID) -> AttemptTicket:
    return AttemptTicket(
        attempt_id=uuid.uuid4(),
        operation_id=operation_id,
        budget_scope_id=uuid.uuid4(),
        started_at=datetime.now(timezone.utc),
        remaining_seconds=30.0,
    )


def _valid_payload() -> dict:
    return {
        "summary": "Uses only bounded graph evidence.",
        "detailed": "No raw provider response enters the attempt ledger.",
        "claims": [],
        "open_questions": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key_source", "expected_provider_mode"),
    [
        ("none", "unknown"),
        ("ANTHROPIC_API_KEY", "api"),
    ],
)
async def test_agent_sdk_starts_with_exact_input_before_dispatch_and_finishes(
    monkeypatch,
    api_key_source,
    expected_provider_mode,
) -> None:
    events: list[str] = []
    starts = []
    finishes = []
    system_prompt = "Return the canonical Mnemos summary JSON."
    evidence = [{"kind": "node", "node_id": "sym:orders"}]
    options_seen = []

    async def query(**kwargs):
        events.append("provider")
        assert starts
        options_seen.append(kwargs["options"])
        yield _SystemMessage(data={"apiKeySource": api_key_source})
        yield _AssistantMessage([_TextBlock(json.dumps(_valid_payload()))])
        yield _ResultMessage(False)

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _sdk_module(query))
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)

    async def start(metadata):  # noqa: ANN001
        events.append("start")
        starts.append(metadata)
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        events.append("finish")
        finishes.append((ticket, outcome))

    state = AgentSDKAttemptState()
    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        mark_provider_dispatch=lambda: events.append("marker"),
    )
    async with use_attempt_callbacks(callbacks):
        parsed = await summarize_via_agent_sdk(
            model=AGENT_SDK_BUDGETED_MODEL,
            level=1,
            target_id="sym:orders",
            evidence=evidence,
            system_prompt=system_prompt,
            attempt_state=state,
        )

    assert events == ["start", "marker", "provider", "finish"]
    metadata = starts[0]
    prompt = _build_prompt(1, "sym:orders", evidence, 16_000)
    assert metadata.attempt_kind == AttemptKind.PRIMARY
    assert metadata.provider == "anthropic"
    assert metadata.provider_mode == "unknown"
    assert metadata.requested_model == AGENT_SDK_BUDGETED_MODEL
    assert metadata.input_fingerprint == fingerprint_input(system_prompt, prompt)
    assert (
        metadata.estimated_input_tokens
        == AGENT_SDK_MAX_PROVIDER_INPUT_TOKENS
        == 1_000_000
    )
    assert metadata.input_estimate_method == "model_context_ceiling_v1"
    assert (
        metadata.requested_max_output_tokens
        == AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
        == 128_000
    )
    assert metadata.usage_source == UsageSource.AGENT_SDK

    ticket, outcome = finishes[0]
    assert outcome.attempt_status == AttemptStatus.COMPLETED
    assert outcome.result_status == ResultStatus.PENDING
    assert outcome.usage.status == UsageStatus.REPORTED_COMPLETE
    assert outcome.usage.source == UsageSource.AGENT_SDK
    assert outcome.usage.input_tokens == 12
    assert outcome.usage.output_tokens == 5
    assert outcome.usage.total_tokens == 17
    assert outcome.usage.reported_cost_usd is None
    assert outcome.usage.estimated_cost_usd == Decimal("0.01")
    assert outcome.usage.pricing_version == (
        "claude-agent-sdk-0.2.118-client-estimate"
    )
    assert outcome.resolved_model == "claude-sonnet-4-6"
    assert outcome.provider_request_id is None
    assert outcome.finish_reason == "end_turn"
    assert outcome.provider_mode == expected_provider_mode
    assert state.attempt_id == ticket.attempt_id
    assert state.usage == outcome.usage
    assert state.failure_code is None
    assert state.resolved_model == "claude-sonnet-4-6"
    assert parsed == _valid_payload()
    # The canonical ledger metadata/state contain neither prompt nor output.
    assert system_prompt not in repr((metadata, outcome, state))
    assert parsed["detailed"] not in repr((metadata, outcome, state))
    isolation = options_seen[0].kwargs
    assert isolation["tools"] == []
    assert isolation["allowed_tools"] == []
    assert isolation["setting_sources"] == []
    assert isolation["skills"] == []
    assert isolation["mcp_servers"] == {}
    assert isolation["agents"] == {}
    assert isolation["plugins"] == []
    assert isolation["permission_mode"] == "dontAsk"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "case",
        "expected_attempt",
        "expected_result",
        "expected_failure",
        "expected_exception",
    ),
    [
        (
            "empty",
            AttemptStatus.COMPLETED,
            ResultStatus.EMPTY,
            AGENT_SDK_EMPTY_FAILURE,
            None,
        ),
        (
            "parse",
            AttemptStatus.COMPLETED,
            ResultStatus.PARSE_REJECTED,
            AGENT_SDK_PARSE_FAILURE,
            None,
        ),
        (
            "schema",
            AttemptStatus.COMPLETED,
            ResultStatus.SCHEMA_REJECTED,
            AGENT_SDK_SCHEMA_FAILURE,
            None,
        ),
        (
            "output",
            AttemptStatus.FAILED,
            ResultStatus.OUTPUT_LIMIT_REJECTED,
            AGENT_SDK_OUTPUT_FAILURE,
            AgentSDKOutputLimitError,
        ),
        (
            "result",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_RESULT_FAILURE,
            AgentSDKResultError,
        ),
        (
            "provider_output",
            AttemptStatus.COMPLETED,
            ResultStatus.OUTPUT_LIMIT_REJECTED,
            AGENT_SDK_OUTPUT_FAILURE,
            AgentSDKOutputLimitError,
        ),
        (
            "finish",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_FINISH_FAILURE,
            AgentSDKResultError,
        ),
        (
            "subtype",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_FINISH_FAILURE,
            AgentSDKResultError,
        ),
        (
            "assistant",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_ASSISTANT_FAILURE,
            AgentSDKResultError,
        ),
        (
            "model_mismatch",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_ASSISTANT_FAILURE,
            AgentSDKResultError,
        ),
        (
            "resolved_model_mismatch",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_ASSISTANT_FAILURE,
            AgentSDKResultError,
        ),
        (
            "model_missing",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_ASSISTANT_FAILURE,
            AgentSDKResultError,
        ),
        (
            "usage",
            AttemptStatus.COMPLETED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_USAGE_FAILURE,
            AgentSDKUsageError,
        ),
        (
            "transport",
            AttemptStatus.FAILED,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_TRANSPORT_FAILURE,
            AgentSDKTransportError,
        ),
        (
            "timeout",
            AttemptStatus.TIMEOUT,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_TIMEOUT_FAILURE,
            TimeoutError,
        ),
        (
            "timeout_after_init",
            AttemptStatus.TIMEOUT,
            ResultStatus.NOT_APPLICABLE,
            AGENT_SDK_TIMEOUT_FAILURE,
            TimeoutError,
        ),
    ],
)
async def test_agent_sdk_terminal_failures_finish_the_started_attempt(
    monkeypatch,
    case: str,
    expected_attempt: AttemptStatus,
    expected_result: ResultStatus,
    expected_failure: str,
    expected_exception: type[BaseException] | None,
) -> None:
    async def query(**_kwargs):
        if case == "transport":
            raise RuntimeError("raw provider failure must not enter the ledger")
        if case in {"timeout", "timeout_after_init"}:
            if case == "timeout_after_init":
                yield _SystemMessage(
                    data={"apiKeySource": "ANTHROPIC_API_KEY"}
                )
            await asyncio.sleep(1)
            return
        if case == "output":
            yield _AssistantMessage(
                [
                    _TextBlock(
                        "😀" * (MAX_AGENT_SUMMARY_OUTPUT_BYTES // 4 + 1)
                    )
                ]
            )
            return
        if case == "assistant":
            yield _AssistantMessage(
                [_TextBlock(json.dumps(_valid_payload()))],
                error="server_error",
            )
        elif case == "model_mismatch":
            yield _AssistantMessage(
                [_TextBlock(json.dumps(_valid_payload()))]
            )
            yield _AssistantMessage([], model="claude-other-model")
        elif case == "resolved_model_mismatch":
            yield _AssistantMessage(
                [_TextBlock(json.dumps(_valid_payload()))],
                model="claude-other-model",
            )
        elif case == "model_missing":
            yield _AssistantMessage(
                [_TextBlock(json.dumps(_valid_payload()))], model=""
            )
        elif case == "parse":
            yield _AssistantMessage([_TextBlock("not json: source is private")])
        elif case == "schema":
            yield _AssistantMessage([_TextBlock(json.dumps({"summary": 42}))])
        elif case not in {
            "empty",
            "result",
            "usage",
            "provider_output",
            "finish",
            "subtype",
            "assistant",
            "model_mismatch",
            "resolved_model_mismatch",
            "model_missing",
        }:
            raise AssertionError(f"unsupported test case: {case}")
        stop_reason = (
            "max_tokens"
            if case == "provider_output"
            else None
            if case == "finish"
            else "end_turn"
        )
        result = _ResultMessage(
            case == "result",
            stop_reason=stop_reason,
            subtype=("error_max_turns" if case == "subtype" else "success"),
        )
        if case == "usage":
            result.usage["input_tokens"] = "7"
        yield result

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _sdk_module(query))
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    finishes = []

    async def start(metadata):  # noqa: ANN001
        return _ticket(metadata.operation_id)

    async def finish(ticket, outcome):  # noqa: ANN001
        finishes.append((ticket, outcome))

    state = AgentSDKAttemptState()
    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        mark_provider_dispatch=lambda: None,
    )
    async with use_attempt_callbacks(callbacks):
        call = summarize_via_agent_sdk(
            model=AGENT_SDK_BUDGETED_MODEL,
            level=1,
            target_id="sym:failure",
            evidence=[],
            timeout_s=(
                0.01 if case in {"timeout", "timeout_after_init"} else 60
            ),
            attempt_state=state,
        )
        if expected_exception is None:
            assert await call is None
        else:
            with pytest.raises(expected_exception):
                await call

    assert len(finishes) == 1
    ticket, outcome = finishes[0]
    assert state.attempt_id == ticket.attempt_id
    assert state.failure_code == expected_failure
    assert outcome.attempt_status == expected_attempt
    assert outcome.result_status == expected_result
    assert outcome.failure_code == expected_failure
    assert outcome.usage.source == UsageSource.AGENT_SDK
    if case in {"output", "transport", "timeout", "timeout_after_init"}:
        assert outcome.usage.status == UsageStatus.LOST_AFTER_DISPATCH
    elif case == "usage":
        assert outcome.usage.status == UsageStatus.UNAVAILABLE
    else:
        assert outcome.usage.status == UsageStatus.REPORTED_COMPLETE
    assert "source is private" not in repr((outcome, state))
    assert "raw provider failure" not in repr((outcome, state))
    assert outcome.provider_mode == (
        "api" if case == "timeout_after_init" else "unknown"
    )
    if case == "assistant":
        assert outcome.resolved_model == AGENT_SDK_BUDGETED_MODEL


@pytest.mark.asyncio
async def test_agent_sdk_unknown_model_fails_before_start_or_dispatch(
    monkeypatch,
) -> None:
    dispatched = False
    starts = []

    async def query(**_kwargs):
        nonlocal dispatched
        dispatched = True
        yield _ResultMessage(False)

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _sdk_module(query))
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)

    async def start(metadata):  # noqa: ANN001
        starts.append(metadata)
        return _ticket(metadata.operation_id)

    async def finish(_ticket, _outcome):  # noqa: ANN001
        raise AssertionError("an unstarted attempt cannot finish")

    async with use_attempt_callbacks(AttemptCallbacks(start=start, finish=finish)):
        with pytest.raises(
            AgentSDKPreDispatchError, match="agent_sdk_model_unsupported"
        ):
            await summarize_via_agent_sdk(
                model="claude-future-with-unknown-output-ceiling",
                level=1,
                target_id="sym:unsupported-model",
                evidence=[],
            )

    assert starts == []
    assert dispatched is False


def test_installed_agent_sdk_dataclasses_expose_the_consumed_shape() -> None:
    sdk = pytest.importorskip("claude_agent_sdk")

    assistant = sdk.AssistantMessage(
        content=[sdk.TextBlock("{}")],
        model=AGENT_SDK_BUDGETED_MODEL,
        error=None,
        stop_reason="end_turn",
    )
    result = sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="shape-only",
        stop_reason="end_turn",
    )
    system = sdk.SystemMessage(
        subtype="init",
        data={"apiKeySource": "none"},
    )
    options = sdk.ClaudeAgentOptions(
        **agent_sdk_isolation_options(),
        permission_mode="dontAsk",
    )

    assert assistant.model == AGENT_SDK_BUDGETED_MODEL
    assert assistant.error is None
    assert result.stop_reason == "end_turn"
    assert not hasattr(result, "model")
    auth = AgentSDKAuthContract()
    auth.observe(system)
    assert auth.provider_mode == "unknown"
    assert options.tools == []
    assert options.setting_sources == []
    assert options.permission_mode == "dontAsk"
    assert options.strict_mcp_config is True
    assert options.stderr is not None

    # Exercise the pinned SDK's real argv builder.  Empty MCP configuration
    # alone emits no --mcp-config, so the strict flag is the ambient-config
    # exclusion boundary.
    from claude_agent_sdk._internal.transport.subprocess_cli import (
        SubprocessCLITransport,
    )

    transport = SubprocessCLITransport(prompt="{}", options=options)
    transport._cli_path = "claude"  # type: ignore[attr-defined]
    command = transport._build_command()  # type: ignore[attr-defined]
    assert "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == ""
    assert "--setting-sources=" in command
    assert "--no-session-persistence" in command


def test_agent_sdk_stderr_boundary_discards_raw_diagnostics(capsys) -> None:
    callback = agent_sdk_isolation_options()["stderr"]
    secret = "RAW_SOURCE_AND_PROVIDER_SECRET_MUST_NOT_ESCAPE"
    callback(secret)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


@pytest.mark.asyncio
async def test_installed_sdk_subprocess_env_overrides_content_telemetry(
    monkeypatch,
) -> None:
    sdk = pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk._internal.transport import subprocess_cli

    content_flags = {
        "OTEL_LOG_USER_PROMPTS",
        "OTEL_LOG_ASSISTANT_RESPONSES",
        "OTEL_LOG_TOOL_DETAILS",
        "OTEL_LOG_TOOL_CONTENT",
        "OTEL_LOG_RAW_API_BODIES",
        "DEBUG_CLAUDE_AGENT_SDK",
        "DEBUG",
        "DEBUG_SDK",
        "CLAUDE_DEBUG",
        "CLAUDE_CODE_TEE_SDK_STDOUT",
    }
    for name in content_flags:
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    captured: dict[str, object] = {}

    class _Process:
        stdin = None
        stdout = None
        stderr = None

    async def open_process(command, **kwargs):  # noqa: ANN001,ANN003
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return _Process()

    monkeypatch.setattr(subprocess_cli.anyio, "open_process", open_process)
    options = sdk.ClaudeAgentOptions(**agent_sdk_isolation_options())
    transport = subprocess_cli.SubprocessCLITransport("{}", options)
    transport._cli_path = "claude"  # type: ignore[attr-defined]
    await transport.connect()
    try:
        child_env = captured["env"]
        assert isinstance(child_env, dict)
        assert {name: child_env[name] for name in content_flags} == {
            name: "0" for name in content_flags
        }
        assert child_env["ANTHROPIC_LOG"] == "off"
    finally:
        subprocess_cli._ACTIVE_CHILDREN.discard(transport._process)
        transport._process = None  # type: ignore[attr-defined]


def test_agent_sdk_auth_contract_aggregates_conflicts_fail_closed() -> None:
    auth = AgentSDKAuthContract()
    auth.observe(_AssistantMessage([]))
    assert auth.provider_mode == "unknown"

    auth.observe(_SystemMessage(data={"apiKeySource": "none"}))
    assert auth.provider_mode == "unknown"
    auth.observe(_SystemMessage(data={}))
    assert auth.provider_mode == "unknown"

    source = "AWS_BEDROCK_CREDENTIALS_PRIVATE_SENTINEL"
    auth.observe(_SystemMessage(data={"apiKeySource": source}))
    assert auth.provider_mode == "unknown"
    assert source not in repr(auth)
    auth.observe(_SystemMessage(data={"apiKeySource": "none"}))
    assert auth.provider_mode == "unknown"

    api_auth = AgentSDKAuthContract()
    api_auth.observe(_SystemMessage(data={"apiKeySource": source}))
    api_auth.observe(
        _SystemMessage(data={"apiKeySource": "ANTHROPIC_API_KEY"})
    )
    assert api_auth.provider_mode == "api"
    api_auth.observe(_SystemMessage(data={"apiKeySource": "none"}))
    assert api_auth.provider_mode == "unknown"
