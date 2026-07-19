"""Enqueue-time coalescing of webhook push bursts.

A burst of pushes used to leave every older still-queued webhook run for
the worker to pick up and cancel one by one at execution time. The new
enqueue-time supersede cancels them as soon as the replacement run has a
committed row and a live job — with a CAS-style ``status == "queued"``
guard so running/manual/newer runs are never touched.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "webhook-coalesce-test")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.models import (  # noqa: E402,F401 — register every mapper
    audit,
    auth,
    comments,
    findings,
    graph,
    onboarding,
    organization,
    plans,
    projects,
    runtime,
    samples,
    stages,
)
from app.api.webhooks import _supersede_older_queued_webhook_runs  # noqa: E402
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


def _run(
    project_id: uuid.UUID,
    *,
    status: str,
    triggered_by: str,
    created_at: datetime,
) -> AnalysisRun:
    return AnalysisRun(
        id=uuid.uuid4(),
        project_id=project_id,
        status=status,
        triggered_by=triggered_by,
        git_sha="d" * 40,
        scope="incremental",
        started_at=created_at,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_burst_supersedes_only_older_queued_webhook_runs():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    earlier = now - timedelta(seconds=60)

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="coalesce",
                gitlab_project_id=93001,
                gitlab_url="https://example.invalid/coalesce",
                default_branch="main",
                languages=["python"],
            )
        )
        old_queued_1 = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:a",
            created_at=earlier,
        )
        old_queued_2 = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:b",
            created_at=earlier,
        )
        manual_queued = _run(
            project_id,
            status="queued",
            triggered_by="manual:operator",
            created_at=earlier,
        )
        webhook_running = _run(
            project_id,
            status="running",
            triggered_by="webhook:gitlab:c",
            created_at=earlier,
        )
        new_run = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:d",
            created_at=now,
        )
        session.add_all(
            [old_queued_1, old_queued_2, manual_queued, webhook_running, new_run]
        )
        await session.commit()

        count = await _supersede_older_queued_webhook_runs(
            session, project_id=project_id, new_run=new_run
        )

    assert count == 2
    async with Session() as session:
        for run_id in (old_queued_1.id, old_queued_2.id):
            row = await session.get(AnalysisRun, run_id)
            assert row is not None
            assert row.status == "cancelled"
            assert row.error_log == f"superseded_by_newer_webhook:{new_run.id}"
            assert row.completed_at is not None
        untouched = {
            manual_queued.id: "queued",
            webhook_running.id: "running",
            new_run.id: "queued",
        }
        for run_id, expected_status in untouched.items():
            row = await session.get(AnalysisRun, run_id)
            assert row is not None
            assert row.status == expected_status
            assert row.error_log is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_older_queued_runs_is_a_noop():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="coalesce-noop",
                gitlab_project_id=93002,
                gitlab_url="https://example.invalid/coalesce-noop",
                default_branch="main",
                languages=["python"],
            )
        )
        new_run = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:solo",
            created_at=now,
        )
        session.add(new_run)
        await session.commit()

        count = await _supersede_older_queued_webhook_runs(
            session, project_id=project_id, new_run=new_run
        )

    assert count == 0
    await engine.dispose()
