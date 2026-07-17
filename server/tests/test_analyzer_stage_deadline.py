"""Hard analyzer-stage deadlines include downstream graph persistence.

The subprocess runner already has a watchdog, but a child can exit while a
record-level SELECT/upsert/commit remains blocked.  These tests pin the outer
deadline and prove both timeout and operator cancellation close the producer
stream and leave a terminal stage row.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "analyzer-stage-deadline-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.models import audit as _audit  # noqa: E402,F401
from app.models import auth as _auth  # noqa: E402,F401
from app.models import comments as _comments  # noqa: E402,F401
from app.models import findings as _findings  # noqa: E402,F401
from app.models import graph as _graph  # noqa: E402,F401
from app.models import onboarding as _onboarding  # noqa: E402,F401
from app.models import organization as _organization  # noqa: E402,F401
from app.models import plans as _plans  # noqa: E402,F401
from app.models import projects as _projects  # noqa: E402,F401
from app.models import runtime as _runtime  # noqa: E402,F401
from app.models import samples as _samples  # noqa: E402,F401
from app.models import stages as _stages  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.models.stages import AnalysisStage  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, _run_id, event) -> None:
        self.events.append(event)


class _OneRecordRunner:
    def __init__(self) -> None:
        self.closed = False

    async def run(self, _verb, _path, **_kwargs):
        from app.analyzers.runner import RunRecord

        try:
            yield RunRecord(
                stream="stdout",
                payload={
                    "record_type": "symbol",
                    "source_name": "ggoss-py",
                    "data": {"id": "py:service.py::answer", "name": "answer"},
                },
            )
            await asyncio.Future()
        finally:
            self.closed = True


async def _database(monkeypatch):
    from app.orchestrator import jobs, stages

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", sessions)
    monkeypatch.setattr(stages, "SessionLocal", sessions)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with sessions() as session:
        session.add(
            Project(
                id=project_id,
                name="stage-deadline",
                gitlab_project_id=88001,
                gitlab_url="https://example.invalid/stage-deadline",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="running",
                triggered_by="test",
                git_sha="a" * 40,
                scope="incremental",
                started_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.commit()
    return engine, sessions, project_id, run_id


def _totals() -> dict[str, int]:
    return {
        "symbols": 0,
        "edges": 0,
        "contracts": 0,
        "data_entities": 0,
        "errors": 0,
    }


@pytest.mark.asyncio
async def test_hard_deadline_covers_blocked_record_ingest(monkeypatch, tmp_path):
    from app.orchestrator import jobs
    from app.orchestrator.stages import StageBudgetExceeded

    engine, sessions, project_id, run_id = await _database(monkeypatch)
    runner = _OneRecordRunner()
    entered_ingest = asyncio.Event()

    async def _blocked_ingest(*_args, **_kwargs):
        entered_ingest.set()
        await asyncio.Future()

    monkeypatch.setattr(jobs, "runner_for", lambda _language: runner)
    monkeypatch.setattr(jobs, "analyzer_available", lambda _language: True)
    monkeypatch.setattr(jobs, "_record_payload", _blocked_ingest)
    bus = _Bus()

    with pytest.raises(StageBudgetExceeded, match="hard deadline"):
        await jobs._run_analyzer_stage(
            bus,
            project_id,
            run_id,
            "python",
            "symbols",
            str(tmp_path),
            1,
            _totals(),
            stage_timeout_s=0.05,
        )

    assert entered_ingest.is_set()
    assert runner.closed is True
    async with sessions() as session:
        stage = (await session.execute(select(AnalysisStage))).scalar_one()
        assert stage.status == "partial"
        assert stage.error_log == "stage_budget_exceeded"
    assert any(event.get("event") == "stage_partial" for event in bus.events)
    await engine.dispose()


@pytest.mark.asyncio
async def test_cancellation_closes_stream_and_terminalizes_stage(monkeypatch, tmp_path):
    from app.orchestrator import jobs

    engine, sessions, project_id, run_id = await _database(monkeypatch)
    runner = _OneRecordRunner()
    entered_ingest = asyncio.Event()

    async def _blocked_ingest(*_args, **_kwargs):
        entered_ingest.set()
        await asyncio.Future()

    monkeypatch.setattr(jobs, "runner_for", lambda _language: runner)
    monkeypatch.setattr(jobs, "analyzer_available", lambda _language: True)
    monkeypatch.setattr(jobs, "_record_payload", _blocked_ingest)
    bus = _Bus()
    task = asyncio.create_task(
        jobs._run_analyzer_stage(
            bus,
            project_id,
            run_id,
            "python",
            "symbols",
            str(tmp_path),
            1,
            _totals(),
            stage_timeout_s=60,
        )
    )
    await asyncio.wait_for(entered_ingest.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert runner.closed is True
    async with sessions() as session:
        stage = (await session.execute(select(AnalysisStage))).scalar_one()
        assert stage.status == "cancelled"
    assert any(event.get("event") == "stage_cancelled" for event in bus.events)
    await engine.dispose()
