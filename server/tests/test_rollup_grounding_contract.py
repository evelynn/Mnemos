"""L2/L3 map-reduce grounding uses graph IDs, never path pseudo IDs.

The mock producer emits JSON text through the real extractor parser and the
runner persists it through the real validator.  This is E2 contract evidence;
it intentionally makes no live-provider claim.
"""

from __future__ import annotations

import copy
import json
import sys
import uuid
from datetime import datetime, timezone
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.extractor.agent import Extractor, ExtractorResult
from app.extractor.agent_sdk import (
    SUMMARY_CANDIDATE_CONTRACT,
    current_summary_candidate_context,
    normalize_summary_candidate_payload,
)
import app.extractor.runner as runner_module
from app.extractor.cost import LLMRunBudget
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
from app.llm.contracts import (
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    UsageStatus,
)
from app.llm.lifecycle import (
    AttemptOutcome,
    AttemptStartMetadata,
    LLMLifecycleError,
    SemanticCandidate,
    fingerprint_input,
    require_attempt_callbacks,
)
from app.models.findings import Summary
from app.models.graph import AnalysisRun, Edge, GraphHead, Node
from app.models.organization import Organization
from app.models.overlays import (
    GraphEdgeHumanOverlay,
    GraphEdgeRuntimeOverlay,
    GraphNodeHumanOverlay,
)
from app.models.projects import Project
from app.models.findings import LLMCall
from app.models.llm import LLMSemanticCandidate
from app.testing.graph_publication import published_run_fields
from app.testing.llm_adapter import (
    create_llm_ledger_tables,
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


async def _finish_summary_candidate(
    result: ExtractorResult,
    *,
    target_id: str,
    input_text: str,
) -> ExtractorResult:
    """Finalize the mock response through the real Summary candidate boundary."""

    callbacks = require_attempt_callbacks("grounding contract test provider")
    context = current_summary_candidate_context()
    assert context is not None
    assert callbacks.finish_candidate is not None
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=uuid.uuid4(),
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="test",
            provider_mode="api",
            requested_model=result.model_used,
            input_fingerprint=fingerprint_input(
                "ledgered-summary-test-system", input_text
            ),
            estimated_input_tokens=max(1, len(input_text.encode("utf-8"))),
            input_estimate_method="utf8_bytes_upper_v1",
            requested_max_output_tokens=1_024,
            usage_source=UsageSource.PROVIDER_API,
        )
    )
    if callbacks.mark_provider_dispatch is not None:
        callbacks.mark_provider_dispatch()
    tokens = int(result.tokens_used or 0)
    payload = normalize_summary_candidate_payload(
        {
            "contract": SUMMARY_CANDIDATE_CONTRACT,
            "summary": result.summary,
            "detailed": result.detailed,
            "claims": result.claims,
            "open_questions": result.open_questions,
        }
    )
    await callbacks.finish_candidate(
        ticket,
        AttemptOutcome(
            attempt_status=AttemptStatus.COMPLETED,
            result_status=ResultStatus.PENDING,
            usage=LLMUsageV1(
                status=UsageStatus.REPORTED_COMPLETE,
                source=UsageSource.PROVIDER_API,
                input_tokens=tokens,
                output_tokens=0,
                total_tokens=tokens,
            ),
            resolved_model=result.model_used,
        ),
        SemanticCandidate(
            contract_name=SUMMARY_CANDIDATE_CONTRACT,
            binding_fingerprint=context.binding_fingerprint,
            payload=payload,
        ),
    )
    result._llm_call_id = ticket.attempt_id
    return result


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
        result = ExtractorResult(
            summary=parsed["summary"],
            detailed=parsed["detailed"],
            claims=parsed["claims"],
            open_questions=parsed["open_questions"],
            model_used="mock-provider",
            tokens_used=7,
        )
        return await _finish_summary_candidate(
            result,
            target_id=target_id,
            input_text=repr(evidence),
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
        await create_llm_ledger_tables(connection)
        await connection.run_sync(Summary.__table__.create)
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
@pytest.mark.parametrize("source_certainty", ["asserted", "inferred"])
async def test_atomic_summary_product_rejects_caller_certainty_upgrade(
    monkeypatch,
    source_certainty: str,
) -> None:
    """The final transaction re-grounds certainty instead of trusting callers."""

    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_id = f"sym:certainty-{source_certainty}"

    async with Session() as session:
        await _seed_ready_head(session, project_id)
        session.add(
            Node(
                id=node_id,
                project_id=project_id,
                kind="Symbol",
                data={"name": node_id, "location": {"file": "certainty.py"}},
                certainty=source_certainty,
                created_by=["test"],
            )
        )
        await session.commit()

    actual_ground = runner_module._ground_result_or_fallback

    async def forge_verified_certainty(*args, **kwargs):
        grounded = await actual_ground(*args, **kwargs)
        assert grounded.claims[0]["evidence"][0]["certainty"] == source_certainty
        grounded.claims[0]["evidence"][0]["certainty"] = "verified"
        return grounded

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        forge_verified_certainty,
    )
    extractor = ParsedGroundingExtractor(fake_id="certainty.py")
    async with Session() as session:
        with pytest.raises(
            LLMLifecycleError,
            match="grounded Summary does not match its semantic candidate",
        ):
            await summarise_l1(
                session,
                extractor,
                project_id=project_id,
                limit=1,
                run_budget=LLMRunBudget(),
            )
        await session.rollback()
        summaries = (
            await session.execute(select(Summary).where(Summary.level == 1))
        ).scalars().all()
        calls = (await session.execute(select(LLMCall))).scalars().all()
        candidates = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalars().all()

    await engine.dispose()
    assert summaries == []
    assert len(calls) == len(candidates) == 1
    assert calls[0].result_status == "pending"
    assert candidates[0].candidate_status == "candidate"


