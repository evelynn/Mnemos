"""PR-138 — verify the budget guard is wired (not dead code).

The audit's biggest "claimed but not used" risk: a budget module that
no callsite invokes. This test:

- Sets ``MNEMOS_LLM_BUDGET_USD_PER_PROJECT=0.01`` (extremely tight).
- Plants an existing Summary that already exceeds the cap.
- Runs ``summarise_l1`` against the project.
- Asserts the freshly-created Summary carries the
  ``stub:budget_exceeded`` label (so the operator can debug).

If the runner forgot to invoke ``require_budget``, the freshly-
written Summary would carry ``model_used="stub"`` (no reason) — the
test would fail.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.findings import Summary  # noqa: E402
from app.models.graph import Node  # noqa: E402
from app.models.projects import Project  # noqa: E402


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
    # Existing Summary that already busts the $0.01 cap (5M tokens
    # @ $3/Mtok = $15).
    busted = Summary(
        id=_uuid.uuid4(),
        project_id=proj.id,
        level=1,
        target_id="sym:test:other",
        summary="old", detailed="old",
        claims=[], open_questions=[],
        model_used="claude-sonnet-4-6",
        tokens_used=5_000_000,
        generated_at=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
    )
    session.add(busted)
    await session.commit()
    return proj


@pytest.mark.asyncio
async def test_l1_runner_short_circuits_to_stub_when_over_budget(
    session, monkeypatch,
):
    """The big-money test: every L1 LLM call must pass through the
    budget guard. With cap=$0.01 and $15 already spent the guard
    must trip; the new Summary lands with the explicit
    ``stub:budget_exceeded`` label.
    """
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.01")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_WINDOW_SEC", "86400")
    # Also force the stub path so we never hit a real API key, but
    # the budget guard runs first.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    proj = await _seed(session)

    from app.extractor.agent import Extractor
    from app.extractor.runner import summarise_l1

    ext = Extractor()
    n = await summarise_l1(
        session, ext, project_id=proj.id, limit=5,
    )
    assert n >= 1, "summarise_l1 should have written ≥1 row"

    # The new row exists, and it's labelled with the budget-exceeded
    # reason — proves require_budget was actually called.
    fresh = (await session.execute(
        select(Summary)
        .where(Summary.project_id == proj.id)
        .where(Summary.target_id == "sym:test:foo")
        .where(Summary.level == 1)
    )).scalar_one_or_none()
    assert fresh is not None, "summarise_l1 didn't write the expected row"
    assert fresh.model_used == "stub:budget_exceeded", (
        f"runner did NOT pass through budget guard; "
        f"model_used={fresh.model_used!r}"
    )


@pytest.mark.asyncio
async def test_runner_does_not_call_budget_guard_when_cap_unset(
    session, monkeypatch,
):
    """When the cap is disabled (default), the runner runs normally
    and the L1 row carries the regular stub label (NOT
    ``stub:budget_exceeded``)."""
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    proj = await _seed(session)

    from app.extractor.agent import Extractor
    from app.extractor.runner import summarise_l1

    ext = Extractor()
    n = await summarise_l1(
        session, ext, project_id=proj.id, limit=5,
    )
    assert n >= 1
    fresh = (await session.execute(
        select(Summary)
        .where(Summary.project_id == proj.id)
        .where(Summary.target_id == "sym:test:foo")
        .where(Summary.level == 1)
    )).scalar_one_or_none()
    assert fresh is not None
    # No backend configured, no budget → plain "stub" label.
    assert fresh.model_used == "stub", (
        f"unexpected label when budget unset: {fresh.model_used!r}"
    )


def test_runner_module_imports_budget_helpers():
    """Forward-guard: the runner must keep importing the budget API,
    or a future refactor could quietly orphan it again."""
    import app.extractor.runner as r
    assert hasattr(r, "_summarize_with_budget")
    assert hasattr(r, "require_budget")
    assert hasattr(r, "BudgetExceeded")
