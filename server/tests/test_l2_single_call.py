from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.extractor.agent import FALLBACK_AGENT_SDK_ERROR, Extractor, ExtractorResult
from app.extractor.runner import FALLBACK_UNGROUNDED_RESPONSE, summarise_l2
from app.models.findings import LLMCall, Summary
from app.models.graph import Node
from app.models.overlays import GraphNodeHumanOverlay
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


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
    def __init__(self, claims: list[dict]) -> None:
        self.calls = 0
        self.claims = claims

    async def summarize(self, level, target_id, evidence):
        self.calls += 1
        return ExtractorResult(
            summary="bounded file summary",
            detailed="bounded file summary",
            claims=self.claims,
            open_questions=[],
            model_used="fake",
            tokens_used=7,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_claims",
    [
        pytest.param([], id="empty-claims"),
        pytest.param(
            [
                {
                    "claim": "A plausible but out-of-scope claim.",
                    "evidence": [
                        {
                            "kind": "node",
                            "node_id": "sym:not-in-provider-scope",
                            "certainty": "asserted",
                        }
                    ],
                }
            ],
            id="all-claims-rejected",
        ),
    ],
)
async def test_one_chunk_l2_uses_exactly_one_provider_call(
    monkeypatch, provider_claims
):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
        await connection.run_sync(Summary.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with Session() as session:
        session.add(
            Node(
                id="sym:one",
                project_id=project_id,
                kind="Symbol",
                data={"name": "one", "location": {"file": "service.py"}},
                certainty="asserted",
                created_by=["ggoss-py"],
            )
        )
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=project_id,
                level=1,
                target_id="sym:one",
                validated_graph_generation=1,
                validated_overlay_generation=0,
                validated_at=datetime.now(tz=timezone.utc),
                summary="function one",
                detailed="function one",
                claims=[],
                open_questions=[],
                model_used="fake",
                tokens_used=1,
                generated_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.commit()

        extractor = CountingExtractor(provider_claims)
        produced = await summarise_l2(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=1,
            analysis_run_id=run_id,
        )
        saved = await session.get(Summary, next(
            row.id for row in (await session.execute(
                Summary.__table__.select().where(Summary.level == 2)
            )).mappings()
        ))
        ledger_rows = (
            await session.execute(LLMCall.__table__.select())
        ).mappings().all()

    await engine.dispose()
    assert produced == 1
    assert extractor.calls == 1
    assert saved is not None
    assert saved.analysis_run_id == run_id
    assert saved.evidence_hash
    assert saved.fallback_reason == FALLBACK_UNGROUNDED_RESPONSE
    assert saved.model_used == f"stub:{FALLBACK_UNGROUNDED_RESPONSE}"
    assert all(
        not isinstance(claim, dict)
        or claim.get("claim") != "_evidence_hash"
        for claim in (saved.claims or [])
    )
    assert len(ledger_rows) == 1
    assert ledger_rows[0]["analysis_run_id"] == run_id
    assert ledger_rows[0]["model_used"] == "fake"
    assert ledger_rows[0]["tokens_used"] == 7
    assert ledger_rows[0]["status"] == "fallback"
    assert ledger_rows[0]["fallback_reason"] == FALLBACK_UNGROUNDED_RESPONSE


class FailSecondChunkExtractor:
    def __init__(self) -> None:
        self.calls = 0

    async def summarize(self, level, target_id, evidence):
        self.calls += 1
        if self.calls == 2:
            return Extractor._stub(
                level, target_id, evidence, FALLBACK_AGENT_SDK_ERROR
            )
        return ExtractorResult(
            summary="map succeeded",
            detailed="map succeeded",
            claims=[
                {
                    "claim": "The chunk contains this symbol.",
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
            model_used="fake",
            tokens_used=7,
        )


@pytest.mark.asyncio
async def test_partial_failure_blocks_reduce_and_closes_stale_current_summary(monkeypatch):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
        await connection.run_sync(Summary.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    old_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    async with Session() as session:
        for index in range(2):
            node_id = f"sym:{index}"
            session.add(
                Node(
                    id=node_id,
                    project_id=project_id,
                    kind="Symbol",
                    data={
                        "name": node_id,
                        "location": {"file": "large.py"},
                    },
                    certainty="asserted",
                    created_by=["ggoss-py"],
                )
            )
            session.add(
                Summary(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    level=1,
                    target_id=node_id,
                    validated_graph_generation=1,
                    validated_overlay_generation=0,
                    validated_at=now,
                    summary=str(index) * 8_000,
                    detailed="child",
                    claims=[],
                    open_questions=[],
                    evidence_hash=f"child-{index}",
                    model_used="fake",
                    tokens_used=1,
                    generated_at=now,
                )
            )
        session.add(
            Summary(
                id=old_id,
                project_id=project_id,
                level=2,
                target_id="large.py",
                summary="known-good summary",
                detailed="known-good detail",
                claims=[],
                open_questions=[],
                evidence_hash="stale-hash",
                model_used="claude-success",
                tokens_used=11,
                generated_at=now,
            )
        )
        await session.commit()

        extractor = FailSecondChunkExtractor()
        produced = await summarise_l2(
            session,
            extractor,  # type: ignore[arg-type]
            project_id=project_id,
            limit=1,
        )
        rows = (
            await session.execute(
                Summary.__table__.select().where(
                    Summary.level == 2,
                )
            )
        ).mappings().all()

    await engine.dispose()
    assert extractor.calls == 2, "failed maps must not trigger a reduce call"
    assert produced == 1
    current = next(row for row in rows if row["superseded_by"] is None)
    historical = next(row for row in rows if row["id"] == old_id)
    assert current["id"] != old_id
    assert current["model_used"] == f"stub:{FALLBACK_AGENT_SDK_ERROR}"
    assert current["fallback_reason"] == FALLBACK_AGENT_SDK_ERROR
    assert current["claims"] == []
    assert historical["superseded_by"] is not None
