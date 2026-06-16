"""PR-178 — the Chat tab's multi-provider selector.

The operator picks which AI answers: OpenAI, Gemini, Claude Code
(subscription/API) or Atlas (hansol ai). Availability is derived purely
from env config, so these assertions run in CI with no real LLM call.
"""

import asyncio

import pytest

from app.api import llm_providers as lp

_KEYS = [
    "OPENAI_API_KEY", "OPENAI_MODEL",
    "GEMINI_API_KEY", "GEMINI_MODEL",
    "ATLAS_API_KEY", "ATLAS_BASE_URL", "ATLAS_MODEL",
    "ANTHROPIC_API_KEY",
]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Each test starts from no provider config and no subscription, then
    sets only what it needs."""
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")


def test_lists_all_four_providers():
    ids = [p["id"] for p in lp.available_providers()]
    assert ids == ["claudecode", "openai", "gemini", "atlas"]


def test_labels_name_the_four_ais():
    labels = {p["id"]: p["label"] for p in lp.available_providers()}
    assert "OpenAI" in labels["openai"]
    assert "Gemini" in labels["gemini"]
    assert "Atlas" in labels["atlas"]
    assert "Claude" in labels["claudecode"]


def test_nothing_configured_means_none_available():
    assert lp.any_provider_available() is False
    assert all(not p["available"] for p in lp.available_providers())
    # default still returns a stable id even with nothing configured.
    assert lp.default_provider() == "claudecode"


def test_openai_available_with_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert lp.is_provider_available("openai") is True
    # claudecode unavailable here → default is the first available one.
    assert lp.default_provider() == "openai"


def test_gemini_available_with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    assert lp.is_provider_available("gemini") is True


def test_atlas_needs_both_key_and_base_url(monkeypatch):
    monkeypatch.setenv("ATLAS_API_KEY", "a-test")
    assert lp.is_provider_available("atlas") is False  # base_url missing
    monkeypatch.setenv("ATLAS_BASE_URL", "https://atlas.hansol/v1")
    assert lp.is_provider_available("atlas") is True


def test_claudecode_available_with_anthropic_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert lp.is_provider_available("claudecode") is True
    assert lp.default_provider() == "claudecode"


def test_default_prefers_claudecode_when_multiple(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert lp.default_provider() == "claudecode"  # registry order leads


def test_blank_key_is_not_configured(monkeypatch):
    # A whitespace-only value must not count as configured.
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert lp.is_provider_available("openai") is False


def test_unknown_provider_not_available():
    assert lp.is_provider_available("bogus") is False


def test_provider_chat_unknown_returns_none():
    out = asyncio.run(
        lp.provider_chat("bogus", system="s", prompt="p", timeout_s=1)
    )
    assert out is None