@pytest.mark.asyncio
async def test_atomic_summary_product_uses_locked_receipt_model_and_tokens(
    monkeypatch,
) -> None:
    """Mutable caller provenance cannot replace the durable attempt receipt."""

    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_id = "sym:immutable-product-receipt"

    async with Session() as session:
        await _seed_ready_head(session, project_id)
        session.add(
            Node(
                id=node_id,
                project_id=project_id,
                kind="Symbol",
                data={"name": node_id, "location": {"file": "receipt.py"}},
                certainty="asserted",
                created_by=["test"],
            )
        )
        await session.commit()

    actual_ground = runner_module._ground_result_or_fallback

    async def forge_product_accounting(*args, **kwargs):
        grounded = await actual_ground(*args, **kwargs)
        grounded.model_used = "caller-controlled-model"
        grounded.tokens_used = 999_999
        return grounded

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        forge_product_accounting,
    )
    extractor = ParsedGroundingExtractor(fake_id="receipt.py")
    async with Session() as session:
        assert await summarise_l1(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
        saved = (
            await session.execute(select(Summary).where(Summary.level == 1))
        ).scalar_one()
        call = (await session.execute(select(LLMCall))).scalar_one()

    await engine.dispose()
    assert saved.model_used == "mock-provider"
    assert saved.tokens_used == 7
    assert saved.llm_attempt_id == call.id


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

        assert await summarise_l1(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
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

        assert await summarise_l2(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
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

        assert await summarise_l2(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
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

        assert await summarise_l3(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
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

        assert await summarise_l3(
            session,
            extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(),
        ) == 1
        saved = (await session.execute(select(Summary).where(Summary.level == 3))).scalar_one()

    await engine.dispose()
    assert len(extractor.calls) == 3  # two maps + one reduce
    rollup_evidence = extractor.calls[-1][2]
    assert {row["node_id"] for row in rollup_evidence} == node_ids
    assert all(row["node_id"] != module for row in rollup_evidence)
    assert _approx_tokens(rollup_evidence) <= 4000
    _assert_only_actual_claim(saved, node_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rejected_before_crash",
    [pytest.param(False, id="pending"), pytest.param(True, id="rejected")],
)
async def test_l1_restart_recovers_candidate_before_summary_without_redispatch(
    monkeypatch,
    rejected_before_crash: bool,
) -> None:
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    node_id = "sym:candidate-crash"
    dispatches = 0

    async def create(**_kwargs):
        nonlocal dispatches
        dispatches += 1
        return SimpleNamespace(
            id="msg-summary-candidate-crash",
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage={"input_tokens": 4, "output_tokens": 3},
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "summary": "The crash target is present.",
                            "detailed": "The claim is bound to graph evidence.",
                            "claims": [
                                {
                                    "claim": "The crash target exists.",
                                    "evidence": [
                                        {
                                            "kind": "node",
                                            "node_id": (
                                                "sym:not-in-evidence"
                                                if rejected_before_crash
                                                else node_id
                                            ),
                                            "certainty": "asserted",
                                        }
                                    ],
                                }
                            ],
                            "open_questions": [],
                        }
                    ),
                )
            ],
        )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    async with Session() as session:
        await _seed_ready_head(session, project_id)
        session.add(
            Node(
                id=node_id,
                project_id=project_id,
                kind="Symbol",
                data={
                    "name": "candidate_crash",
                    "location": {"file": "candidate_crash.py"},
                },
                certainty="asserted",
                created_by=["test"],
            )
        )
        await session.commit()

    actual_ground = runner_module._ground_result_or_fallback

    async def crash_after_candidate(*args, **kwargs):
        if rejected_before_crash:
            await actual_ground(*args, **kwargs)
        raise RuntimeError("simulated crash after candidate finalize")

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        crash_after_candidate,
    )
    first_scope = uuid.uuid4()
    first_extractor = Extractor()
    first_extractor._api_key = "test-key"
    async with Session() as session:
        with pytest.raises(RuntimeError, match="after candidate finalize"):
            await summarise_l1(
                session,
                first_extractor,
                project_id=project_id,
                limit=1,
                run_budget=LLMRunBudget(scope_id=first_scope),
            )

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        actual_ground,
    )
    restarted_extractor = Extractor()
    restarted_extractor._api_key = "test-key"
    async with Session() as session:
        assert await summarise_l1(
            session,
            restarted_extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(scope_id=uuid.uuid4()),
        ) == 1
        summaries = (
            await session.execute(select(Summary).where(Summary.level == 1))
        ).scalars().all()
        calls = (await session.execute(select(LLMCall))).scalars().all()
        candidates = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalars().all()

    await engine.dispose()
    assert dispatches == 1
    assert len(summaries) == len(calls) == len(candidates) == 1
    assert summaries[0].tokens_used == 7
    if rejected_before_crash:
        assert summaries[0].fallback_reason == "ungrounded_response"
        assert calls[0].result_status == "grounding_rejected"
        assert candidates[0].candidate_status == "rejected"
    else:
        assert summaries[0].fallback_reason is None
        assert summaries[0].summary == "The crash target is present."
        assert calls[0].result_status == "accepted"
        assert candidates[0].candidate_status == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [2, 3])
