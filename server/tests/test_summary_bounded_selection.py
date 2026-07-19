"""L2/L3 selection is pushed into SQL and bounded by ``limit``.

Regression guard for the bounded-selection rewrite: the first ``limit``
files (L2) / modules (L3) in lexicographic order are summarized, later ones
are not touched, and provider calls stay bounded by the selection.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import app.extractor.runner as runner_module
from app.extractor.agent import ExtractorResult
from app.extractor.cost import LLMRunBudget
from app.extractor.runner import summarise_l2, summarise_l3
from app.models.findings import Summary
from app.models.graph import Node
from app.models.organization import Organization
from app.models.overlays import GraphNodeHumanOverlay
from app.models.projects import Project
from app.testing.llm_adapter import (
    create_llm_ledger_tables,
    finish_test_provider_result,
)
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

_PRODUCTION_BEGIN_ATTEMPT = runner_module.begin_attempt


@pytest.fixture(autouse=True)
def _summary_contract_lifecycle_mode(monkeypatch):
    async def generic_test_begin_attempt(*args, **kwargs):  # noqa: ANN002, ANN003
        assert kwargs.pop("require_atomic_dollar_reservation") is True
        return await _PRODUCTION_BEGIN_ATTEMPT(
            *args,
            **kwargs,
            require_atomic_dollar_reservation=False,
        )

    monkeypatch.setattr(
        runner_module,
        "begin_attempt",
        generic_test_begin_attempt,
    )


def _pin_graph(monkeypatch) -> None:  # noqa: ANN001
    async def fake_read(_session, *, project_id):  # noqa: ANN001
        return SimpleNamespace(
            project_id=project_id,
            generation=1,
            overlay_generation=0,
        )

    async def fake_lock(
        _session,
        *,
        project_id,  # noqa: ARG001, ANN001
        expected_generation=None,
        expected_overlay_generation=None,
    ):
        assert expected_generation == 1
        assert expected_overlay_generation == 0
        return SimpleNamespace(source_generation=1, overlay_generation=0)

    monkeypatch.setattr("app.extractor.runner.read_graph_stamp", fake_read)
    monkeypatch.setattr(
        "app.extractor.runner.lock_ready_summary_generation", fake_lock
    )


class CountingExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, level, target_id, evidence):
        self.calls += 1
        result = ExtractorResult(
            summary="bounded summary",
            detailed="bounded summary",
            claims=[],
            open_questions=[],
            model_used="fake",
            tokens_used=3,
        )
        return await finish_test_provider_result(
            result,
            target_id=target_id,
            input_text=repr(evidence),
        )


async def _make_session_factory(engine):
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
        await create_llm_ledger_tables(connection)
        await connection.run_sync(Summary.__table__.create)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _l1_pair(project_id: uuid.UUID, node_id: str, file_path: str):
    now = datetime.now(tz=timezone.utc)
    return (
        Node(
            id=node_id,
            project_id=project_id,
            kind="Symbol",
            data={"name": node_id, "location": {"file": file_path}},
            certainty="asserted",
            created_by=["ggoss-py"],
        ),
        Summary(
            id=uuid.uuid4(),
            project_id=project_id,
            level=1,
            target_id=node_id,
            validated_graph_generation=1,
            validated_overlay_generation=0,
            validated_at=now,
            summary=f"function {node_id}",
            detailed=f"function {node_id}",
            claims=[],
            open_questions=[],
            model_used="fake",
            tokens_used=1,
            generated_at=now,
        ),
    )


@pytest.mark.asyncio
async def test_l2_limit_selects_first_files_lexicographically(monkeypatch):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = await _make_session_factory(engine)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with Session() as session:
        for index, file_path in enumerate(["a.py", "b.py", "c.py"]):
            node, summary = _l1_pair(project_id, f"sym:{index}", file_path)
            session.add(node)
            session.add(summary)
        await session.commit()

        extractor = CountingExtractor()
        produced = await summarise_l2(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=2,
            analysis_run_id=run_id,
            run_budget=LLMRunBudget(scope_id=run_id),
        )
        l2_targets = sorted(
            row["target_id"]
            for row in (
                await session.execute(
                    Summary.__table__.select().where(Summary.level == 2)
                )
            ).mappings()
        )

    await engine.dispose()
    assert produced == 2
    assert extractor.calls == 2, "files beyond the limit must not reach the provider"
    assert l2_targets == ["a.py", "b.py"]


@pytest.mark.asyncio
async def test_l3_limit_selects_first_modules_lexicographically(monkeypatch):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    Session = await _make_session_factory(engine)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    files = ["alpha/x.py", "alpha/y.py", "beta/z.py", "gamma/w.py"]
    async with Session() as session:
        for index, file_path in enumerate(files):
            node, l1 = _l1_pair(project_id, f"sym:{index}", file_path)
            session.add(node)
            session.add(l1)
            session.add(
                Summary(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    level=2,
                    target_id=file_path,
                    validated_graph_generation=1,
                    validated_overlay_generation=0,
                    validated_at=now,
                    summary=f"file {file_path}",
                    detailed=f"file {file_path}",
                    claims=[],
                    open_questions=[],
                    model_used="fake",
                    tokens_used=1,
                    generated_at=now,
                )
            )
        await session.commit()

        extractor = CountingExtractor()
        produced = await summarise_l3(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=2,
            analysis_run_id=run_id,
            run_budget=LLMRunBudget(scope_id=run_id),
        )
        l3_targets = sorted(
            row["target_id"]
            for row in (
                await session.execute(
                    Summary.__table__.select().where(Summary.level == 3)
                )
            ).mappings()
        )

    await engine.dispose()
    assert produced == 2
    assert l3_targets == ["alpha", "beta"]
