"""PR-138b — Summary.fallback_reason column round-trip.

Audit follow-up: PR-138's fallback_reason was only encoded into the
``model_used`` string. The dashboard had to parse the suffix to
explain "why is this a stub?" — fragile and not introspectable. This
PR adds a structured column + persists it from the runner.

Tests
-----
- column exists on the Summary model
- alembic migration 0024 is the latest head + has a real downgrade
- the L1 runner writes the structured field when the budget guard
  trips
- happy-path summaries leave the column NULL
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.testing.sqlite_polyglot import install_polyglot


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

install_polyglot()

from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.findings import LLMCall, Summary  # noqa: E402
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


# ─── column exists on the model ───────────────────────────────────


def test_summary_model_has_fallback_reason_column():
    """The schema must expose the column so dashboards / APIs can
    read it without string-parsing model_used."""
    cols = {c.name for c in Summary.__table__.columns}
    assert "fallback_reason" in cols
    col = Summary.__table__.columns["fallback_reason"]
    assert col.nullable, "fallback_reason must be NULL-able for happy path"


def test_migration_0024_is_present_and_round_trippable():
    """Forward-guard: the migration file exists with a real downgrade,
    and is the new head (chain integrity)."""
    from pathlib import Path

    vdir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    f = vdir / "0024_summary_fallback_reason.py"
    assert f.exists(), "migration 0024 missing"
    src = f.read_text(encoding="utf-8")
    assert "add_column" in src
    assert "drop_column" in src
    # Chain: down_revision must be the previous head.
    assert 'down_revision = "0023_fk_ondelete"' in src


# ─── real write round-trip ────────────────────────────────────────


@pytest.mark.asyncio
async def test_l1_runner_persists_fallback_reason_when_budget_trips(
    session, monkeypatch,
):
    _pin_graph(monkeypatch)
    monkeypatch.setenv("MNEMOS_LLM_USD_PER_MTOK", "3.0")
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "0.01")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    proj = Project(
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main", languages=["py"],
    )
    session.add(proj)
    session.add(Node(
        id="sym:f", project_id=proj.id, kind="Symbol",
        certainty="asserted", data={"name": "f"}, created_by=["t"],
    ))
    # 5M tokens already spent → over the $0.01 cap.
    old_summary = Summary(
        id=_uuid.uuid4(), project_id=proj.id, level=1,
        target_id="sym:old", summary="x", detailed="x",
        claims=[], open_questions=[],
        model_used="claude-sonnet-4-6", tokens_used=5_000_000,
        generated_at=datetime.now(tz=timezone.utc) - timedelta(minutes=5),
    )
    session.add(old_summary)
    session.add(
        LLMCall(
            id=old_summary.id,
            project_id=proj.id,
            target_id=old_summary.target_id,
            level=old_summary.level,
            model_used=old_summary.model_used,
            tokens_used=old_summary.tokens_used,
            status="legacy_summary",
            generated_at=old_summary.generated_at,
        )
    )
    await session.commit()

    from app.extractor.agent import Extractor
    from app.extractor.runner import summarise_l1

    await summarise_l1(session, Extractor(), project_id=proj.id, limit=5)

    fresh = (await session.execute(
        select(Summary).where(
            Summary.project_id == proj.id,
            Summary.target_id == "sym:f",
            Summary.level == 1,
        )
    )).scalar_one()
    # Structured column carries the reason.
    assert fresh.fallback_reason == "budget_exceeded"
    # Encoded model_used stays for back-compat consumers.
    assert fresh.model_used == "stub:budget_exceeded"


@pytest.mark.asyncio
async def test_happy_path_leaves_fallback_reason_null(session, monkeypatch):
    """The column is null when the LLM call actually succeeded.
    We simulate success by patching the extractor to return a
    populated ExtractorResult with no fallback_reason."""
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    proj = Project(
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main", languages=["py"],
    )
    session.add(proj)
    session.add(Node(
        id="sym:happy", project_id=proj.id, kind="Symbol",
        certainty="asserted", data={"name": "happy"}, created_by=["t"],
    ))
    await session.commit()

    from app.extractor.agent import Extractor, ExtractorResult
    from app.extractor.runner import summarise_l1

    class _GoodExtractor(Extractor):
        async def summarize(self, level, target_id, evidence):
            return ExtractorResult(
                summary="ok", detailed="ok",
                claims=[{
                    "claim": "The symbol exists.",
                    "evidence": [{
                        "kind": "node",
                        "node_id": evidence[0]["node_id"],
                        "certainty": evidence[0]["certainty"],
                    }],
                }],
                open_questions=[],
                model_used="claude-sonnet-4-6",
                tokens_used=1234,
                fallback_reason="",
            )

    await summarise_l1(
        session, _GoodExtractor(), project_id=proj.id, limit=5,
    )

    row = (await session.execute(
        select(Summary).where(
            Summary.target_id == "sym:happy", Summary.level == 1,
        )
    )).scalar_one()
    assert row.fallback_reason is None
    assert row.model_used == "claude-sonnet-4-6"
