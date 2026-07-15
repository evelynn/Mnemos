"""PR-188 — the subscription chat provider retries the flaky SDK init.

The Agent SDK's control handshake ("initialize") intermittently times out
on a cold CLI subprocess and raises *before* any tokens stream — observed
as "Control request timeout: initialize" → a 503 on a large repo, while the
very next identical call answers in ~90s. ``_claude_subscription`` now
retries once on that transient failure (but not on a real content timeout,
which would just wait again).
"""

from __future__ import annotations

from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "app" / "api" / "llm_providers.py"


def test_subscription_retries_transient_init_failure():
    body = _SRC.read_text(encoding="utf-8")
    idx = body.find("async def _claude_subscription(")
    assert idx >= 0
    end = body.find("async def provider_chat(", idx)
    slab = body[idx:end if end > idx else idx + 3000]
    assert "for attempt in range(2):" in slab   # bounded retry, not infinite
    assert "except TimeoutError:" in slab        # real content timeout still handled
    assert "init attempt" in slab                # the transient-init retry log line
    assert "continue" in slab                    # transient path retries, doesn't return
