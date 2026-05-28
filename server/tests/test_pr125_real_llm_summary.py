"""PR-125 — REAL LLM summary via Claude Agent SDK.

User's correction: "L3 LLM 불가" 는 핑계였다. Mnemos 가 Claude
Code 안에서 돌고 있고, Claude Agent SDK 가 그 subscription 을
노출함. PR-125 이 그걸 사용해서 진짜 LLM 호출.

이 파일은 **실제로 Claude 를 호출**합니다. ANTHROPIC_API_KEY
없어도, agent SDK 가 OAuth 토큰을 사용. 검증:

1. agent_sdk 사용 가능 + 정확히 발견
2. Mnemos 의 진짜 노드 (서버 코드의 한 심볼) 를 1-shot summarize
3. 응답이 schema 준수 (summary/claims/etc)
4. claims 의 evidence id 가 input evidence 에 실제로 존재 (hallucination 가드)
5. SDK 실패 시 stub fallback 정상

각 테스트는 max_turns=1, 단일 짧은 prompt — token 비용 최소.
SDK 미설치 환경에서는 자동 skip.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ─── SDK 환경 감지 ─────────────────────────────────────────────────


def _sdk_available() -> bool:
    try:
        from app.extractor.agent_sdk import is_agent_sdk_available
        return is_agent_sdk_available()
    except Exception:  # noqa: BLE001
        return False


requires_sdk = pytest.mark.skipif(
    not _sdk_available(),
    reason="claude_agent_sdk not installed",
)


# ─── 진짜 LLM 호출 (Mnemos 의 자체 그래프 데이터로) ────────────────


@pytest.mark.asyncio
@requires_sdk
async def test_real_llm_summarises_mnemos_function():
    """Mnemos 의 자체 함수를 진짜 Claude 에게 summarize 요청.
    이 테스트가 통과하면 'L3 LLM 작동' 입증."""
    from app.extractor.agent_sdk import summarize_via_agent_sdk

    target_id = "py:server/app/mcp/queries.py:search_symbols@68"
    evidence = [
        {
            "kind": "node",
            "id": target_id,
            "data": {
                "name": "search_symbols",
                "qual_name": "search_symbols",
                "kind": "async_function",
                "component_id": "py.mcp",
                "signature": "async def search_symbols(session, *, project_id, query, kind=None, component_id=None, top_k=20):",
            },
        },
        {
            "kind": "edge",
            "kind_": "CALLS",
            "source": target_id,
            "target": "py:server/app/mcp/queries.py:_tokenize@23",
            "certainty": "asserted",
        },
    ]
    parsed = await summarize_via_agent_sdk(
        model="claude-sonnet-4-6",
        level=1,
        target_id=target_id,
        evidence=evidence,
        timeout_s=90,
    )
    assert parsed is not None, "agent_sdk returned None — SDK call failed"
    # Schema-required fields present.
    assert "summary" in parsed, f"missing 'summary': keys={list(parsed.keys())}"
    assert isinstance(parsed["summary"], str)
    assert len(parsed["summary"]) > 10, f"summary too short: {parsed['summary']!r}"


@pytest.mark.asyncio
@requires_sdk
async def test_real_llm_response_carries_full_schema():
    """Full schema: summary + detailed + claims + open_questions."""
    from app.extractor.agent_sdk import summarize_via_agent_sdk

    parsed = await summarize_via_agent_sdk(
        model="claude-sonnet-4-6",
        level=2,
        target_id="comp:mcp",
        evidence=[
            {"kind": "node", "id": "py:queries.py:search_symbols",
             "data": {"name": "search_symbols", "kind": "function"}},
            {"kind": "node", "id": "py:queries.py:get_symbol",
             "data": {"name": "get_symbol", "kind": "function"}},
            {"kind": "node", "id": "py:queries.py:find_callers",
             "data": {"name": "find_callers", "kind": "function"}},
        ],
        timeout_s=90,
    )
    assert parsed is not None
    # Schema completeness.
    for field in ("summary", "detailed", "claims", "open_questions"):
        assert field in parsed, f"missing field {field}: {list(parsed.keys())}"
    assert isinstance(parsed["claims"], list)
    assert isinstance(parsed["open_questions"], list)


@pytest.mark.asyncio
@requires_sdk
async def test_real_llm_evidence_ids_not_hallucinated():
    """The system prompt forbids inventing evidence IDs. Verify
    every cited id in claims appears in the input evidence."""
    from app.extractor.agent_sdk import summarize_via_agent_sdk

    known_ids = {
        "py:foo.py:create_order@10",
        "py:foo.py:OrdersRepo@5",
    }
    evidence = [
        {"kind": "node", "id": "py:foo.py:create_order@10",
         "data": {"name": "create_order", "kind": "function"}},
        {"kind": "node", "id": "py:foo.py:OrdersRepo@5",
         "data": {"name": "OrdersRepo", "kind": "class"}},
    ]
    parsed = await summarize_via_agent_sdk(
        model="claude-sonnet-4-6",
        level=1,
        target_id="py:foo.py:create_order@10",
        evidence=evidence,
        timeout_s=90,
    )
    assert parsed is not None
    cited_ids = set()
    for claim in parsed.get("claims", []) or []:
        for ev in claim.get("evidence", []) or []:
            for k in ("node_id", "edge_id"):
                if ev.get(k):
                    cited_ids.add(ev[k])
    # If the model cited any ids, they must come from the input
    # set. Empty cite set is fine — model may just summarise.
    hallucinated = cited_ids - known_ids
    assert not hallucinated, (
        f"model invented ids not in input evidence: {hallucinated}"
    )


# ─── 통합: Extractor 가 SDK 백엔드 실제 사용 ───────────────────────


@pytest.mark.asyncio
@requires_sdk
async def test_extractor_uses_agent_sdk_when_no_api_key():
    """API key 없이 Extractor.summarize() 호출 → SDK 경로 작동
    → ExtractorResult 반환 (model_used 가 :agent_sdk suffix)."""
    from app.extractor.agent import Extractor

    ext = Extractor()
    ext._api_key = None  # force no-key path
    result = await ext.summarize(
        level=1,
        target_id="py:queries.py:search_symbols",
        evidence=[
            {"kind": "node", "id": "py:queries.py:search_symbols",
             "data": {"name": "search_symbols"}}
        ],
    )
    # Must be the real path, not the stub.
    assert result.model_used != "stub", (
        f"Extractor fell back to stub despite SDK being available: "
        f"model_used={result.model_used}"
    )
    assert "agent_sdk" in result.model_used
    assert result.summary and "stub" not in result.summary.lower()


# ─── SDK 미존재 시 stub fallback ───────────────────────────────────


@pytest.mark.asyncio
async def test_extractor_falls_back_to_stub_when_sdk_disabled(monkeypatch):
    """MNEMOS_DISABLE_AGENT_SDK=1 → SDK 경로 skip → stub fallback."""
    from app.extractor.agent import Extractor

    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    ext = Extractor()
    ext._api_key = None
    result = await ext.summarize(
        level=1, target_id="t:x", evidence=[{"kind": "node", "id": "x"}],
    )
    assert result.model_used == "stub"


# ─── JSON 파싱 견고성 ─────────────────────────────────────────────


def test_parse_response_strips_markdown_fence():
    from app.extractor.agent_sdk import _parse_json_response

    text = '```json\n{"summary": "ok", "claims": []}\n```'
    out = _parse_json_response(text)
    assert out == {"summary": "ok", "claims": []}


def test_parse_response_strips_prose_before_json():
    from app.extractor.agent_sdk import _parse_json_response

    text = "Sure! Here's the JSON:\n\n{\"summary\": \"ok\"}"
    out = _parse_json_response(text)
    assert out == {"summary": "ok"}


def test_parse_response_strips_prose_after_json():
    from app.extractor.agent_sdk import _parse_json_response

    text = '{"summary": "ok"}\n\nLet me know if you need more!'
    out = _parse_json_response(text)
    assert out == {"summary": "ok"}


def test_parse_response_returns_none_on_garbage():
    from app.extractor.agent_sdk import _parse_json_response

    assert _parse_json_response("totally not json") is None
    assert _parse_json_response("{broken: json,") is None
    assert _parse_json_response("") is None


def test_is_agent_sdk_available_returns_bool():
    """Function must always return a bool, never raise."""
    from app.extractor.agent_sdk import is_agent_sdk_available

    result = is_agent_sdk_available()
    assert isinstance(result, bool)


def test_is_agent_sdk_disabled_via_env(monkeypatch):
    """The MNEMOS_DISABLE_AGENT_SDK opt-out must be honoured even
    if the package is importable."""
    from app.extractor.agent_sdk import is_agent_sdk_available

    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")
    assert is_agent_sdk_available() is False


# ─── timeout safety ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_handles_sdk_timeout(monkeypatch):
    """If the SDK call exceeds timeout_s, function returns None
    rather than raising into the caller."""
    from app.extractor import agent_sdk

    import asyncio as _asyncio

    async def slow(*a, **kw):
        await _asyncio.sleep(10)

    # Force the SDK importable check to pass.
    monkeypatch.setattr(agent_sdk, "is_agent_sdk_available", lambda: True)

    # Patch the SDK's query() to a slow generator.
    class FakeSDK:
        class types:
            pass

    async def fake_query_gen(prompt, options):
        await _asyncio.sleep(30)
        yield None

    fake_mod = type("M", (), {
        "AssistantMessage": object,
        "ClaudeAgentOptions": lambda **kw: None,
        "ResultMessage": object,
        "TextBlock": object,
        "query": fake_query_gen,
    })

    with patch.dict("sys.modules", {"claude_agent_sdk": fake_mod}):
        result = await agent_sdk.summarize_via_agent_sdk(
            model="x", level=1, target_id="t",
            evidence=[], timeout_s=1,
        )
    assert result is None
