"""L2/L3 map-reduce grounding uses graph IDs, never path pseudo IDs.

The mock producer emits JSON text through the real extractor parser and the
runner persists it through the real validator.  This is E2 contract evidence;
it intentionally makes no live-provider claim.
"""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.extractor.agent import ExtractorResult
from app.extractor.packing import (
    MAX_EVIDENCE_PROMPT_CHARS,
    EvidencePromptTooLarge,
    _approx_tokens,
    serialize_evidence,
)
from app.extractor.runner import (
    _bounded_rollup_evidence,
    summarise_l1,
    summarise_l2,
    summarise_l3,
)
from app.extractor.schema import parse_and_normalize_extractor_payload
from app.models.findings import LLMCall, Summary
from app.models.graph import AnalysisRun, Edge, GraphHead, Node
from app.models.organization import Organization
from app.models.overlays import (
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)
from app.models.projects import Project
from app.testing.graph_publication import published_run_fields
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


class ParsedGroundingExtractor:
    """Mock provider text routed through the production output contract."""

    def __init__(self, *, fake_id: str) -> None:
        self.fake_id = fake_id
        self.calls: list[tuple[int, str, list[dict]]] = []

    async def summarize(self, level, target_id, evidence):
        captured = copy.deepcopy(evidence)
        self.calls.append((level, target_id, captured))
        actual_id = next(
            (
                row.get("node_id")
                for row in evidence
                if row.get("kind") == "node" and isinstance(row.get("node_id"), str)
            ),
            "missing:graph-id",
        )
        # The fake/path citation is schema-valid on purpose.  Only the
        # input-scope + current-graph validator is allowed to reject it.
        provider_text = json.dumps(
            {
                "summary": f"mock L{level} summary",
                "detailed": f"mock L{level} detail",
                "claims": [
                    {
                        "claim": "actual graph claim",
                        "evidence": [
                            {
                                "kind": "node",
                                "node_id": actual_id,
                                "certainty": "inferred",
                            }
                        ],
                    },
                    {
                        "claim": "path-shaped fake claim",
                        "evidence": [
                            {
                                "kind": "node",
                                "node_id": self.fake_id,
                                "certainty": "asserted",
                            }
                        ],
                    },
                ],
                "open_questions": [],
            }
        )
        parsed = parse_and_normalize_extractor_payload(provider_text)
        return ExtractorResult(
            summary=parsed["summary"],
            detailed=parsed["detailed"],
            claims=parsed["claims"],
            open_questions=parsed["open_questions"],
            model_used="mock-provider",
            tokens_used=7,
        )


async def _new_database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Organization.__table__.create)
        await connection.run_sync(Project.__table__.create)
        await connection.run_sync(AnalysisRun.__table__.create)
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)
        await connection.run_sync(GraphNodeHumanOverlay.__table__.create)
        await connection.run_sync(GraphEdgeHumanOverlay.__table__.create)
        await connection.run_sync(GraphEdgeRuntimeOverlay.__table__.create)
        await connection.run_sync(GraphHead.__table__.create)
        await connection.run_sync(Summary.__table__.create)
        await connection.run_sync(LLMCall.__table__.create)
    return engine, sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def _seed_ready_head(
    session: AsyncSession, project_id: uuid.UUID
) -> None:
    now = datetime.now(tz=timezone.utc)
    run_id = uuid.uuid4()
    session.add(
        Project(
            id=project_id,
            name="rollup grounding",
            gitlab_project_id=1,
            gitlab_url="https://example.invalid/rollup",
            default_branch="main",
            languages=["python"],
        )
    )
    session.add(
        AnalysisRun(
            id=run_id,
            project_id=project_id,
            status="completed",
            triggered_by="test",
            git_sha="a" * 40,
            scope="full",
            started_at=now,
            completed_at=now,
            **published_run_fields(generation=1, published_at=now),
        )
    )
    await session.commit()
    session.add(
        GraphHead(
            project_id=project_id,
            current_run_id=run_id,
            generation=1,
            overlay_generation=0,
            state="ready",
            published_at=now,
        )
    )
    await session.commit()


def _node_and_l1(
    *,
    project_id: uuid.UUID,
    node_id: str,
    file_path: str,
    l1_summary: str,
) -> tuple[Node, Summary]:
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
            validated_at=datetime.now(tz=timezone.utc),
            summary=l1_summary,
            detailed=l1_summary,
            claims=[],
            open_questions=[],
            evidence_hash=f"l1-{node_id}",
            model_used="seed",
            tokens_used=1,
            generated_at=now,
        ),
    )


