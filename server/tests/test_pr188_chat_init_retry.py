"""Claude Chat must remain a bounded direct-API-only route."""

from __future__ import annotations

from pathlib import Path


_SRC = Path(__file__).resolve().parents[1] / "app" / "api" / "llm_providers.py"


def test_chat_has_no_agent_sdk_retry_or_fallback_path():
    body = _SRC.read_text(encoding="utf-8")
    assert "async def _claude_subscription(" not in body
    assert "claude_agent_sdk" not in body
    assert "agent_sdk_isolation_options" not in body
    assert "max_retries=0" in body
