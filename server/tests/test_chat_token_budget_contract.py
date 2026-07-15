"""Hard token/context boundaries for the on-demand source-analysis chat."""

from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-chat-token-budget")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.api import chat as chat_api
from app.api import graph_guard
from app.api import llm_providers as providers


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
                return {"choices": [{"message": {"content": "ok"}}]}
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

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
        await providers.provider_chat(
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
        )
        assert captured["max_tokens"] == 128
        assert "max_completion_tokens" not in captured
    else:
        await providers.provider_chat(
            "gemini",
            {"gemini": {"api_key": "test", "model": "mock"}},
            system="s",
            prompt="p",
            timeout_s=1,
            max_output_tokens=128,
        )
        assert captured["generationConfig"]["maxOutputTokens"] == 128


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


@pytest.mark.asyncio
async def test_anthropic_api_wire_payload_receives_purpose_limit(monkeypatch):
    captured = {}

    class Messages:
        async def create(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok")])

    class AsyncAnthropic:
        def __init__(self, **_kwargs):  # noqa: ANN003
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
    )
    assert result == "ok"
    assert captured["max_tokens"] == 1_200


def _fake_subscription_sdk(monkeypatch, blocks: list[str], calls: list[int]):
    class TextBlock:
        def __init__(self, text):  # noqa: ANN001
            self.text = text

    class AssistantMessage:
        def __init__(self, content):  # noqa: ANN001
            self.content = content

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):  # noqa: ANN003
            pass

    async def query(**_kwargs):  # noqa: ANN003
        calls.append(1)
        yield AssistantMessage([TextBlock(block) for block in blocks])

    monkeypatch.setattr(providers, "is_agent_sdk_available", lambda: True)
    monkeypatch.setitem(
        sys.modules,
        "claude_agent_sdk",
        SimpleNamespace(
            AssistantMessage=AssistantMessage,
            ClaudeAgentOptions=ClaudeAgentOptions,
            TextBlock=TextBlock,
            query=query,
        ),
    )


@pytest.mark.asyncio
async def test_subscription_client_ceiling_rejects_whole_answer_without_retry(
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
    )

    assert result is None
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_subscription_client_ceiling_allows_complete_bounded_answer(
    monkeypatch,
):
    calls: list[int] = []
    _fake_subscription_sdk(monkeypatch, ["first", "second"], calls)

    result = await providers._claude_subscription(
        system="s",
        prompt="p",
        timeout_s=1,
        requested_max_output_tokens=128,
    )

    assert result == "first\nsecond"
    assert len(calls) == 1


def test_client_enforced_provider_output_caps_are_explicit():
    assert (
        providers.output_token_limit_capability("atlas", {})["output_token_limit_enforcement"]
        == "client"
    )
    assert (
        providers.output_token_limit_capability("claudecode", {})["output_token_limit_enforcement"]
        == "client"
    )
    partial = providers.output_token_limit_capability(
        "claudecode", {"claudecode": {"api_key": "test", "mode": "api"}}
    )
    assert partial["output_token_limit_enforcement"] == "partial"


@pytest.mark.asyncio
async def test_atlas_rejects_complete_answer_above_client_character_ceiling(monkeypatch):
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
    )

    assert result is None
    assert responses == []


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
