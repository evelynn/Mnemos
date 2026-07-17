"""No external model dispatch is authorized without durable accounting."""

from __future__ import annotations

import ast
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api import llm_providers
from app.extractor.agent import Extractor
from app.extractor.agent_extract import extract_file_via_agent_sdk
from app.extractor.agent_flow import analyze_flow_via_agent_sdk
from app.extractor.agent_sdk import summarize_via_agent_sdk
from app.llm.lifecycle import LLMLifecycleError
from app.safety.review.second_opinion_provider import (
    FAILURE_ACCOUNTING,
    review_via_anthropic,
)


_SERVER_ROOT = Path(__file__).resolve().parents[1]
_PRODUCTION_BEGIN_ATTEMPT_FILES = (
    "app/api/chat.py",
    "app/api/flow.py",
    "app/extractor/runner.py",
    "app/orchestrator/jobs.py",
    "app/safety/review/second_opinion.py",
)


def test_every_production_begin_attempt_requires_atomic_dollar_authority():
    calls: list[tuple[str, ast.Call]] = []
    for path in (_SERVER_ROOT / "app").rglob("*.py"):
        relative_path = path.relative_to(_SERVER_ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls.extend(
            (relative_path, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "begin_attempt"
        )

    assert {relative_path for relative_path, _call in calls} == set(
        _PRODUCTION_BEGIN_ATTEMPT_FILES
    )
    assert len(calls) == len(_PRODUCTION_BEGIN_ATTEMPT_FILES)
    for relative_path, call in calls:
        requirement = next(
            (
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "require_atomic_dollar_reservation"
            ),
            None,
        )
        assert isinstance(requirement, ast.Constant), relative_path
        assert requirement.value is True, relative_path


def _fake_agent_sdk(dispatches: list[str]) -> SimpleNamespace:
    class ClaudeAgentOptions:
        def __init__(self, **_kwargs):
            pass

    class AssistantMessage:
        pass

    class ResultMessage:
        pass

    class TextBlock:
        pass

    async def query(**_kwargs):
        dispatches.append("agent_sdk")
        if False:  # pragma: no cover - keep this an async generator
            yield None

    return SimpleNamespace(
        AssistantMessage=AssistantMessage,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        TextBlock=TextBlock,
        query=query,
    )


@pytest.mark.asyncio
async def test_chat_refuses_before_private_provider_dispatch(monkeypatch):
    dispatches: list[str] = []

    async def provider(**_kwargs):
        dispatches.append("openai")
        return "should-not-run"

    monkeypatch.setattr(llm_providers, "_openai_compatible", provider)
    with pytest.raises(LLMLifecycleError, match="accounting capability"):
        await llm_providers.provider_chat(
            "openai",
            {
                "openai": {
                    "api_key": "test-key",
                    "model": "gpt-4o-2024-11-20",
                    "base_url": "https://api.openai.com/v1",
                }
            },
            system="bounded system",
            prompt="bounded prompt",
            operation_id=uuid.uuid4(),
        )
    assert dispatches == []


@pytest.mark.asyncio
async def test_direct_anthropic_summary_refuses_before_messages_create(monkeypatch):
    dispatches: list[str] = []

    class Messages:
        async def create(self, **_kwargs):
            dispatches.append("anthropic")

    class AsyncAnthropic:
        def __init__(self, **_kwargs):
            self.messages = Messages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    extractor = Extractor()

    with pytest.raises(LLMLifecycleError, match="accounting capability"):
        await extractor.summarize(1, "sym:bounded", [])
    assert dispatches == []


@pytest.mark.asyncio
async def test_agent_sdk_summary_refuses_before_query(monkeypatch):
    dispatches: list[str] = []
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_agent_sdk(dispatches))

    with pytest.raises(LLMLifecycleError, match="accounting capability"):
        await summarize_via_agent_sdk(
            model="claude-sonnet-4-6",
            level=1,
            target_id="sym:bounded",
            evidence=[],
        )
    assert dispatches == []


@pytest.mark.asyncio
async def test_agent_flow_refuses_before_query(monkeypatch):
    dispatches: list[str] = []
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_agent_sdk(dispatches))

    with pytest.raises(LLMLifecycleError, match="accounting capability"):
        await analyze_flow_via_agent_sdk(
            entry="checkout",
            operation_id=uuid.uuid4(),
            semantic_binding_identity="b" * 64,
            sources=[
                {
                    "label": "checkout.py",
                    "tier": "backend",
                    "language": "python",
                    "code": "def checkout():\n    return True\n",
                }
            ],
        )
    assert dispatches == []


@pytest.mark.asyncio
async def test_agent_extraction_refuses_before_query(monkeypatch):
    dispatches: list[str] = []
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", _fake_agent_sdk(dispatches))

    with pytest.raises(LLMLifecycleError, match="accounting capability"):
        await extract_file_via_agent_sdk(
            language="python",
            file_rel="bounded.py",
            code="def bounded():\n    return True\n",
        )
    assert dispatches == []


@pytest.mark.asyncio
async def test_second_opinion_refuses_before_client_initialization(monkeypatch):
    initializations: list[str] = []

    class AsyncAnthropic:
        def __init__(self, **_kwargs):
            initializations.append("anthropic")

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        SimpleNamespace(AsyncAnthropic=AsyncAnthropic),
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    result = await review_via_anthropic(prompt='{"review":"bounded"}')
    assert result.failure_code == FAILURE_ACCOUNTING
    assert initializations == []


@pytest.mark.asyncio
async def test_atlas_connection_test_never_creates_a_session(monkeypatch):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("Atlas connection test attempted network I/O")

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", ForbiddenClient)
    result = await llm_providers.test_provider(
        "atlas",
        {
            "atlas": {
                "api_key": "test-key",
                "agent_id": "agent-1",
                "base_url": "https://atlas.invalid/api",
            }
        },
    )
    assert result["ok"] is False
    assert "disabled" in result["message"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    [
        "https://compatible.example.invalid/v1",
        "http://api.openai.com/v1",
        "https://api.openai.com:8443/v1",
        "https://user@api.openai.com/v1",
        "https://api.openai.com/v2",
        "https://api.openai.com/v1?route=proxy",
    ],
)
async def test_unattested_openai_connection_test_never_sends_api_key(
    monkeypatch,
    base_url,
):
    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("unattested OpenAI probe attempted network I/O")

    monkeypatch.setattr(llm_providers.httpx, "AsyncClient", ForbiddenClient)
    result = await llm_providers.test_provider(
        "openai",
        {
            "openai": {
                "api_key": "test-key",
                "model": "gpt-4o-2024-11-20",
                "base_url": base_url,
            }
        },
    )
    assert result == {
        "ok": False,
        "message": "OpenAI base URL is not price-attested",
        "models": [],
    }
