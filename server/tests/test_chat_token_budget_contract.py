"""Hard token/context boundaries for the on-demand source-analysis chat."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import claude_agent_sdk as claude_sdk
import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-chat-token-budget")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.api import chat as chat_api  # noqa: E402
from app.api import graph_guard  # noqa: E402
from app.api import llm_providers as providers  # noqa: E402

pytestmark = pytest.mark.usefixtures("fake_llm_attempt_callbacks")


def _leaf_dispatch_capability(
    *,
    provider: str,
    system: str = "s",
    prompt: str = "p",
    timeout_s: float = 5,
    max_output_tokens: int = 128,
):  # noqa: ANN202
    """Mint the same private leaf capability the durable owner supplies."""

    from app.llm.lifecycle import AttemptTicket

    return providers._issue_provider_dispatch_capability(
        AttemptTicket(
            attempt_id=uuid.uuid4(),
            operation_id=uuid.uuid4(),
            budget_scope_id=uuid.uuid4(),
            started_at=datetime.now(tz=timezone.utc),
            remaining_seconds=timeout_s,
        ),
        provider=provider,
        system=system,
        prompt=prompt,
        timeout_s=timeout_s,
        requested_max_output_tokens=max_output_tokens,
    )


def test_worst_case_history_is_packed_as_complete_messages_under_hard_cap():
    history = [
        chat_api.ChatMessage(role="assistant", content=chr(ord("A") + index) * 8_000)
        for index in range(20)
    ]

    prompt, metadata = chat_api._build_bounded_prompt(
        history,
        "c" * chat_api.CHAT_CONTEXT_MAX_CHARS,
        "q" * 4_000,
        system=chat_api._SYSTEM,
    )

    assert metadata["provider_input_chars"] <= chat_api.CHAT_PROMPT_MAX_CHARS
    assert metadata["actual_chars"] == len(prompt)
    assert metadata["history"]["provided_items"] == 20
    assert metadata["history"]["included_items"] == 1
    assert metadata["history"]["omitted_items"] == 19
    # The selected item is whole; no blind prefix of the preceding message is
    # smuggled in merely to fill the remaining budget.
    assert ("T" * 8_000) in prompt
    assert ("S" * 100) not in prompt


def test_context_budget_drops_code_or_whole_records_never_slices_them():
    overview = {
        "counts": {"Symbol": 2},
        "contracts": ["GET /" + "x" * 2_500, "GET /small"],
        "entities": ["orders"],
    }
    first = {
        "name": "checkout",
        "kind": "function",
        "file": "checkout.py",
        "line": 1,
        "signature": "def checkout():",
        "reads": [],
        "writes": ["orders"],
        "callers": 1,
        "callees": 2,
        "code": "z" * 20_000,
    }
    second = {
        "name": "persist",
        "kind": "function",
        "file": "orders.py",
        "line": 2,
        "signature": "def persist():",
        "reads": [],
        "writes": ["orders"],
        "callers": 1,
        "callees": 0,
    }

    text, included, metadata = chat_api._pack_chat_context(overview, [first, second])

    assert len(text) <= chat_api.CHAT_CONTEXT_MAX_CHARS
    assert metadata["actual_chars"] == len(text)
    assert metadata["truncated"] is True
    assert "z" * 100 not in text
    assert included[0]["name"] == "checkout"
    assert "code" not in included[0]
    # The oversized first contract is omitted as one item; it is not sliced.
    assert "GET /small" in text  # later complete facts still use remaining room
    assert "x" * 100 not in text


def test_oversized_symbol_metadata_does_not_block_later_small_record():
    oversized = {
        "name": "giant",
        "kind": "function",
        "file": "giant.py",
        "line": 1,
        "signature": "def " + "x" * 2_000,
        "reads": [],
        "writes": [],
        "callers": 0,
        "callees": 0,
    }
    small = {
        "name": "useful",
        "kind": "function",
        "file": "small.py",
        "line": 2,
        "signature": "def useful():",
        "reads": [],
        "writes": [],
        "callers": 0,
        "callees": 0,
    }

    text, included, metadata = chat_api._pack_chat_context(
        {"counts": {}, "contracts": [], "entities": []},
        [oversized, small],
        max_chars=500,
    )

    assert [item["name"] for item in included] == ["useful"]
    assert "def useful():" in text
    assert "x" * 100 not in text
    assert metadata["symbol_items_skipped_for_budget"] == 1
    assert metadata["omitted_symbol_items"] == 1


def test_source_excerpt_uses_complete_lines_only():
    text = "short\n" + "g" * 100 + "\nlast\n"
    excerpt, truncated = chat_api._complete_line_excerpt(text, 20)
    assert excerpt == "short"
    assert truncated is True
    giant, truncated = chat_api._complete_line_excerpt("g" * 100, 20)
    assert giant is None
    assert truncated is True


@pytest.mark.parametrize(
    "payload",
    [
        '["auth", "session", "token"]',
        '```json\n["auth", "session", "token"]\n```',
    ],
)
def test_rewrite_term_parser_accepts_only_canonical_bounded_dialects(payload):
    assert chat_api._parse_rewrite_terms(payload) == [
        "auth",
        "session",
        "token",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        'answer: ["auth", "session", "token"]',
        '["one", "two"]',
        json.dumps([str(i) for i in range(9)]),
        json.dumps(["ok", "still-ok", "x" * 65]),
        '["auth", 3, "token"]',
        '["Auth", "auth", "token"]',
        '{"terms": ["auth", "session", "token"]}',
    ],
)
def test_rewrite_term_parser_rejects_prose_bad_shape_density_and_size(payload):
    with pytest.raises(chat_api.RewriteTermsContractError):
        chat_api._parse_rewrite_terms(payload)


def test_mapped_korean_strong_static_hits_skip_rewrite():
    strong = [{"score": chat_api._WEAK_SCORE}]
    weak = [{"score": chat_api._WEAK_SCORE - 0.1}]
    assert not chat_api._weak_recall(
        "인증은 어떻게 동작해?",
        [{"score": 99}],
        static_expansion_hits=strong,
    )
    assert chat_api._weak_recall(
        "인증은 어떻게 동작해?",
        [{"score": 99}],
        static_expansion_hits=weak,
    )
    assert chat_api._weak_recall("도구 호출 차단", [{"score": 99}])


@pytest.mark.asyncio
async def test_llm_rewrite_mock_runs_through_parser_with_128_token_cap(monkeypatch):
    captured = {}

    async def fake_provider(*_args, **kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return '```json\n["beforeToolCall", "approval", "guard"]\n```'

    monkeypatch.setattr(chat_api, "provider_chat", fake_provider)
    terms = await chat_api._llm_search_terms(
        "도구 호출 차단",
        {"counts": {}, "contracts": ["x" * 10_000], "entities": []},
        "openai",
        {},
        180,
    )

    assert terms == ["beforeToolCall", "approval", "guard"]
    assert captured["max_output_tokens"] == chat_api.CHAT_REWRITE_MAX_OUTPUT_TOKENS
    assert len(captured["prompt"]) <= chat_api.REWRITE_PROMPT_MAX_CHARS


@pytest.mark.asyncio
async def test_llm_rewrite_never_logs_raw_provider_exception(
    monkeypatch,
    caplog,
):
    sentinel = "rewrite-provider-secret-and-prompt-sentinel"

    async def failing_provider(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(chat_api, "provider_chat", failing_provider)
    terms = await chat_api._llm_search_terms(
        "질문",
        {"counts": {}, "contracts": [], "entities": []},
        "anthropic",
        {},
        180,
    )

    assert terms == []
    assert "RuntimeError" in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_chat_mapped_korean_uses_one_answer_call_and_returns_budget_metadata(
    monkeypatch,
):
    calls: list[dict] = []
    search_calls = 0

    async def fake_config(_db):  # noqa: ANN001
        return {
            "openai": {
                "api_key": "test",
                "model": "mock",
                "base_url": "https://invalid.test/v1",
            }
        }

    async def fake_search(*_args, **_kwargs):  # noqa: ANN003
        nonlocal search_calls
        search_calls += 1
        score = 1.0 if search_calls == 1 else 20.0
        return [{"symbol_id": "sym:auth", "name": "authenticate", "score": score}]

    async def fake_provider(*_args, **kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return "grounded answer"

    async def fake_context(*_args, **_kwargs):  # noqa: ANN003
        return []

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(chat_api, "resolve_config", fake_config)
    monkeypatch.setattr(chat_api, "search_symbols", fake_search)

    async def overview(*_args, **_kwargs):  # noqa: ANN003
        return {"counts": {}, "contracts": [], "entities": []}

    monkeypatch.setattr(chat_api, "_project_overview", overview)
    monkeypatch.setattr(chat_api, "_build_context", fake_context)
    monkeypatch.setattr(chat_api, "provider_chat", fake_provider)
    monkeypatch.setattr(chat_api, "audit_record", fake_audit)

    result = await chat_api.chat(
        uuid.uuid4(),
        chat_api.ChatRequest(message="인증은 어떻게 동작해?", provider="openai"),
        SimpleNamespace(id=uuid.uuid4()),
        object(),
    )

    assert search_calls == 2  # original + free static expansion; no LLM rewrite search
    assert len(calls) == 1
    assert calls[0]["max_output_tokens"] == chat_api.CHAT_ANSWER_MAX_OUTPUT_TOKENS
    assert result["rewrite"]["attempted"] is False
    assert result["rewrite"]["skipped_by_strong_static_expansion"] is True
    assert result["truncation"]["prompt"]["actual_chars"] <= (chat_api.CHAT_PROMPT_MAX_CHARS)
    assert result["truncation"]["prompt"]["provider_input_chars"] <= (
        chat_api.CHAT_PROMPT_MAX_CHARS
    )
    assert result["output_budget"]["output_token_limit_enforcement"] == "provider"


@pytest.mark.asyncio
async def test_chat_rewrite_and_answer_share_one_physical_attempt_budget(monkeypatch):
    budget_ids: list[int] = []
    provider_calls = 0

    async def fake_config(_db):  # noqa: ANN001
        return {
            "openai": {
                "api_key": "test",
                "model": "mock",
                "base_url": "https://invalid.test/v1",
            }
        }

    async def fake_search(*_args, **_kwargs):  # noqa: ANN003
        return [{"symbol_id": "sym:auth", "name": "authenticate", "score": 0.1}]

    async def fake_provider(*_args, **kwargs):  # noqa: ANN003
        nonlocal provider_calls
        provider_calls += 1
        budget = kwargs["run_budget"]
        budget_ids.append(id(budget))
        budget.reserve(100, requested_output_tokens=kwargs["max_output_tokens"])
        if provider_calls == 1:
            return '["authenticate", "session", "token"]'
        return "grounded answer"

    async def fake_context(*_args, **_kwargs):  # noqa: ANN003
        return []

    async def fake_audit(**_kwargs):  # noqa: ANN003
        return None

    async def overview(*_args, **_kwargs):  # noqa: ANN003
        return {"counts": {}, "contracts": [], "entities": []}

    monkeypatch.setattr(chat_api, "resolve_config", fake_config)
    monkeypatch.setattr(chat_api, "search_symbols", fake_search)
    monkeypatch.setattr(chat_api, "_project_overview", overview)
    monkeypatch.setattr(chat_api, "_build_context", fake_context)
    monkeypatch.setattr(chat_api, "provider_chat", fake_provider)
    monkeypatch.setattr(chat_api, "audit_record", fake_audit)

    result = await chat_api.chat(
        uuid.uuid4(),
        chat_api.ChatRequest(message="explain the authorization lifecycle", provider="openai"),
        SimpleNamespace(id=uuid.uuid4()),
        object(),
    )

    assert provider_calls == 2
    assert len(set(budget_ids)) == 1
    assert result["request_budget"]["calls_started"] == 2
    assert result["request_budget"]["estimated_input_tokens"] == 200
    assert result["request_budget"]["reserved_output_tokens"] == (
        chat_api.CHAT_REWRITE_MAX_OUTPUT_TOKENS
        + chat_api.CHAT_ANSWER_MAX_OUTPUT_TOKENS
    )


@pytest.mark.asyncio
async def test_optional_rewrite_transport_failure_falls_back_without_classifying(
    monkeypatch,
):
    calls = 0
    classifications = []

    async def fake_config(_db):  # noqa: ANN001
        return {
            "openai": {
                "api_key": "test",
                "model": "mock",
                "base_url": "https://invalid.test/v1",
            }
        }

    async def fake_search(*_args, **_kwargs):  # noqa: ANN003
        return [{"symbol_id": "sym:auth", "name": "authenticate", "score": 0.1}]

    async def fake_provider(*_args, **kwargs):  # noqa: ANN003
        nonlocal calls
        calls += 1
        state = kwargs["attempt_state"]
        if calls == 1:
            state.attempt_id = uuid.uuid4()
            state.consumer_result_status = providers.ResultStatus.NOT_APPLICABLE
            state.consumer_failure_code = "chat_transport_error"
            return None
        return "grounded answer"

    async def fake_classify(*args, **kwargs):  # noqa: ANN002, ANN003
        classifications.append((args, kwargs))

    async def overview(*_args, **_kwargs):  # noqa: ANN003
        return {"counts": {}, "contracts": [], "entities": []}

    async def no_context(*_args, **_kwargs):  # noqa: ANN003
        return []

    async def no_audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(chat_api, "resolve_config", fake_config)
    monkeypatch.setattr(chat_api, "search_symbols", fake_search)
    monkeypatch.setattr(chat_api, "_project_overview", overview)
    monkeypatch.setattr(chat_api, "_build_context", no_context)
    monkeypatch.setattr(chat_api, "provider_chat", fake_provider)
    monkeypatch.setattr(chat_api, "_classify_chat_candidate", fake_classify)
    monkeypatch.setattr(chat_api, "audit_record", no_audit)

    result = await chat_api.chat(
        uuid.uuid4(),
        chat_api.ChatRequest(
            message="explain an unknown authorization lifecycle",
            provider="openai",
        ),
        SimpleNamespace(id=uuid.uuid4()),
        object(),
    )

    assert calls == 2
    assert classifications == []
    assert result["reply"] == "grounded answer"
    assert result["rewrite"]["terms"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "gemini"])
async def test_http_provider_wire_payload_receives_purpose_limit(monkeypatch, provider):
    captured = {}

    class Response:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

        def raise_for_status(self):
            return None

        def _payload(self):
            if provider == "openai":
                return {
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "ok"}}
                    ]
                }
            return {
                "responseId": "wire-gemini-id",
                "modelVersion": "mock",
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "ok"}]},
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 1,
                    "candidatesTokenCount": 1,
                    "totalTokenCount": 2,
                },
            }

        async def aiter_bytes(self):
            yield json.dumps(self._payload()).encode()

    class Client:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

        def stream(self, _method, _url, **kwargs):  # noqa: ANN003
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    if provider == "openai":
        result = await providers.provider_chat(
            "openai",
            {
                "openai": {
                    "base_url": "https://invalid.test/v1",
                    "api_key": "test",
                    "model": "mock",
                }
            },
            system="s",
            prompt="p",
            timeout_s=1,
            max_output_tokens=128,
            operation_id=uuid.uuid4(),
        )
        assert captured["max_tokens"] == 128
        assert "max_completion_tokens" not in captured
    else:
        result = await providers.provider_chat(
            "gemini",
            {"gemini": {"api_key": "test", "model": "mock"}},
            system="s",
            prompt="p",
            timeout_s=1,
            max_output_tokens=128,
            operation_id=uuid.uuid4(),
        )
        assert captured["generationConfig"]["maxOutputTokens"] == 128
    assert result == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "finish_reason", "expected"),
    [
        ("openai", "stop", "complete answer"),
        ("openai", "length", None),
        ("openai", "content_filter", None),
        ("openai", "tool_calls", None),
        ("openai", None, None),
        ("gemini", "STOP", "complete answer"),
        ("gemini", "MAX_TOKENS", None),
        ("gemini", "SAFETY", None),
        ("gemini", "RECITATION", None),
        ("gemini", "OTHER", None),
        ("gemini", None, None),
    ],
)
async def test_http_provider_leaf_terminal_matrix_uses_issued_capability(
    monkeypatch,
    provider,
    finish_reason,
    expected,
):
    post_calls = 0

    class Client:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

    async def post_json(*_args, **kwargs):  # noqa: ANN002, ANN003
        nonlocal post_calls
        post_calls += 1
        assert kwargs["timeout_s"] > 0
        if provider == "openai":
            return {
                "choices": [
                    {
                        "finish_reason": finish_reason,
                        "message": {"content": "complete answer"},
                    }
                ]
            }
        return {
            "candidates": [
                {
                    "finishReason": finish_reason,
                    "content": {"parts": [{"text": "complete answer"}]},
                }
            ]
        }

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setattr(providers, "_post_json_bounded", post_json)
    if provider == "openai":
        result = await providers._openai_compatible(
            base_url="https://api.openai.com/v1",
            api_key="test",
            model="gpt-4o",
            system="s",
            prompt="p",
            timeout_s=5,
            max_output_tokens=128,
            _dispatch=_leaf_dispatch_capability(provider="openai"),
        )
    else:
        result = await providers._gemini_generate(
            api_key="test",
            model="gemini-2.5-flash",
            system="s",
            prompt="p",
            timeout_s=5,
            max_output_tokens=128,
            _dispatch=_leaf_dispatch_capability(provider="gemini"),
        )

    assert result == expected
    assert post_calls == 1


@pytest.mark.asyncio
async def test_private_provider_leaf_rejects_invalid_capability_before_network(
    monkeypatch,
):
    from app.llm.lifecycle import LLMLifecycleError

    post_calls = 0

    async def forbidden_post(*_args, **_kwargs):  # noqa: ANN002, ANN003
        nonlocal post_calls
        post_calls += 1
        return {}

    monkeypatch.setattr(providers, "_post_json_bounded", forbidden_post)
    with pytest.raises(LLMLifecycleError, match="STARTED capability"):
        await providers._openai_compatible(
            base_url="https://api.openai.com/v1",
            api_key="test",
            model="gpt-4o",
            system="s",
            prompt="p",
            timeout_s=5,
            max_output_tokens=128,
            _dispatch=object(),  # type: ignore[arg-type]
        )

    assert post_calls == 0


@pytest.mark.asyncio
async def test_bounded_http_response_has_total_slow_drip_deadline():
    class Response:
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b'{"message":'
            await asyncio.sleep(0.05)
            yield b'"too late"}'

    class Client:
        def stream(self, _method, _url, **kwargs):  # noqa: ANN003
            assert kwargs["timeout"] == 0.01
            return Response()

    with pytest.raises(TimeoutError):
        await providers._post_json_bounded(
            Client(),
            "https://provider.invalid",
            max_bytes=1_024,
            timeout_s=0.01,
            headers={},
            payload={},
        )


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://api.openai.com/v1", True),
        ("https://api.openai.com:443/v1", True),
        ("HTTPS://API.OPENAI.COM/v1", True),
        ("http://api.openai.com/v1", False),
        ("https://api.openai.com:8443/v1", False),
        ("https://api.openai.com:0443/v1", False),
        ("https://user@api.openai.com/v1", False),
        ("https://api.openai.com/v1/", False),
        ("https://api.openai.com/v2", False),
        ("https://api.openai.com/v1?", False),
        ("https://api.openai.com/v1?route=proxy", False),
        ("https://api.openai.com/v1#fragment", False),
        ("https://api.openai.com.evil.invalid/v1", False),
        ("https://api.openai.com:not-a-port/v1", False),
    ],
)
def test_openai_price_attestation_requires_exact_official_https_root(
    base_url,
    expected,
):
    assert providers.is_price_attested_openai_chat_base_url(base_url) is expected


def test_openai_output_limit_field_uses_current_contract_for_official_and_reasoning_models():
    assert providers._openai_output_limit_field(
        base_url="https://api.openai.com/v1", model="gpt-4o"
    ) == "max_completion_tokens"
    assert providers._openai_output_limit_field(
        base_url="https://proxy.invalid/v1", model="o3"
    ) == "max_completion_tokens"
    assert providers._openai_output_limit_field(
        base_url="https://proxy.invalid/v1", model="legacy-model"
    ) == "max_tokens"
    assert providers._openai_output_limit_field(
        base_url="http://api.openai.com/v1", model="gpt-4o"
    ) == "max_tokens"


@pytest.mark.asyncio
async def test_anthropic_api_wire_payload_receives_purpose_limit(monkeypatch):
    captured = {}
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://override.invalid")

    class Messages:
        async def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return SimpleNamespace(
                id="wire-anthropic-id",
                model="mock",
                content=[SimpleNamespace(type="text", text="ok")],
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    class AsyncAnthropic:
        def __init__(self, **kwargs):  # noqa: ANN003
            captured["client_options"] = kwargs
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
    )
    result = await providers.provider_chat(
        "claudecode",
        {
            "claudecode": {
                "api_key": "test",
                "model": "mock",
                "mode": "api",
            }
        },
        system="s",
        prompt="p",
        timeout_s=1,
        max_output_tokens=1_200,
        operation_id=uuid.uuid4(),
    )
    assert result == "ok"
    assert captured["max_tokens"] == 1_200
    assert captured["client_options"] == {
        "api_key": "test",
        "base_url": "https://api.anthropic.com",
        "max_retries": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("end_turn", "complete answer"),
        ("max_tokens", None),
        ("refusal", None),
        ("pause_turn", None),
        ("tool_use", None),
        (None, None),
    ],
)
async def test_anthropic_api_leaf_terminal_matrix_uses_issued_capability(
    monkeypatch,
    stop_reason,
    expected,
):
    dispatches = 0
    client_options: list[dict] = []

    class Messages:
        async def create(self, **_kwargs):  # noqa: ANN003
            nonlocal dispatches
            dispatches += 1
            return SimpleNamespace(
                id="anthropic-terminal-id",
                model="claude-sonnet-4-6",
                content=[SimpleNamespace(type="text", text="complete answer")],
                stop_reason=stop_reason,
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    class AsyncAnthropic:
        def __init__(self, **kwargs):  # noqa: ANN003
            client_options.append(kwargs)
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
    )
    result = await providers._claude_api(
        api_key="test",
        model="claude-sonnet-4-6",
        system="s",
        prompt="p",
        timeout_s=5,
        max_output_tokens=128,
        _dispatch=_leaf_dispatch_capability(provider="anthropic"),
    )

    assert result == expected
    assert client_options == [
        {
            "api_key": "test",
            "base_url": "https://api.anthropic.com",
            "max_retries": 0,
        }
    ]
    assert dispatches == 1


@pytest.mark.asyncio
async def _removed_claude_api_fallback_cannot_reset_attempt_budget(monkeypatch):
    class Messages:
        async def create(self, **_kwargs):  # noqa: ANN003
            raise RuntimeError("remote dispatch failed")

    class AsyncAnthropic:
        def __init__(self, **_kwargs):  # noqa: ANN003
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
    )
    fallback_calls = 0

    async def fallback(**kwargs):  # noqa: ANN003
        nonlocal fallback_calls
        fallback_calls += 1
        kwargs["run_budget"].reserve(
            1,
            requested_output_tokens=kwargs["requested_max_output_tokens"],
        )
        return "must not run"

    monkeypatch.setattr(providers, "_claude_subscription", fallback)
    budget = providers.LLMRunBudget(
        max_calls=1,
        max_input_tokens=10_000,
        max_output_tokens=2_400,
        wall_time_sec=30,
    )

    result = await providers.provider_chat(
        "claudecode",
        {
            "claudecode": {
                "api_key": "test",
                "model": "mock",
                "mode": "api",
            }
        },
        system="s",
        prompt="p",
        timeout_s=5,
        max_output_tokens=1_200,
        run_budget=budget,
        operation_id=uuid.uuid4(),
    )

    assert result is None
    assert fallback_calls == 1
    assert budget.calls_started == 1
    assert budget.exhausted_reason == "run_call_limit_exceeded"


def _fake_subscription_sdk(
    monkeypatch,
    blocks: list[str],
    calls: list[int],
    *,
    options_seen: list[dict] | None = None,
):
    async def query(**kwargs):  # noqa: ANN003
        calls.append(1)
        if options_seen is not None:
            options = kwargs["options"]
            options_seen.append(
                {
                    name: getattr(options, name)
                    for name in (
                        "tools",
                        "allowed_tools",
                        "setting_sources",
                        "skills",
                        "mcp_servers",
                        "strict_mcp_config",
                        "agents",
                        "plugins",
                        "permission_mode",
                        "model",
                    )
                }
            )
        yield claude_sdk.AssistantMessage(
            content=[claude_sdk.TextBlock(text=block) for block in blocks],
            model=providers.AGENT_SDK_BUDGETED_MODEL,
        )
        yield _actual_agent_result(stop_reason="end_turn", is_error=False)

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude_sdk, "query", query)


def _agent_sdk_budget(*, max_calls: int = 1) -> providers.LLMRunBudget:
    return providers.LLMRunBudget(
        max_calls=max_calls,
        max_input_tokens=10_000,
        max_output_tokens=(
            max_calls * providers.AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
        ),
        wall_time_sec=30,
    )


def _actual_agent_result(*, stop_reason: str | None, is_error: bool):
    return claude_sdk.ResultMessage(
        subtype="error_during_execution" if is_error else "success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="test-session",
        stop_reason=stop_reason,
    )


@pytest.mark.asyncio
async def _removed_subscription_client_ceiling_rejects_whole_answer_without_retry(
    monkeypatch,
):
    calls: list[int] = []
    cap = 128 * providers._SUBSCRIPTION_CHARS_PER_TOKEN
    _fake_subscription_sdk(monkeypatch, ["x" * (cap + 1)], calls)

    result = await providers._claude_subscription(
        system="s",
        prompt="p",
        timeout_s=1,
        requested_max_output_tokens=128,
        run_budget=_agent_sdk_budget(),
    )

    assert result is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def _removed_subscription_client_ceiling_allows_complete_bounded_answer(
    monkeypatch,
):
    calls: list[int] = []
    options_seen: list[dict] = []
    _fake_subscription_sdk(
        monkeypatch,
        ["first", "second"],
        calls,
        options_seen=options_seen,
    )

    result = await providers._claude_subscription(
        system="s",
        prompt="p",
        timeout_s=1,
        requested_max_output_tokens=128,
        run_budget=_agent_sdk_budget(),
    )

    assert result == "first\nsecond"
    assert len(calls) == 1
    assert options_seen[0]["tools"] == []
    assert options_seen[0]["allowed_tools"] == []
    assert options_seen[0]["setting_sources"] == []
    assert options_seen[0]["skills"] == []
    assert options_seen[0]["mcp_servers"] == {}
    assert options_seen[0]["agents"] == {}
    assert options_seen[0]["plugins"] == []
    assert options_seen[0]["permission_mode"] == "dontAsk"
    assert options_seen[0]["model"] == providers.AGENT_SDK_BUDGETED_MODEL


@pytest.mark.asyncio
async def _removed_subscription_init_retry_reserves_each_physical_attempt(monkeypatch):
    calls = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("initialize failed before content")
        yield claude_sdk.AssistantMessage(
            content=[claude_sdk.TextBlock(text="ok")],
            model=providers.AGENT_SDK_BUDGETED_MODEL,
        )
        yield _actual_agent_result(stop_reason="end_turn", is_error=False)

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude_sdk, "query", query)
    budget = _agent_sdk_budget(max_calls=2)

    result = await providers._claude_subscription(
        model=providers.AGENT_SDK_BUDGETED_MODEL,
        system="s",
        prompt="p",
        timeout_s=5,
        requested_max_output_tokens=128,
        run_budget=budget,
    )

    assert result == "ok"
    assert calls == 2
    assert budget.calls_started == 2
    assert budget.output_tokens_reserved == (
        2 * providers.AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "stop_reason",
        "is_error",
        "emit_result",
        "assistant_error",
        "assistant_model",
        "expected",
    ),
    [
        ("end_turn", False, True, None, "claude-sonnet-4-6", "complete answer"),
        ("max_tokens", False, True, None, "claude-sonnet-4-6", None),
        ("refusal", False, True, None, "claude-sonnet-4-6", None),
        ("pause_turn", False, True, None, "claude-sonnet-4-6", None),
        ("tool_use", False, True, None, "claude-sonnet-4-6", None),
        (None, False, True, None, "claude-sonnet-4-6", None),
        ("end_turn", True, True, None, "claude-sonnet-4-6", None),
        ("end_turn", False, False, None, "claude-sonnet-4-6", None),
        ("end_turn", False, True, "server_error", "claude-sonnet-4-6", None),
        ("end_turn", False, True, None, "claude-other", None),
    ],
)
async def _removed_subscription_actual_terminal_matrix_has_one_reserved_dispatch(
    monkeypatch,
    stop_reason,
    is_error,
    emit_result,
    assistant_error,
    assistant_model,
    expected,
):
    dispatches = 0
    option_models: list[str | None] = []

    async def query(**kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
        option_models.append(kwargs["options"].model)
        yield claude_sdk.AssistantMessage(
            content=[claude_sdk.TextBlock(text="complete answer")],
            model=assistant_model,
            error=assistant_error,
        )
        if emit_result:
            yield _actual_agent_result(
                stop_reason=stop_reason,
                is_error=is_error,
            )

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude_sdk, "query", query)
    budget = _agent_sdk_budget()

    result = await providers._claude_subscription(
        model=providers.AGENT_SDK_BUDGETED_MODEL,
        system="s",
        prompt="p",
        timeout_s=1,
        requested_max_output_tokens=128,
        run_budget=budget,
    )

    assert result == expected
    assert dispatches == 1
    assert option_models == [providers.AGENT_SDK_BUDGETED_MODEL]
    assert budget.calls_started == 1
    assert (
        budget.output_tokens_reserved
        == providers.AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("response_kind", ["system", "assistant", "provider"])
async def _removed_subscription_never_retries_after_any_sdk_response(
    monkeypatch,
    response_kind,
):
    dispatches = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
        if response_kind == "system":
            yield claude_sdk.SystemMessage(
                subtype="init",
                data={"apiKeySource": "none"},
            )
        elif response_kind == "assistant":
            yield claude_sdk.AssistantMessage(
                content=[claude_sdk.TextBlock(text="partial")],
                model=providers.AGENT_SDK_BUDGETED_MODEL,
            )
        else:
            yield claude_sdk.UserMessage(content="provider envelope")
        raise RuntimeError("transport failed after response")

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude_sdk, "query", query)
    budget = _agent_sdk_budget(max_calls=2)

    result = await providers._claude_subscription(
        model=providers.AGENT_SDK_BUDGETED_MODEL,
        system="s",
        prompt="p",
        timeout_s=5,
        requested_max_output_tokens=128,
        run_budget=budget,
    )

    assert result is None
    assert dispatches == 1
    assert budget.calls_started == 1
    assert (
        budget.output_tokens_reserved
        == providers.AGENT_SDK_MAX_PROVIDER_OUTPUT_TOKENS
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_failure", ["missing_budget", "small_budget", "model"])
async def _removed_subscription_policy_rejects_before_reservation_and_dispatch(
    monkeypatch,
    policy_failure,
):
    dispatches = 0

    async def query(**_kwargs):  # noqa: ANN003
        nonlocal dispatches
        dispatches += 1
        yield claude_sdk.AssistantMessage(
            content=[claude_sdk.TextBlock(text="must not dispatch")],
            model=providers.AGENT_SDK_BUDGETED_MODEL,
        )

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setattr(claude_sdk, "query", query)
    budget = None
    model = providers.AGENT_SDK_BUDGETED_MODEL
    if policy_failure == "small_budget":
        budget = providers.LLMRunBudget(
            max_calls=1,
            max_input_tokens=10_000,
            max_output_tokens=1_200,
            wall_time_sec=30,
        )
    elif policy_failure == "model":
        budget = _agent_sdk_budget()
        model = "claude-opus-4-8"

    result = await providers._claude_subscription(
        model=model,
        system="s",
        prompt="p",
        timeout_s=5,
        requested_max_output_tokens=128,
        run_budget=budget,
    )

    assert result is None
    assert dispatches == 0
    if budget is not None:
        assert budget.calls_started == 0
        assert budget.output_tokens_reserved == 0


def test_provider_output_capability_is_direct_api_only_for_claude():
    atlas = providers.output_token_limit_capability("atlas", {})
    assert atlas["output_token_limit_enforcement"] == "unsupported"
    assert (
        atlas["output_token_limit_detail"]
        == providers.ATLAS_GENERATION_DISABLED_REASON
    )
    unavailable = providers.output_token_limit_capability("claudecode", {})
    assert unavailable["output_token_limit_enforcement"] == "unsupported"
    assert unavailable["output_token_limit_detail"] == "anthropic_api_mode_and_key_required"
    direct = providers.output_token_limit_capability(
        "claudecode", {"claudecode": {"api_key": "test", "mode": "api"}}
    )
    assert direct["output_token_limit_enforcement"] == "provider"
    assert direct["output_token_limit_detail"] == "anthropic_messages_max_tokens"


@pytest.mark.asyncio
async def test_atlas_public_generation_is_fail_disabled_before_network(monkeypatch):
    limit = 16
    responses = [
        {"id": "session-1"},
        {"message": "x" * (limit * providers._SUBSCRIPTION_CHARS_PER_TOKEN + 1)},
    ]

    class Response:
        headers = {}

        def __init__(self, payload):  # noqa: ANN001
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield json.dumps(self.payload).encode()

    class Client:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

        def stream(self, _method, _url, **_kwargs):  # noqa: ANN003
            return Response(responses.pop(0))

    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    result = await providers.provider_chat(
        "atlas",
        {
            "atlas": {
                "base_url": "https://atlas.invalid",
                "api_key": "test",
                "agent_id": "agent-1",
            }
        },
        system="s",
        prompt="p",
        timeout_s=1,
        max_output_tokens=limit,
        operation_id=uuid.uuid4(),
    )

    assert result is None
    assert len(responses) == 2


@pytest.mark.asyncio
async def test_atlas_two_steps_share_one_monotonic_absolute_deadline(monkeypatch):
    clock = iter([100.0, 100.0, 100.0, 101.0, 104.0])
    timeouts: list[float] = []
    urls: list[str] = []

    class Client:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

    async def post_json(_client, url, **kwargs):  # noqa: ANN001, ANN003
        urls.append(url)
        timeouts.append(kwargs["timeout_s"])
        if len(urls) == 1:
            return {"id": "session-1"}
        return {"message": "complete answer"}

    monkeypatch.setattr(providers, "monotonic", lambda: next(clock))
    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setattr(providers, "_post_json_bounded", post_json)

    result = await providers._atlas_chat(
        base_url="https://atlas.invalid",
        api_key="test",
        agent_id="agent-1",
        system="s",
        prompt="p",
        timeout_s=10,
        requested_max_output_tokens=128,
        _dispatch=_leaf_dispatch_capability(
            provider="atlas",
            timeout_s=10,
        ),
    )

    assert result == "complete answer"
    assert len(urls) == 2
    assert timeouts == [9.0, 6.0]


@pytest.mark.asyncio
async def test_atlas_does_not_dispatch_message_after_shared_deadline(monkeypatch):
    clock = iter([100.0, 100.0, 100.0, 101.0, 111.0])
    timeouts: list[float] = []
    urls: list[str] = []

    class Client:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):  # noqa: ANN002
            return False

    async def post_json(_client, url, **kwargs):  # noqa: ANN001, ANN003
        urls.append(url)
        timeouts.append(kwargs["timeout_s"])
        return {"id": "session-1"}

    monkeypatch.setattr(providers, "monotonic", lambda: next(clock))
    monkeypatch.setattr(providers.httpx, "AsyncClient", Client)
    monkeypatch.setattr(providers, "_post_json_bounded", post_json)

    result = await providers._atlas_chat(
        base_url="https://atlas.invalid",
        api_key="test",
        agent_id="agent-1",
        system="s",
        prompt="p",
        timeout_s=10,
        requested_max_output_tokens=128,
        _dispatch=_leaf_dispatch_capability(
            provider="atlas",
            timeout_s=10,
        ),
    )

    assert result is None
    assert len(urls) == 1
    assert timeouts == [9.0]


@pytest.mark.asyncio
async def test_provider_failures_do_not_log_raw_exception_text(monkeypatch, caplog):
    async def fail(**_kwargs):  # noqa: ANN003
        raise RuntimeError("source-secret-sentinel")

    monkeypatch.setattr(providers, "_openai_compatible", fail)
    result = await providers.provider_chat(
        "openai",
        {"openai": {"api_key": "test"}},
        system="s",
        prompt="p",
        operation_id=uuid.uuid4(),
    )

    assert result is None
    assert "source-secret-sentinel" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_project_chat_route_refuses_mixed_current_graph_snapshot():
    for route in chat_api.router.routes:
        dependency_functions = {dependency.dependency for dependency in route.dependencies}
        assert graph_guard.require_readable_current_graph in dependency_functions


@pytest.mark.asyncio
async def test_provider_rejects_invalid_output_limit_before_dispatch():
    with pytest.raises(ValueError, match="max_output_tokens"):
        await providers.provider_chat(
            "bogus",
            {},
            system="s",
            prompt="p",
            max_output_tokens=0,
        )


@pytest.mark.asyncio
async def test_provider_expired_durable_ticket_finishes_without_dispatch(monkeypatch):
    from app.llm.lifecycle import (
        AttemptCallbacks,
        AttemptTicket,
        LLMSemanticCandidateUnavailable,
        use_attempt_callbacks,
    )

    finished = []
    dispatches = 0

    async def start(metadata):  # noqa: ANN001
        return AttemptTicket(
            attempt_id=uuid.uuid4(),
            operation_id=metadata.operation_id,
            budget_scope_id=uuid.uuid4(),
            started_at=datetime.now(tz=timezone.utc),
            remaining_seconds=0.0,
        )

    async def finish(_ticket, outcome):  # noqa: ANN001
        finished.append(outcome)

    async def finish_candidate(*_args):  # noqa: ANN002
        raise AssertionError("an expired attempt cannot produce a candidate")

    async def replay_candidate(_request):  # noqa: ANN001
        raise LLMSemanticCandidateUnavailable("no replay")

    def mark_dispatch() -> None:
        nonlocal dispatches
        dispatches += 1

    async def forbidden_leaf(**_kwargs):  # noqa: ANN003
        raise AssertionError("an expired durable ticket cannot reach the network leaf")

    monkeypatch.setattr(providers, "_openai_compatible", forbidden_leaf)
    callbacks = AttemptCallbacks(
        start=start,
        finish=finish,
        finish_candidate=finish_candidate,
        replay_candidate=replay_candidate,
        mark_provider_dispatch=mark_dispatch,
    )
    async with use_attempt_callbacks(callbacks):
        result = await providers.provider_chat(
            "openai",
            {"openai": {"api_key": "test", "model": "mock"}},
            system="s",
            prompt="p",
            timeout_s=30,
            operation_id=uuid.uuid4(),
        )

    assert result is None
    assert dispatches == 0
    assert len(finished) == 1
    assert finished[0].attempt_status.value == "timeout"
    assert finished[0].failure_code == "chat_timeout"
    assert finished[0].usage.status.value == "unavailable"
