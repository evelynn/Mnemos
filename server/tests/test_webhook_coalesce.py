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
async def test_never_touches_another_projects_queued_runs():
    """The project guard is the highest-consequence predicate here.

    This UPDATE terminalizes rows and carries no LIMIT, so losing the
    project scope would silently cancel every other tenant's queued
    webhook runs.
    """
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    earlier = now - timedelta(seconds=60)

    async with Session() as session:
        for index, project_id in enumerate((mine, theirs)):
            session.add(
                Project(
                    id=project_id,
                    name=f"tenant-{index}",
                    gitlab_project_id=93100 + index,
                    gitlab_url=f"https://example.invalid/tenant-{index}",
                    default_branch="main",
                    languages=["python"],
                )
            )
        other_tenant_queued = _run(
            theirs,
            status="queued",
            triggered_by="webhook:gitlab:other",
            created_at=earlier,
        )
        mine_queued = _run(
            mine,
            status="queued",
            triggered_by="webhook:gitlab:mine-old",
            created_at=earlier,
        )
        new_run = _run(
            mine,
            status="queued",
            triggered_by="webhook:gitlab:mine-new",
            created_at=now,
        )
        session.add_all([other_tenant_queued, mine_queued, new_run])
        await session.commit()

        count = await _supersede_older_queued_webhook_runs(
            session, project_id=mine, new_run=new_run
        )

    assert count == 1, "only this project's older queued run may be superseded"
    async with Session() as session:
        untouched = await session.get(AnalysisRun, other_tenant_queued.id)
        assert untouched is not None
        assert untouched.status == "queued"
        assert untouched.error_log is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_never_cancels_a_newer_push():
    """The ``created_at <`` guard, pinned on its own.

    A run created *after* the one being processed represents a newer push
    and must outlive it — otherwise a burst could cancel its own winner.
    """
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
                name="newer-push",
                gitlab_project_id=93200,
                gitlab_url="https://example.invalid/newer-push",
                default_branch="main",
                languages=["python"],
            )
        )
        middle_run = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:middle",
            created_at=now,
        )
        newer_run = _run(
            project_id,
            status="queued",
            triggered_by="webhook:gitlab:newer",
            created_at=now + timedelta(seconds=30),
        )
        session.add_all([middle_run, newer_run])
        await session.commit()

        count = await _supersede_older_queued_webhook_runs(
            session, project_id=project_id, new_run=middle_run
        )

    assert count == 0, "a newer push's run must never be superseded"
    async with Session() as session:
        survivor = await session.get(AnalysisRun, newer_run.id)
        assert survivor is not None
        assert survivor.status == "queued"
        assert survivor.error_log is None
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
