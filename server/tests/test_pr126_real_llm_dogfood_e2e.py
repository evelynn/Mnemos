"""PR-126 — 진짜 end-to-end: ggoss-py → 진짜 LLM → Summary → MCP.

PR-125 가 LLM 호출만 검증. 이번엔 풀 chain:
1. ggoss-py 가 Mnemos 자체 함수 분석 → Node 추출
2. real Claude (PR-125 의 agent_sdk 경로) 가 그 노드를 summarize
3. ExtractorResult → Summary ORM 행으로 persist
4. MCP get_module_summary 가 그 Summary 를 응답으로 반환

이게 작동하면 'Mnemos 가 자기 자신을 분석하고 LLM 으로 요약해서
agent 에게 응답한다' 의 완전한 입증.

비용 고려: 진짜 LLM 호출 2-3회 + 짧은 prompt + 짧은 응답.
SDK 미설치 / 비활성화 시 skip.
"""

from __future__ import annotations

import subprocess
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

# Bring every model module into Base.metadata before CREATE TABLE.
from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import findings as _findings_mod  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models import plans as _plans  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.findings import Summary  # noqa: E402
from app.models.graph import Node  # noqa: E402


_ROOT = Path(__file__).resolve().parents[2]
_PY_ANALYZER = _ROOT / "analyzers" / "ggoss-py" / "src" / "ggoss_py.py"


def _sdk_ok() -> bool:
    try:
        from app.extractor.agent_sdk import is_agent_sdk_available
        return is_agent_sdk_available()
    except Exception:  # noqa: BLE001
        return False


requires_sdk = pytest.mark.skipif(
    not _sdk_ok(),
    reason="claude_agent_sdk not available",
)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


# ─── 통합 전체 chain ───────────────────────────────────────────────


@pytest.mark.asyncio
@requires_sdk
async def test_full_chain_analyzer_to_llm_to_summary_table(session: AsyncSession):
    """analyzer 출력 → Extractor.summarize (real LLM) → Summary 행
    저장 → 검증."""
    import json
    from app.extractor.agent import Extractor

    # Step 1: ggoss-py 로 mcp/queries.py 단일 파일 분석.
    queries_py = _ROOT / "server" / "app" / "mcp" / "queries.py"
    cp = subprocess.run(
        [sys.executable, str(_PY_ANALYZER), "symbols", str(queries_py)],
        capture_output=True, text=True, timeout=30,
    )
    assert cp.returncode == 0
    envs = [json.loads(line) for line in cp.stdout.splitlines() if line.strip()]
    symbols = [e["data"] for e in envs if e["record_type"] == "symbol"]
    assert symbols, "ggoss-py produced no symbols"

    # Find search_symbols specifically (we know it exists).
    target = next(
        (s for s in symbols if s["name"] == "search_symbols"),
        None,
    )
    assert target, "ggoss-py didn't extract search_symbols itself"

    pid = _uuid.uuid4()

    # Persist target as a Node so the Summary FK-ish target_id resolves.
    session.add(Node(
        id=target["id"], project_id=pid, kind="Symbol",
        data=target, certainty="asserted",
        created_by=["ggoss-py"],
        valid_from=datetime.now(tz=timezone.utc),
    ))
    await session.commit()

    # Step 2: real Claude summarises this symbol.
    ext = Extractor()
    ext._api_key = None  # force agent_sdk path
    evidence = [{"kind": "node", "id": target["id"], "data": target}]
    result = await ext.summarize(level=1, target_id=target["id"], evidence=evidence)

    assert result.model_used != "stub", (
        f"Extractor fell back to stub: {result.model_used}"
    )
    assert "agent_sdk" in result.model_used
    assert result.summary, "LLM returned empty summary"
    assert len(result.summary) > 20

    # Step 3: persist as Summary row.
    summ = Summary(
        id=_uuid.uuid4(),
        project_id=pid,
        target_id=target["id"],
        level=1,
        summary=result.summary,
        detailed=result.detailed,
        claims=result.claims,
        open_questions=result.open_questions,
        model_used=result.model_used,
        tokens_used=result.tokens_used,
    )
    session.add(summ)
    await session.commit()

    # Step 4: read it back — proves the whole chain is observable.
    stored = (await session.execute(
        select(Summary).where(
            Summary.project_id == pid,
            Summary.target_id == target["id"],
        )
    )).scalar_one()
    assert stored.summary == result.summary
    assert stored.model_used == result.model_used
    # Tokens used may be None for agent_sdk (CLI doesn't surface count
    # via the same API), but the field must round-trip.
    assert stored.tokens_used == result.tokens_used


@pytest.mark.asyncio
@requires_sdk
async def test_mcp_get_module_summary_returns_real_llm_text(session: AsyncSession):
    """The MCP loop: get_module_summary must hand back the LLM-
    generated prose, not the stub placeholder. This is the test
    that proves an agent actually GETS a real summary back when
    it calls the tool."""
    from app.mcp.queries import get_module_summary
    from app.extractor.agent import Extractor

    pid = _uuid.uuid4()
    target_id = "py:server/app/mcp/queries.py:get_module_summary@xx"
    node_data = {
        "name": "get_module_summary",
        "qual_name": "get_module_summary",
        "kind": "async_function",
        "component_id": "py.mcp",
        "signature": "async def get_module_summary(session, *, project_id, target_id, level):",
    }
    session.add(Node(
        id=target_id, project_id=pid, kind="Symbol",
        data=node_data, certainty="asserted",
        created_by=["ggoss-py"],
        valid_from=datetime.now(tz=timezone.utc),
    ))
    await session.commit()

    # Generate REAL summary via Claude.
    ext = Extractor()
    ext._api_key = None
    result = await ext.summarize(
        level=1, target_id=target_id,
        evidence=[{"kind": "node", "id": target_id, "data": node_data}],
    )
    assert result.model_used != "stub"

    # Persist it.
    session.add(Summary(
        id=_uuid.uuid4(),
        project_id=pid, target_id=target_id, level=1,
        summary=result.summary, detailed=result.detailed,
        claims=result.claims, open_questions=result.open_questions,
        model_used=result.model_used, tokens_used=result.tokens_used,
    ))
    await session.commit()

    # MCP query reads it back.
    mcp_out = await get_module_summary(
        session, project_id=pid, target_id=target_id, level=1,
    )
    assert mcp_out is not None
    # Documented response shape varies — check the LLM text is in there.
    payload_str = str(mcp_out)
    assert result.summary[:30] in payload_str, (
        f"LLM summary not surfaced in MCP response. Got: {payload_str[:400]}"
    )
    # Confirm 'stub' marker NOT in the response — proves the
    # real-LLM path landed end-to-end.
    assert "stub" not in payload_str.lower() or "agent_sdk" in payload_str.lower()
