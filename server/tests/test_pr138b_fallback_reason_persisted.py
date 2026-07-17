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

import app.extractor.runner as runner_module  # noqa: E402
from app.extractor.agent import Extractor, ExtractorResult  # noqa: E402
from app.extractor.agent_sdk import (  # noqa: E402
    SUMMARY_CANDIDATE_CONTRACT,
    current_summary_candidate_context,
    normalize_summary_candidate_payload,
)
from app.extractor.cost import LLMRunBudget  # noqa: E402
from app.llm.contracts import (  # noqa: E402
    AttemptKind,
    AttemptStatus,
    LLMUsageV1,
    ResultStatus,
    UsageSource,
    UsageStatus,
)
from app.llm.lifecycle import (  # noqa: E402
    AttemptOutcome,
    AttemptStartMetadata,
    SemanticCandidate,
    fingerprint_input,
    require_attempt_callbacks,
)
from app.models.base import Base  # noqa: E402
from app.models import auth as _auth  # noqa: E402,F401
from app.models import audit as _audit  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import organization as _org  # noqa: E402,F401
from app.models.findings import Summary  # noqa: E402
from app.models.graph import Node  # noqa: E402
from app.models.projects import Project  # noqa: E402


_PRODUCTION_BEGIN_ATTEMPT = runner_module.begin_attempt


def _use_generic_test_lifecycle(monkeypatch) -> None:  # noqa: ANN001
    """Keep product-only assertions independent of deployment dollar policy."""

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
) -> ExtractorResult:
    """Finalize a deterministic mock through the real Summary candidate ledger."""

    callbacks = require_attempt_callbacks("PR-138b test provider")
    context = current_summary_candidate_context()
    assert context is not None
    assert callbacks.finish_candidate is not None
    ticket = await callbacks.start(
        AttemptStartMetadata(
            operation_id=_uuid.uuid4(),
            attempt_no=1,
            attempt_kind=AttemptKind.PRIMARY,
            provider="test",
            provider_mode="api",
            requested_model=result.model_used,
            input_fingerprint=fingerprint_input(
                "pr138b-test-system",
                target_id,
            ),
            estimated_input_tokens=10,
            input_estimate_method="fixed_test_estimate_v1",
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


class _PaidExtractor(Extractor):
    async def summarize(self, _level, target_id, evidence):  # noqa: ANN001
        return await _finish_summary_candidate(
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
async def test_l1_runner_persists_reason_when_dollar_policy_is_absent(
    session, monkeypatch,
):
    _pin_graph(monkeypatch)
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    proj = Project(
        id=_uuid.uuid4(), name="x", gitlab_project_id=1,
        gitlab_url="https://x", default_branch="main", languages=["py"],
    )
    session.add(proj)
    session.add(Node(
        id="sym:f", project_id=proj.id, kind="Symbol",
        certainty="asserted", data={"name": "f"}, created_by=["t"],
    ))
    await session.commit()

    from app.extractor.runner import summarise_l1

    await summarise_l1(
        session,
        _PaidExtractor(),
        project_id=proj.id,
        limit=5,
        run_budget=LLMRunBudget(),
    )

    fresh = (await session.execute(
        select(Summary).where(
            Summary.project_id == proj.id,
            Summary.target_id == "sym:f",
            Summary.level == 1,
        )
    )).scalar_one()
    # Structured column carries the reason.
    assert fresh.fallback_reason == "project_dollar_budget_required"
    # Encoded model_used stays for back-compat consumers.
    assert fresh.model_used == "stub:project_dollar_budget_required"


@pytest.mark.asyncio
async def test_happy_path_leaves_fallback_reason_null(session, monkeypatch):
    """The column is null when the LLM call actually succeeded.
    We simulate success by patching the extractor to return a
    populated ExtractorResult with no fallback_reason."""
    _pin_graph(monkeypatch)
    _use_generic_test_lifecycle(monkeypatch)
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

    from app.extractor.runner import summarise_l1

    class _GoodExtractor(Extractor):
        async def summarize(self, level, target_id, evidence):
            return await _finish_summary_candidate(
                ExtractorResult(
                    summary="ok",
                    detailed="ok",
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
                ),
                target_id=target_id,
            )

    await summarise_l1(
        session,
        _GoodExtractor(),
        project_id=proj.id,
        limit=5,
        run_budget=LLMRunBudget(),
    )

    row = (await session.execute(
        select(Summary).where(
            Summary.target_id == "sym:happy", Summary.level == 1,
        )
    )).scalar_one()
    assert row.fallback_reason is None
    assert row.model_used == "claude-sonnet-4-6"