def _assert_only_actual_claim(saved: Summary, node_ids: set[str]) -> None:
    assert [claim["claim"] for claim in saved.claims] == ["actual graph claim"]
    evidence = saved.claims[0]["evidence"][0]
    assert evidence["node_id"] in node_ids
    # The validator owns certainty and replaces the model's "inferred" value.
    assert evidence["certainty"] == "asserted"


def test_rollup_pack_stops_at_hard_budget_with_real_ids_only():
    source_ids = {f"sym:many:{index}" for index in range(5_000)}
    source_chunk = [
        {
            "kind": "node",
            "node_id": node_id,
            "certainty": "asserted",
        }
        for node_id in sorted(source_ids)
    ]
    partial = ExtractorResult(
        summary="bounded partial",
        detailed="bounded partial",
        claims=[],
        open_questions=[],
        model_used="mock-provider",
        tokens_used=7,
    )

    packed = _bounded_rollup_evidence(
        [partial],
        [source_chunk],
        max_tokens=3000,
    )

    assert packed
    assert _approx_tokens(packed) <= 3000
    assert len(packed) < len(source_chunk)
    assert {row["node_id"] for row in packed} <= source_ids


def _prompt_evidence_json(prompt: str) -> str:
    graph_section = prompt.split("\nGraph evidence ", 1)[1]
    return graph_section.split(":\n", 1)[1].split("\n\nCite only", 1)[0]


def test_both_provider_prompts_embed_the_same_complete_valid_json():
    from app.extractor.agent import Extractor
    from app.extractor.agent_sdk import _build_prompt

    evidence = [
        {
            "kind": "node",
            "node_id": "sym:한글",
            "certainty": "asserted",
            "data": {"name": "서비스"},
        }
    ]

    direct_prompt = Extractor._prompt(2, "service.py", evidence)
    sdk_prompt = _build_prompt(
        2,
        "service.py",
        evidence,
        MAX_EVIDENCE_PROMPT_CHARS,
    )

    assert json.loads(_prompt_evidence_json(direct_prompt)) == evidence
    assert json.loads(_prompt_evidence_json(sdk_prompt)) == evidence
    assert _prompt_evidence_json(direct_prompt) == serialize_evidence(evidence)
    assert _prompt_evidence_json(sdk_prompt) == serialize_evidence(evidence)


def test_agent_sdk_prompt_rejects_oversize_instead_of_slicing_json():
    from app.extractor.agent_sdk import _build_prompt

    oversized = [{"payload": "x" * (MAX_EVIDENCE_PROMPT_CHARS + 1)}]

    with pytest.raises(EvidencePromptTooLarge):
        _build_prompt(3, "module", oversized, MAX_EVIDENCE_PROMPT_CHARS)


@pytest.mark.asyncio
async def test_extractor_oversize_fails_before_any_provider_call():
    from app.extractor.agent import FALLBACK_EVIDENCE_BUDGET, Extractor

    create = AsyncMock()
    extractor = Extractor()
    extractor._api_key = "test-key"
    extractor._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    oversized = [{"payload": "x" * (MAX_EVIDENCE_PROMPT_CHARS + 1)}]

    result = await extractor.summarize(1, "sym:x", oversized)

    assert result.fallback_reason == FALLBACK_EVIDENCE_BUDGET
    assert result.model_used == f"stub:{FALLBACK_EVIDENCE_BUDGET}"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_l1_oversized_node_uses_one_complete_exact_validation_scope(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_id = "sym:l1-large"
    target_id = "sym:l1-neighbour"
    edge_id = uuid.uuid4()
    async with Session() as session:
        await _seed_ready_head(session, project_id)
        session.add_all(
            [
                Node(
                    id=node_id,
                    project_id=project_id,
                    kind="Symbol",
                    data={
                        "name": node_id,
                        "is_entry_point": True,
                        "payload": "x" * 30_000,
                        "location": {"file": "large.py"},
                    },
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
                Edge(
                    id=edge_id,
                    project_id=project_id,
                    source_id=node_id,
                    target_id=target_id,
                    kind="CALLS",
                    data={},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                ),
            ]
        )
        await session.commit()
        extractor = ParsedGroundingExtractor(fake_id="large.py")

        assert await summarise_l1(session, extractor, project_id=project_id, limit=1) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 1))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 1
    prompt_scope = extractor.calls[0][2]
    assert _approx_tokens(prompt_scope) <= 3000
    assert json.loads(serialize_evidence(prompt_scope)) == prompt_scope
    assert prompt_scope[0]["scope"] == "prompt_evidence"
    assert prompt_scope[0]["truncated"] is True
    assert prompt_scope[0]["included_rows"] < prompt_scope[0]["total_rows"]
    assert {row["node_id"] for row in prompt_scope if row.get("kind") == "node"} == {node_id}
    assert all(row.get("edge_id") != str(edge_id) for row in prompt_scope)
    _assert_only_actual_claim(saved, {node_id})


