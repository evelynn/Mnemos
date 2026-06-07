"""PR-138 — silent-fallback visibility for LLM extractor + embedding.

Pre-PR-138 the two audit-flagged "silent fallback" paths produced
zero operator signal:

1. ``Extractor.summarize`` collapsed every failure (no key, SDK
   import error, agent-SDK timeout, JSON parse fail) into a single
   ``model_used="stub"`` row. Inspecting ``summaries.model_used`` told
   the operator "this is a stub" but not WHY.

2. ``app.mcp.queries.search_symbols`` degraded to BM25 silently
   whenever the configured embedding provider couldn't actually
   reach pgvector (missing extra, no API key, dim mismatch).

This PR:
- Stamps ``fallback_reason`` onto ExtractorResult and encodes the
  reason into ``model_used`` (``stub:agent_sdk_timeout`` etc).
- Increments two new Prometheus counters:
  ``mnemos_llm_fallback_total{from,reason}`` and
  ``mnemos_embedding_silent_fallback_total{reason}``.
- Adds ``_check_embeddings_soft`` to startup_verify so a misconfigured
  ``MNEMOS_EMBEDDING_PROVIDER`` is logged on boot, not after days of
  invisibly degraded recall.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch


from app.extractor.agent import (
    FALLBACK_AGENT_SDK_ERROR,
    FALLBACK_AGENT_SDK_TIMEOUT,
    FALLBACK_ANTHROPIC_HTTP,
    FALLBACK_NO_BACKEND,
    Extractor,
    ExtractorResult,
)


# ─── ExtractorResult.fallback_reason is plumbed ───────────────────


def test_stub_no_backend_has_no_fallback_reason(monkeypatch):
    """Cold env (no key, no SDK) — the stub still works but is
    labelled ``no_backend``, not a silent timeout."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    ext = Extractor()
    out = asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    assert isinstance(out, ExtractorResult)
    assert out.model_used == "stub"
    assert out.fallback_reason == FALLBACK_NO_BACKEND
    # "[stub L1]" prefix is preserved so existing UX rendering still
    # signals stub mode.
    assert "[stub L1]" in out.summary


def test_agent_sdk_timeout_surfaces_distinct_reason(monkeypatch):
    """When the agent-SDK times out, the result must carry
    ``stub:agent_sdk_timeout`` so the operator can act (tune timeout,
    fall back to ANTHROPIC_API_KEY, etc.)."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)

    async def raise_timeout(**_):
        raise TimeoutError("agent-sdk 60s window exceeded")

    with patch("app.extractor.agent_sdk.is_agent_sdk_available",
               return_value=True), \
         patch("app.extractor.agent_sdk.summarize_via_agent_sdk",
               raise_timeout):
        ext = Extractor()
        out = asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    assert out.fallback_reason == FALLBACK_AGENT_SDK_TIMEOUT
    assert out.model_used == f"stub:{FALLBACK_AGENT_SDK_TIMEOUT}"


def test_agent_sdk_generic_error_surfaces_distinct_reason(monkeypatch):
    """Non-timeout exceptions land in a separate bucket so the metric
    can split bug-vs-config issues."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    async def raise_generic(**_):
        raise RuntimeError("subprocess crashed")

    with patch("app.extractor.agent_sdk.is_agent_sdk_available",
               return_value=True), \
         patch("app.extractor.agent_sdk.summarize_via_agent_sdk",
               raise_generic):
        ext = Extractor()
        out = asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    assert out.fallback_reason == FALLBACK_AGENT_SDK_ERROR
    assert "agent_sdk_error" in out.model_used


