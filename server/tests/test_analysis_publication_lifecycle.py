"""Publication receipt and post-processing lifecycle regressions."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("SECRET_KEY", "analysis-publication-lifecycle-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.models import (  # noqa: E402,F401
    audit,
    auth,
    comments,
    findings,
    graph,
    onboarding,
    organization,
    overlays,
    plans,
    projects,
    runtime,
    samples,
    stages,
)
from app.graph_publication import GraphPublicationInvariantError  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.models.stages import AnalysisStage  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def publish(self, _run_id: uuid.UUID, event: dict[str, Any]) -> None:
        self.events.append(dict(event))


@pytest_asyncio.fixture
async def database(tmp_path: Path):
    db_path = tmp_path / "publication-lifecycle.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path.as_posix()}",
        poolclass=NullPool,
    )
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine, session_factory
    finally:
        await engine.dispose()


def _receipt(published_at: datetime, *, generation: int = 1) -> dict[str, Any]:
    return {
        "contract_version": "mnemos.graph-publication.v1",
        "generation": generation,
        "base_generation": generation - 1,
        "published_at": published_at.isoformat(),
        "authoritative_sources": ["ggoss-py"],
        "coverage_sealed_at": published_at.isoformat(),
        "counts": {
            "nodes_inserted": 0,
            "nodes_closed": 0,
            "nodes_unchanged": 0,
            "nodes_downgrade_ignored": 0,
            "edges_inserted": 0,
            "edges_closed": 0,
            "edges_unchanged": 0,
            "edges_downgrade_ignored": 0,
            "stale_nodes_closed": 0,
            "stale_edges_closed": 0,
        },
        "atomic": True,
    }


async def _seed_published(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    receipt_generation: int = 1,
    head_generation: int = 1,
    published_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, datetime]:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    if published_at is None:
        published_at = datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name=f"lifecycle-{project_id.hex[:8]}",
                gitlab_project_id=project_id.int % 2_000_000_000,
                gitlab_url="https://example.invalid/lifecycle",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="published",
                triggered_by="test:lifecycle",
                git_sha="a" * 40,
                scope="full",
                started_at=published_at,
                completed_at=None,
                graph_base_generation=receipt_generation - 1,
                graph_authoritative_sources=["ggoss-py"],
                graph_coverage_sealed_at=published_at,
                stats={
                    "job_id": f"analysis:{run_id}",
                    "graph_publication": _receipt(
                        published_at,
                        generation=receipt_generation,
                    ),
                    "history_retention": {
                        "automatic_prune": False,
                        "reason": "bitemporal_compare_contract",
                    },
                },
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=head_generation,
                state="ready",
                published_at=published_at,
            )
        )
        await session.commit()
    return project_id, run_id, published_at


def _wire_sessions(monkeypatch: pytest.MonkeyPatch, session_factory) -> None:
    from app.orchestrator import jobs, stages as stage_module

    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    monkeypatch.setattr(stage_module, "SessionLocal", session_factory)


@pytest.mark.asyncio
async def test_resume_rejects_published_receipt_head_generation_mismatch(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(
        session_factory,
        receipt_generation=1,
        head_generation=2,
    )
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    with pytest.raises(
        GraphPublicationInvariantError,
        match="head generation does not match",
    ):
        await jobs._run_published_postprocess(
            _Bus(),
            project_id=project_id,
            run_id=run_id,
            summarize=False,
            l1_limit=0,
            l2_limit=0,
            l3_limit=0,
            position=0,
        )
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and run.status == "published"
        assert run.completed_at is None
        assert head is not None and head.generation == 2


@pytest.mark.asyncio
async def test_run_ingest_resumes_valid_published_postprocess_without_reindexing(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async def _acquired(*_args: Any, **_kwargs: Any) -> bool:
        return True

    async def _released(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _runtime(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"matched": 0, "unmatched": 0}

    async def _findings(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"created": 0}

    async def _supersede(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _audit(**_kwargs: Any) -> None:
        return None

    async def _must_not_prepare(*_args: Any, **_kwargs: Any):
        raise AssertionError("published retry attempted to re-index source")

    monkeypatch.setattr(jobs, "_acquire_project_lock", _acquired)
    monkeypatch.setattr(jobs, "_release_project_lock", _released)
    monkeypatch.setattr(jobs, "reconcile_observations", _runtime)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "_supersede_stale_graph_summaries", _supersede)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    monkeypatch.setattr(jobs, "prepare_source", _must_not_prepare)
    bus = _Bus()
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(run_id),
        "C:/does-not-need-to-exist",
        {"scope": "full", "summarize": False},
    )
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None and run.status == "completed"
        assert run.completed_at is not None
    assert any(event.get("event") == "run_published" for event in bus.events)
    assert any(event.get("event") == "run_completed" for event in bus.events)


@pytest.mark.asyncio
async def test_published_postprocess_reuses_supplied_exhausted_llm_budget(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.extractor.cost import LLMRunBudget, RunBudgetExceeded
    from app.orchestrator import jobs

    async def _runtime(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"matched": 0, "unmatched": 0}

    async def _findings(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"created": 0}

    async def _supersede(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _audit(**_kwargs: Any) -> None:
        return None

    budget = LLMRunBudget(
        max_calls=1,
        max_input_tokens=10_000,
        wall_time_sec=60,
    )
    budget.reserve(100)  # Simulate the sole allowance spent by Agent extraction.
    with pytest.raises(RunBudgetExceeded, match="run_call_limit_exceeded"):
        budget.reserve(1)

    seen_budgets: list[LLMRunBudget] = []

    async def _summary(*_args: Any, run_budget=None, **_kwargs: Any) -> int:
        seen_budgets.append(run_budget)
        assert run_budget is budget
        assert run_budget.exhausted_reason == "run_call_limit_exceeded"
        return 0

    def _must_not_replace_budget():
        raise AssertionError("postprocess replaced the supplied source-run budget")

    monkeypatch.setattr(jobs, "reconcile_observations", _runtime)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "_supersede_stale_graph_summaries", _supersede)
    monkeypatch.setattr(jobs, "summarise_l1", _summary)
    monkeypatch.setattr(jobs, "summarise_l2", _summary)
    monkeypatch.setattr(jobs, "summarise_l3", _summary)
    monkeypatch.setattr(jobs, "Extractor", lambda: object())
    monkeypatch.setattr(jobs.LLMRunBudget, "from_env", _must_not_replace_budget)
    monkeypatch.setattr(jobs, "audit_record", _audit)

    result = await jobs._run_published_postprocess(
        _Bus(),
        project_id=project_id,
        run_id=run_id,
        summarize=True,
        l1_limit=25,
        l2_limit=25,
        l3_limit=25,
        position=0,
        llm_run_budget=budget,
    )

    assert result == "completed"
    assert seen_budgets == [budget, budget, budget]
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        persisted = run.stats["ai_narration"]["run_budget"]
        assert persisted["calls_started"] == 1
        assert persisted["estimated_input_tokens"] == 100
        assert persisted["exhausted_reason"] == "run_call_limit_exceeded"


async def _assert_published_prelock_failure_preserved_source(
    session_factory,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    published_at: datetime,
    bus: _Bus,
) -> None:
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and run.status == "partial"
        assert run.stats["graph_publication"] == _receipt(published_at)
        assert head is not None and head.current_run_id == run_id
        assert head.generation == 1
        head_published_at = head.published_at
        assert head_published_at is not None
        if head_published_at.tzinfo is None:
            head_published_at = head_published_at.replace(tzinfo=timezone.utc)
        assert head_published_at == published_at
    assert any(event.get("event") == "run_partial" for event in bus.events)
    assert not any(event.get("event") == "run_failed" for event in bus.events)


@pytest.mark.asyncio
async def test_published_lock_acquire_exception_closes_partial_not_failed(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, published_at = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async def _raise_lock(*_args: Any, **_kwargs: Any) -> bool:
        raise ConnectionError("redis unavailable")

    async def _released(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(jobs, "_acquire_project_lock", _raise_lock)
    monkeypatch.setattr(jobs, "_release_project_lock", _released)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    bus = _Bus()

    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(run_id),
        "C:/unused",
        {"scope": "full", "summarize": False},
    )

    await _assert_published_prelock_failure_preserved_source(
        session_factory,
        project_id=project_id,
        run_id=run_id,
        published_at=published_at,
        bus=bus,
    )


@pytest.mark.parametrize("failure_mode", ["enqueue_error", "exhausted"])
@pytest.mark.asyncio
async def test_published_lock_retry_failure_closes_partial_not_failed(
    database,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
):
    _, session_factory = database
    project_id, run_id, published_at = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async def _not_acquired(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def _not_reclaimed(*_args: Any, **_kwargs: Any) -> bool:
        return False

    async def _audit(**_kwargs: Any) -> None:
        return None

    class _BrokenQueue:
        async def enqueue_job(self, *_args: Any, **_kwargs: Any) -> None:
            raise ConnectionError("queue unavailable")

    monkeypatch.setattr(jobs, "_acquire_project_lock", _not_acquired)
    monkeypatch.setattr(jobs, "_reclaim_terminal_project_lock", _not_reclaimed)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    options: dict[str, Any] = {"scope": "full", "summarize": False}
    if failure_mode == "exhausted":
        options["_project_lock_retry"] = jobs._PROJECT_LOCK_MAX_RETRIES
    bus = _Bus()

    await jobs.run_ingest(
        {"progress": bus, "redis": _BrokenQueue()},
        str(project_id),
        str(run_id),
        "C:/unused",
        options,
    )

    await _assert_published_prelock_failure_preserved_source(
        session_factory,
        project_id=project_id,
        run_id=run_id,
        published_at=published_at,
        bus=bus,
    )


@pytest.mark.asyncio
async def test_published_cancel_requests_postprocess_then_closes_partial(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, published_at = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.api import analysis
    from app.orchestrator import jobs

    bus = _Bus()
    aborted: list[str] = []

    class _Queue:
        async def abort_job(self, job_id: str) -> bool:
            aborted.append(job_id)
            return True

    async def _queue() -> _Queue:
        return _Queue()

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(analysis, "ProgressBus", lambda: bus)
    monkeypatch.setattr(analysis, "get_queue", _queue)
    monkeypatch.setattr(analysis, "audit_record", _audit)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    async with session_factory() as session:
        response = await analysis.cancel_run(
            run_id,
            SimpleNamespace(id=uuid.uuid4()),
            session,
        )
    assert response == {"status": "published", "cancel_requested": "postprocess"}
    assert aborted == [f"analysis:{run_id}"]

    result = await jobs._run_published_postprocess(
        bus,
        project_id=project_id,
        run_id=run_id,
        summarize=False,
        l1_limit=0,
        l2_limit=0,
        l3_limit=0,
        position=0,
    )
    assert result == "partial"
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and run.status == "partial"
        assert run.completed_at is not None
        assert run.completed_at != published_at
        assert run.stats["postprocess"]["cancel_requested"] is True
        assert run.stats["postprocess_error"]["cancelled"] is True
        assert head is not None and head.current_run_id == run_id
        assert head.generation == 1
    assert any(event.get("event") == "run_cancel_requested" for event in bus.events)
    assert any(event.get("event") == "run_partial" for event in bus.events)


@pytest.mark.asyncio
async def test_findings_failure_is_bounded_partial_and_preserves_head(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async def _runtime(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"matched": 2, "unmatched": 0}

    secret = "provider-token=postprocess-secret source=private-source-fragment"

    async def _findings(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        raise RuntimeError(f"{secret} {'x' * 5000}")

    async def _supersede(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(jobs, "reconcile_observations", _runtime)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "_supersede_stale_graph_summaries", _supersede)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    bus = _Bus()
    result = await jobs._run_published_postprocess(
        bus,
        project_id=project_id,
        run_id=run_id,
        summarize=False,
        l1_limit=0,
        l2_limit=0,
        l3_limit=0,
        position=0,
    )
    assert result == "partial"
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        finding_stage = (
            await session.execute(
                select(AnalysisStage).where(
                    AnalysisStage.run_id == run_id,
                    AnalysisStage.name == "findings",
                )
            )
        ).scalar_one()
        assert run is not None and run.status == "partial"
        assert run.stats["postprocess"]["findings_freshness"]["status"] == "unknown"
        assert run.stats["postprocess_error"]["type"] == "internal_failure"
        assert run.stats["postprocess_error"]["code"] == "postprocess_internal_failure"
        assert run.stats["postprocess_error"]["message"] == "postprocess_internal_failure"
        assert run.error_log == "postprocess_internal_failure"
        assert head is not None and head.current_run_id == run_id
        assert head.generation == 1
        assert finding_stage.error_log == "stage_internal_failure"
    partial_event = next(event for event in bus.events if event["event"] == "run_partial")
    assert partial_event["error"] == "postprocess_internal_failure"
    stage_event = next(event for event in bus.events if event["event"] == "stage_failed")
    assert stage_event["error"] == "stage_internal_failure"
    projections = json.dumps(
        {
            "run_error": run.error_log,
            "run_stats": run.stats,
            "stage_error": finding_stage.error_log,
            "events": bus.events,
        },
        sort_keys=True,
    )
    assert "postprocess-secret" not in projections
    assert "private-source-fragment" not in projections
    baseline = await jobs._current_published_stats(project_id, uuid.uuid4())
    assert baseline is not None
    assert baseline["graph_publication"]["generation"] == 1


@pytest.mark.asyncio
async def test_runtime_failure_marks_successful_findings_freshness_partial(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async def _runtime(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        raise ConnectionError("runtime buffer unavailable")

    async def _findings(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"created": 1}

    async def _supersede(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(jobs, "reconcile_observations", _runtime)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "_supersede_stale_graph_summaries", _supersede)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    result = await jobs._run_published_postprocess(
        _Bus(),
        project_id=project_id,
        run_id=run_id,
        summarize=False,
        l1_limit=0,
        l2_limit=0,
        l3_limit=0,
        position=0,
    )
    assert result == "partial"
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        freshness = run.stats["postprocess"]["findings_freshness"]
        assert freshness == {
            "status": "partial",
            "reason": "runtime_reconcile_failed",
        }


@pytest.mark.asyncio
async def test_completed_at_is_written_only_when_postprocess_finishes(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, run_id, _ = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    from app.orchestrator import jobs

    async with session_factory() as session:
        before = await session.get(AnalysisRun, run_id)
        assert before is not None and before.completed_at is None

    async def _runtime(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"matched": 0, "unmatched": 0}

    async def _findings(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"created": 0}

    async def _supersede(*_args: Any, **_kwargs: Any) -> int:
        return 0

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(jobs, "reconcile_observations", _runtime)
    monkeypatch.setattr(jobs, "rebuild_findings", _findings)
    monkeypatch.setattr(jobs, "_supersede_stale_graph_summaries", _supersede)
    monkeypatch.setattr(jobs, "audit_record", _audit)
    assert await jobs._run_published_postprocess(
        _Bus(),
        project_id=project_id,
        run_id=run_id,
        summarize=False,
        l1_limit=0,
        l2_limit=0,
        l3_limit=0,
        position=0,
    ) == "completed"
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None and run.status == "completed"
        assert run.completed_at is not None
        assert run.stats["postprocess"]["status"] == "completed"


@pytest.mark.asyncio
async def test_close_downgrades_to_partial_if_head_changed_after_derived_reads(
    database,
    monkeypatch: pytest.MonkeyPatch,
):
    _, session_factory = database
    project_id, old_run_id, published_at = await _seed_published(session_factory)
    _wire_sessions(monkeypatch, session_factory)
    successor_id = uuid.uuid4()
    successor_at = published_at + timedelta(minutes=5)
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=successor_id,
                project_id=project_id,
                status="completed",
                triggered_by="test:successor",
                git_sha="b" * 40,
                scope="full",
                started_at=successor_at - timedelta(minutes=1),
                completed_at=successor_at + timedelta(minutes=1),
                graph_base_generation=1,
                graph_authoritative_sources=["ggoss-py"],
                graph_coverage_sealed_at=successor_at,
                stats={
                    "graph_publication": _receipt(successor_at, generation=2),
                },
            )
        )
        await session.flush()
        head = await session.get(GraphHead, project_id)
        assert head is not None
        head.current_run_id = successor_id
        head.generation = 2
        head.published_at = successor_at
        await session.commit()

    from app.orchestrator import jobs

    async def _audit(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(jobs, "audit_record", _audit)
    result = await jobs._close_published_run(
        _Bus(),
        project_id=project_id,
        run_id=old_run_id,
        final_status="completed",
        derived_stats={
            "postprocess": {
                "findings_freshness": {"status": "fresh", "reason": "tested"}
            }
        },
        postprocess_errors=[],
    )
    assert result == "partial"
    async with session_factory() as session:
        old_run = await session.get(AnalysisRun, old_run_id)
        head = await session.get(GraphHead, project_id)
        assert old_run is not None and old_run.status == "partial"
        assert old_run.stats["postprocess_error"]["stage"] == "finalize"
        assert old_run.stats["postprocess"]["findings_freshness"] == {
            "status": "unknown",
            "reason": "publication_head_changed_before_close",
        }
        assert head is not None and head.current_run_id == successor_id
        assert head.generation == 2


def test_analysis_stats_disables_automatic_history_pruning():
    from app.orchestrator import jobs

    assert not hasattr(jobs, "prune_graph_history")
    stats = jobs._compose_analysis_stats(
        project_id=uuid.uuid4(),
        totals={},
        inventory=None,
        inherited_source_snapshot=None,
        source_manifest=None,
        prepared_source=None,
        source_ref=None,
        run_git_sha="",
        producer_coverage={},
        legacy_database_languages=[],
        scope="full",
        requested_scope="full",
        selected_analyzers=set(),
        removed_analyzers=set(),
        sweep_sources=set(),
        deletion_sweep_deferred_reason=None,
        previous_stats=None,
        summarize=False,
        agent_limit=0,
        llm_run_budget=None,
        seen_index=None,
    )
    assert stats["history_retention"] == {
        "automatic_prune": False,
        "reason": "bitemporal_compare_contract",
    }


@pytest.mark.asyncio
async def test_stale_recovery_partializes_orphan_published_without_moving_head(
    database,
):
    _, session_factory = database
    # Backdate the WHOLE publication (started == sealed == published), not
    # just started_at: a fixed seed date with a relative started_at rewrite
    # tripped the "coverage seal precedes the run start" receipt invariant
    # once the calendar passed the seed date (time-bomb fixture).
    project_id, run_id, _ = await _seed_published(
        session_factory,
        published_at=datetime.now(tz=timezone.utc) - timedelta(days=2),
    )

    from app.orchestrator.cron_jobs import _partialize_stale_published_runs

    async with session_factory() as session:
        result = await _partialize_stale_published_runs(session)
    assert result == {"partial": 1}
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        head = await session.get(GraphHead, project_id)
        assert run is not None and run.status == "partial"
        assert run.stats["postprocess_error"]["stage"] == "stale_recovery"
        assert head is not None and head.current_run_id == run_id
        assert head.generation == 1
