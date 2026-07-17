"""PR-138 — verify paid Summary dispatch uses the durable budget gate.

``begin_attempt`` is the sole authorization point for a paid provider call.
The tests below prove that an absent dollar policy becomes a structured,
operator-visible fallback while the deterministic no-backend path remains
available without inventing a paid attempt.
"""

from __future__ import annotations

import ast
import inspect
import uuid as _uuid
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


def _pin_graph(monkeypatch) -> None:  # noqa: ANN001
    async def fake_read(_session, *, project_id):  # noqa: ANN001
        return SimpleNamespace(
            project_id=project_id, generation=1, overlay_generation=0
        )

    async def fake_lock(_session, **kwargs):  # noqa: ANN001
        assert kwargs["expected_generation"] == 1
        assert kwargs["expected_overlay_generation"] == 0
        return SimpleNamespace(source_generation=1, overlay_generation=0)

    monkeypatch.setattr("app.extractor.runner.read_graph_stamp", fake_read)
    monkeypatch.setattr(
        "app.extractor.runner.lock_ready_summary_generation", fake_lock
    )

from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.extractor.agent import Extractor, ExtractorResult  # noqa: E402
from app.extractor.cost import LLMRunBudget  # noqa: E402
from app.models.findings import Summary  # noqa: E402
from app.models.graph import Node  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.llm_adapter import finish_test_provider_result  # noqa: E402


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(Base.metadata.create_all)
    Sess = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    async with Sess() as s:
        yield s
    await eng.dispose()


async def _seed(session: AsyncSession) -> Project:
    proj = Project(
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main", languages=["py"],
    )
    session.add(proj)
    # One symbol so summarise_l1 has work to do.
    sym = Node(
        id="sym:test:foo",
        project_id=proj.id,
        kind="Symbol",
        certainty="asserted",
        data={"name": "foo"},
        created_by=["test"],
    )
    session.add(sym)
    await session.commit()
    return proj


class _PaidExtractor(Extractor):
    async def summarize(self, _level, target_id, evidence):  # noqa: ANN001
        return await finish_test_provider_result(
            ExtractorResult(
                summary="paid result",
                detailed="paid result",
                claims=[
                    {
                        "claim": "The symbol exists.",
                        "evidence": [
                            {
                                "kind": "node",
                                "node_id": evidence[0]["node_id"],
                                "certainty": evidence[0]["certainty"],
                            }
                        ],
                    }
                ],
                open_questions=[],
                model_used="test-model",
                tokens_used=10,
            ),
            target_id=target_id,
        )


@pytest.mark.asyncio
async def test_l1_runner_denies_paid_attempt_when_dollar_policy_is_absent(
    session, monkeypatch,
):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    proj = await _seed(session)

    from app.extractor.runner import summarise_l1

    n = await summarise_l1(
        session,
        _PaidExtractor(),
        project_id=proj.id,
        limit=5,
        run_budget=LLMRunBudget(),
    )
    assert n >= 1, "summarise_l1 should have written ≥1 row"

    fresh = (await session.execute(
        select(Summary)
        .where(Summary.project_id == proj.id)
        .where(Summary.target_id == "sym:test:foo")
        .where(Summary.level == 1)
    )).scalar_one_or_none()
    assert fresh is not None, "summarise_l1 didn't write the expected row"
    assert fresh.fallback_reason == "project_dollar_budget_required"
    assert fresh.model_used == "stub:project_dollar_budget_required"


@pytest.mark.asyncio
async def test_runner_preserves_deterministic_no_backend_path_when_policy_unset(
    session, monkeypatch,
):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    proj = await _seed(session)

    from app.extractor.agent import Extractor
    from app.extractor.runner import summarise_l1

    ext = Extractor()
    n = await summarise_l1(
        session,
        ext,
        project_id=proj.id,
        limit=5,
        run_budget=LLMRunBudget(),
    )
    assert n >= 1
    fresh = (await session.execute(
        select(Summary)
        .where(Summary.project_id == proj.id)
        .where(Summary.target_id == "sym:test:foo")
        .where(Summary.level == 1)
    )).scalar_one_or_none()
    assert fresh is not None
    assert fresh.model_used == "stub"
    assert fresh.fallback_reason == "no_backend"


def test_runner_begin_attempt_requires_atomic_dollar_authority():
    """Forward-guard the sole production paid-dispatch authorization."""
    import app.extractor.runner as r

    tree = ast.parse(inspect.getsource(r._summarize_with_budget))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "begin_attempt"
    ]
    assert len(calls) == 1
    requirement = next(
        (
            keyword.value
            for keyword in calls[0].keywords
            if keyword.arg == "require_atomic_dollar_reservation"
        ),
        None,
    )
    assert isinstance(requirement, ast.Constant)
    assert requirement.value is True