@pytest.mark.asyncio
async def test_l2_single_chunk_accepts_real_id_and_rejects_file_path(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_id = "sym:l2-single"
    file_path = "service.py"
    async with Session() as session:
        await _seed_ready_head(session, project_id)
        session.add_all(
            _node_and_l1(
                project_id=project_id,
                node_id=node_id,
                file_path=file_path,
                l1_summary="small L1 summary",
            )
        )
        await session.commit()
        extractor = ParsedGroundingExtractor(fake_id=file_path)

        assert await summarise_l2(session, extractor, project_id=project_id, limit=1) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 2))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 1
    final_evidence = extractor.calls[-1][2]
    assert {row["node_id"] for row in final_evidence} == {node_id}
    assert _approx_tokens(final_evidence) <= 3000
    _assert_only_actual_claim(saved, {node_id})


@pytest.mark.asyncio
async def test_l2_multi_chunk_rollup_uses_real_ids_within_budget(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_ids = {"sym:l2-a", "sym:l2-b"}
    file_path = "large.py"
    async with Session() as session:
        await _seed_ready_head(session, project_id)
        for index, node_id in enumerate(sorted(node_ids)):
            session.add_all(
                _node_and_l1(
                    project_id=project_id,
                    node_id=node_id,
                    file_path=file_path,
                    l1_summary=str(index) * 13_000,
                )
            )
        await session.commit()
        extractor = ParsedGroundingExtractor(fake_id=file_path)

        assert await summarise_l2(session, extractor, project_id=project_id, limit=1) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 2))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 3  # two maps + one reduce
    rollup_evidence = extractor.calls[-1][2]
    assert {row["node_id"] for row in rollup_evidence} == node_ids
    assert all(row["node_id"] != file_path for row in rollup_evidence)
    assert _approx_tokens(rollup_evidence) <= 3000
    _assert_only_actual_claim(saved, node_ids)


async def _seed_l3(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    files: list[tuple[str, str, str]],
) -> set[str]:
    """Seed (file path, node ID, L2 summary) rows plus their L1 grounding."""

    node_ids: set[str] = set()
    now = datetime.now(tz=timezone.utc)
    for file_path, node_id, l2_summary in files:
        node_ids.add(node_id)
        session.add_all(
            _node_and_l1(
                project_id=project_id,
                node_id=node_id,
                file_path=file_path,
                l1_summary=f"L1 for {node_id}",
            )
        )
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=project_id,
                level=2,
                target_id=file_path,
                validated_graph_generation=1,
                validated_overlay_generation=0,
                validated_at=datetime.now(tz=timezone.utc),
                summary=l2_summary,
                detailed=l2_summary,
                claims=[],
                open_questions=[],
                evidence_hash=f"l2-{node_id}",
                model_used="seed",
                tokens_used=1,
                generated_at=now,
            )
        )
    await session.commit()
    return node_ids


@pytest.mark.asyncio
async def test_l3_single_chunk_accepts_real_id_and_rejects_file_path(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    file_path = "module/service.py"
    async with Session() as session:
        await _seed_ready_head(session, project_id)
        node_ids = await _seed_l3(
            session,
            project_id=project_id,
            files=[(file_path, "sym:l3-single", "small L2 summary")],
        )
        extractor = ParsedGroundingExtractor(fake_id=file_path)

        assert await summarise_l3(session, extractor, project_id=project_id, limit=1) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 3))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 1
    final_evidence = extractor.calls[-1][2]
    assert {row["node_id"] for row in final_evidence} == node_ids
    assert all(row["node_id"] != file_path for row in final_evidence)
    assert _approx_tokens(final_evidence) <= 4000
    _assert_only_actual_claim(saved, node_ids)


@pytest.mark.asyncio
async def test_l3_multi_chunk_rollup_uses_real_ids_within_budget(monkeypatch):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    module = "module"
    files = [
        (f"{module}/a.py", "sym:l3-a", "a" * 17_000),
        (f"{module}/b.py", "sym:l3-b", "b" * 17_000),
    ]
    async with Session() as session:
        await _seed_ready_head(session, project_id)
        node_ids = await _seed_l3(
            session,
            project_id=project_id,
            files=files,
        )
        extractor = ParsedGroundingExtractor(fake_id=module)

        assert await summarise_l3(session, extractor, project_id=project_id, limit=1) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 3))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 3  # two maps + one reduce
    rollup_evidence = extractor.calls[-1][2]
    assert {row["node_id"] for row in rollup_evidence} == node_ids
    assert all(row["node_id"] != module for row in rollup_evidence)
    assert _approx_tokens(rollup_evidence) <= 4000
    _assert_only_actual_claim(saved, node_ids)