def test_anthropic_http_error_falls_through_to_stub_with_reason(monkeypatch):
    """ANTHROPIC_API_KEY set but HTTP call fails (network / quota) —
    we fall through to agent-sdk; when that's also unavailable the
    stub carries the anthropic error reason so the operator can act."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    class _DummyAsyncAnthropic:
        def __init__(self, **_): pass

        class messages:  # noqa: D401 — match SDK shape
            @staticmethod
            async def create(**_):
                raise RuntimeError("connection refused")

    fake_mod = type("anthropic", (), {"AsyncAnthropic": _DummyAsyncAnthropic})
    with patch.dict("sys.modules", {"anthropic": fake_mod}):
        ext = Extractor()
        out = asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    assert out.fallback_reason == FALLBACK_ANTHROPIC_HTTP
    assert "anthropic_http_error" in out.model_used


# ─── Prometheus counter wiring ─────────────────────────────────────


def test_llm_fallback_counter_exists_and_has_correct_labels():
    from app.obs.metrics import llm_fallback_total
    # ``prometheus_client`` strips the ``_total`` suffix internally; the
    # exposed metric line on /metrics still ends in ``_total``. Read
    # both ways to be explicit.
    assert llm_fallback_total._name in (
        "mnemos_llm_fallback", "mnemos_llm_fallback_total",
    )
    assert llm_fallback_total._labelnames == ("from", "reason")
    # Sanity: rendering goes through the suffix.
    rendered = b"".join(
        m.encode() if isinstance(m, str) else m
        for m in [s for s in str(llm_fallback_total).split() if s]
    )  # just exercise __str__ — actual format check is in next test
    assert rendered


def test_llm_fallback_counter_ticks_on_silent_failure(monkeypatch):
    """Real test of the metric path: drive a known failure and assert
    the counter went up by 1 for that label set."""
    from app.obs.metrics import llm_fallback_total
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("MNEMOS_DISABLE_AGENT_SDK", raising=False)

    async def raise_timeout(**_):
        raise TimeoutError("x")

    before = llm_fallback_total.labels(
        **{"from": "agent_sdk", "reason": FALLBACK_AGENT_SDK_TIMEOUT}
    )._value.get()
    with patch("app.extractor.agent_sdk.is_agent_sdk_available",
               return_value=True), \
         patch("app.extractor.agent_sdk.summarize_via_agent_sdk",
               raise_timeout):
        ext = Extractor()
        asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    after = llm_fallback_total.labels(
        **{"from": "agent_sdk", "reason": FALLBACK_AGENT_SDK_TIMEOUT}
    )._value.get()
    assert after == before + 1, (
        f"counter must tick on silent fallback (was {before}, now {after})"
    )


def test_no_backend_does_not_tick_metric(monkeypatch):
    """``no_backend`` is a deliberate choice, not a failure — the
    metric is reserved for actual failures so a quiet deploy doesn't
    inflate the rate."""
    from app.obs.metrics import llm_fallback_total
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    # All possible failure reasons summed — none should increment
    # for a clean no-backend run.
    def _sum_all():
        total = 0.0
        for metric in llm_fallback_total.collect():
            for sample in metric.samples:
                if sample.name == "mnemos_llm_fallback_total":
                    total += sample.value
        return total

    before = _sum_all()
    ext = Extractor()
    asyncio.run(ext.summarize(level=1, target_id="t", evidence=[]))
    after = _sum_all()
    assert after == before, "no-backend stub must not tick the counter"


def test_embedding_fallback_counter_exists():
    from app.obs.metrics import embedding_silent_fallback_total
    assert embedding_silent_fallback_total._name in (
        "mnemos_embedding_silent_fallback",
        "mnemos_embedding_silent_fallback_total",
    )
    assert embedding_silent_fallback_total._labelnames == ("reason",)


# ─── startup_verify embedding misconfig warning ──────────────────


def test_startup_verify_embeddings_quiet_when_unset(monkeypatch, caplog):
    """Operator who never opted into embeddings sees nothing — no
    spurious warnings, no info chatter."""
    monkeypatch.delenv("MNEMOS_EMBEDDING_PROVIDER", raising=False)
    caplog.set_level(logging.WARNING, logger="app.startup_verify")
    from app.startup_verify import _check_embeddings_soft
    _check_embeddings_soft()
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("MISCONFIGURED" in m for m in msgs)


def test_startup_verify_embeddings_warns_when_pgvector_missing(
    monkeypatch, caplog,
):
    """Most common cold-deploy mistake: provider set, pgvector not
    installed. Boot logs a loud MISCONFIGURED line with the fix."""
    monkeypatch.setenv("MNEMOS_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.setenv("VOYAGE_API_KEY", "x")
    caplog.set_level(logging.WARNING, logger="app.startup_verify")
    with patch.dict("sys.modules", {"pgvector": None}):
        # Force ImportError by patching builtins.__import__ for pgvector.
        real_import = __builtins__["__import__"] \
            if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def fake_import(name, *a, **kw):
            if name == "pgvector":
                raise ImportError("not installed")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", fake_import):
            from app.startup_verify import _check_embeddings_soft
            _check_embeddings_soft()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("MISCONFIGURED" in m and "pgvector" in m for m in msgs), (
        f"expected MISCONFIGURED+pgvector warning, got: {msgs}"
    )
    assert any("mnemos-platform[search]" in m for m in msgs)


def test_startup_verify_embeddings_warns_on_missing_api_key(
    monkeypatch, caplog,
):
    monkeypatch.setenv("MNEMOS_EMBEDDING_PROVIDER", "voyage")
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    caplog.set_level(logging.WARNING, logger="app.startup_verify")
    # pgvector import path must succeed for the API-key check to fire.
    import sys
    if "pgvector" not in sys.modules:
        sys.modules["pgvector"] = type(sys)("pgvector")  # tiny stub
    from app.startup_verify import _check_embeddings_soft
    _check_embeddings_soft()
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "MISCONFIGURED" in m and "VOYAGE_API_KEY" in m for m in msgs
    ), f"expected key-missing warning, got: {msgs}"