async def test_map_partial_candidate_replays_before_rollup_without_redispatch(
    monkeypatch,
    level: int,
) -> None:
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)
    engine, Session = await _new_database()
    project_id = uuid.uuid4()
    dispatch_targets: list[str] = []

    async def create(**kwargs):
        prompt = kwargs["messages"][0]["content"]
        target_id = prompt.splitlines()[0].split(": ", 1)[1]
        evidence = json.loads(_prompt_evidence_json(prompt))
        node_id = next(
            row["node_id"]
            for row in evidence
            if row.get("kind") == "node" and "node_id" in row
        )
        dispatch_targets.append(target_id)
        return SimpleNamespace(
            id=f"msg-{len(dispatch_targets)}",
            model="claude-sonnet-4-6",
            stop_reason="end_turn",
            usage={"input_tokens": 4, "output_tokens": 3},
            content=[
                SimpleNamespace(
                    type="text",
                    text=json.dumps(
                        {
                            "summary": f"Summary for {target_id}.",
                            "detailed": "Grounded map or reduce result.",
                            "claims": [
                                {
                                    "claim": "The cited symbol exists.",
                                    "evidence": [
                                        {
                                            "kind": "node",
                                            "node_id": node_id,
                                            "certainty": "asserted",
                                        }
                                    ],
                                }
                            ],
                            "open_questions": [],
                        }
                    ),
                )
            ],
        )

    provider_module = ModuleType("anthropic")
    provider_module.AsyncAnthropic = lambda **_kwargs: SimpleNamespace(  # type: ignore[attr-defined]
        messages=SimpleNamespace(create=create)
    )
    monkeypatch.setitem(sys.modules, "anthropic", provider_module)
    monkeypatch.setenv("MNEMOS_DISABLE_AGENT_SDK", "1")

    async with Session() as session:
        await _seed_ready_head(session, project_id)
        if level == 2:
            for index, node_id in enumerate(("sym:map-l2-a", "sym:map-l2-b")):
                session.add_all(
                    _node_and_l1(
                        project_id=project_id,
                        node_id=node_id,
                        file_path="large.py",
                        l1_summary=str(index) * 13_000,
                    )
                )
            target = "large.py"
            stage = summarise_l2
        else:
            await _seed_l3(
                session,
                project_id=project_id,
                files=[
                    ("module/a.py", "sym:map-l3-a", "a" * 17_000),
                    ("module/b.py", "sym:map-l3-b", "b" * 17_000),
                ],
            )
            target = "module"
            stage = summarise_l3
        await session.commit()

    actual_ground = runner_module._ground_result_or_fallback
    first_partial = f"{target}#chunk1"
    crash_pending = True

    async def crash_first_partial(*args, **kwargs):
        nonlocal crash_pending
        grounded = await actual_ground(*args, **kwargs)
        if crash_pending and kwargs.get("target_id") == first_partial:
            crash_pending = False
            raise RuntimeError("simulated map-partial crash")
        return grounded

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        crash_first_partial,
    )
    first_extractor = Extractor()
    first_extractor._api_key = "test-key"
    async with Session() as session:
        with pytest.raises(RuntimeError, match="map-partial crash"):
            await stage(
                session,
                first_extractor,
                project_id=project_id,
                limit=1,
                run_budget=LLMRunBudget(scope_id=uuid.uuid4()),
            )

    monkeypatch.setattr(
        runner_module,
        "_ground_result_or_fallback",
        actual_ground,
    )
    restarted_extractor = Extractor()
    restarted_extractor._api_key = "test-key"
    async with Session() as session:
        assert await stage(
            session,
            restarted_extractor,
            project_id=project_id,
            limit=1,
            run_budget=LLMRunBudget(scope_id=uuid.uuid4()),
        ) == 1
        final = (
            await session.execute(
                select(Summary).where(
                    Summary.level == level,
                    Summary.target_id == target,
                )
            )
        ).scalar_one()
        calls = (await session.execute(select(LLMCall))).scalars().all()
        candidates = (
            await session.execute(select(LLMSemanticCandidate))
        ).scalars().all()

    await engine.dispose()
    assert dispatch_targets.count(first_partial) == 1
    assert len(dispatch_targets) == len(set(dispatch_targets)) == 3
    assert set(dispatch_targets) == {
        first_partial,
        f"{target}#chunk2",
        target,
    }
    assert len(calls) == len(candidates) == 3
    assert {row.candidate_status for row in candidates} == {"accepted"}
    assert final.fallback_reason is None
    assert final.tokens_used == 7
    assert sum(row.total_tokens or 0 for row in calls) == 21
