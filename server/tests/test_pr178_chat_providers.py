"""PR-178/179 — Chat provider selection logic.

The operator picks which AI answers: OpenAI, Gemini, bounded Anthropic API,
or Atlas (hansol ai). The availability/selection
helpers are pure functions over a resolved config dict, so these run in
CI with no DB and no real LLM call.
"""

import asyncio

from app.api import llm_providers as lp


def _cfg(**over) -> dict:
    base = {pid: {"api_key": None, "model": None, "base_url": None}
            for pid in lp.PROVIDER_ORDER}
    for pid, vals in over.items():
        base[pid].update(vals)
    return base


def test_lists_all_four_providers():
    ids = [p["id"] for p in lp.available_providers(_cfg())]
    assert ids == ["claudecode", "openai", "gemini", "atlas"]


def test_labels_name_the_four_ais():
    labels = {p["id"]: p["label"] for p in lp.available_providers(_cfg())}
    assert "OpenAI" in labels["openai"]
    assert "Gemini" in labels["gemini"]
    assert "Atlas" in labels["atlas"]
    assert "Claude" in labels["claudecode"]


def test_openai_available_with_key():
    cfg = _cfg(openai={"api_key": "sk-x"})
    assert lp.is_provider_available("openai", cfg) is True
    # claudecode has no API key → first available is openai.
    assert lp.default_provider(cfg) == "openai"


def test_gemini_available_with_key():
    assert lp.is_provider_available("gemini", _cfg(gemini={"api_key": "g"})) is True


def test_atlas_generation_remains_unavailable_even_when_configured():
    # Persisted Atlas settings are retained for a future provider contract,
    # but credentials cannot substitute for a provider-enforced token/cost
    # receipt. Generation therefore remains fail-disabled.
    assert lp.is_provider_available("atlas", _cfg(atlas={"api_key": "a"})) is False
    assert lp.is_provider_available(
        "atlas", _cfg(atlas={"api_key": "a", "base_url": "https://x/v1"})
    ) is False
    cfg = _cfg(atlas={"api_key": "a", "agent_id": "ag-1", "base_url": "https://x/v1"})
    assert lp.is_provider_available("atlas", cfg) is False
    capability = lp.output_token_limit_capability("atlas", cfg)
    assert capability == {
        "output_token_limit_enforcement": "unsupported",
        "output_token_limit_detail": lp.ATLAS_GENERATION_DISABLED_REASON,
    }


def test_claudecode_available_with_key():
    cfg = _cfg(claudecode={"api_key": "sk-ant"})
    assert lp.is_provider_available("claudecode", cfg) is True
    assert lp.default_provider(cfg) == "claudecode"


def test_claudecode_legacy_subscription_mode_is_not_advertised():
    cfg = _cfg(claudecode={"api_key": "sk-ant", "mode": "subscription"})
    assert lp.is_provider_available("claudecode", cfg) is False


def test_claudecode_has_no_agent_sdk_availability_path():
    assert not hasattr(lp, "is_agent_sdk_available")
    assert not hasattr(lp, "_claude_subscription")
    assert lp.is_provider_available("claudecode", _cfg()) is False
    provider = next(
        item for item in lp.available_providers(_cfg()) if item["id"] == "claudecode"
    )
    assert "API 키 필수" in provider["label"]


def test_default_prefers_claudecode_when_multiple():
    cfg = _cfg(claudecode={"api_key": "a"}, openai={"api_key": "b"})
    assert lp.default_provider(cfg) == "claudecode"


def test_nothing_configured():
    cfg = _cfg()
    assert lp.any_provider_available(cfg) is False
    assert all(not p["available"] for p in lp.available_providers(cfg))
    # default still returns a stable id.
    assert lp.default_provider(cfg) == "claudecode"


def test_unknown_provider_not_available():
    assert lp.is_provider_available("bogus", _cfg()) is False


def test_provider_chat_unknown_returns_none():
    out = asyncio.run(
        lp.provider_chat("bogus", _cfg(), system="s", prompt="p", timeout_s=1)
    )
    assert out is None


def test_secret_label_maps_claudecode_to_anthropic():
    assert lp.secret_label("claudecode") == "chat-provider:anthropic"
    assert lp.secret_label("openai") == "chat-provider:openai"
    assert lp.secret_label("atlas") == "chat-provider:atlas"


def test_suggested_models_are_current():
    assert lp.SUGGESTED_MODELS["openai"] == ["gpt-4o-2024-11-20"]
    assert any("gemini" in m for m in lp.SUGGESTED_MODELS["gemini"])
    assert any("claude" in m for m in lp.SUGGESTED_MODELS["claudecode"])
