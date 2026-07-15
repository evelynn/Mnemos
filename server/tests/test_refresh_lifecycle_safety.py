from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "refresh-lifecycle-safety-test-key")
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
from app.source_snapshot import find_unpublished_refresh  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, _run_id, event) -> None:
        self.events.append(event)


async def _database(monkeypatch):
    from app.orchestrator import jobs

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    monkeypatch.setattr(jobs, "SessionLocal", session_factory)
    return engine, session_factory


async def _seed_run(
    session_factory,
    *,
    stats=None,
    triggered_by: str = "test:manual",
):
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="lifecycle",
                gitlab_project_id=73001,
                gitlab_url="https://example.invalid/lifecycle",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="queued",
                triggered_by=triggered_by,
                git_sha="a" * 40,
                scope="incremental",
                stats=stats,
            )
        )
        await session.commit()
    return project_id, run_id


@pytest.mark.parametrize(
    ("triggered_by", "path", "expected_error"),
    [
        (
            "webhook:gitlab:test",
            "C:/forged-manual-source",
            "webhook_source_path_must_be_empty",
        ),
        ("test:manual", "", "manual_source_path_required"),
    ],
)
@pytest.mark.asyncio
async def test_worker_rejects_forged_source_mode_before_project_lock(
    monkeypatch,
    triggered_by,
    path,
    expected_error,
):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    project_id, run_id = await _seed_run(
        session_factory,
        triggered_by=triggered_by,
    )
    lock_called = False

    async def _lock_must_not_run(_project_id, _run_id):
        nonlocal lock_called
        lock_called = True
        raise AssertionError("invalid source mode must fail before project lock")

    monkeypatch.setattr(jobs, "_acquire_project_lock", _lock_must_not_run)
    await jobs.run_ingest(
        {"progress": _Bus()},
        str(project_id),
        str(run_id),
        path,
    )

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
    await engine.dispose()
    assert lock_called is False
    assert run is not None and run.status == "failed"
    assert run.error_log == expected_error


@pytest.mark.asyncio
async def test_project_lock_contention_requeues_without_losing_revision(
    monkeypatch,
):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    project_id, run_id = await _seed_run(
        session_factory, stats={"job_id": "analysis:initial"}
    )
    calls = []

    class _Queue:
        async def enqueue_job(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(job_id=kwargs["_job_id"])

    async def _busy(_project_id, _run_id):
        return False

    async def _not_reclaimable(_project_id):
        return False

    monkeypatch.setattr(jobs, "_acquire_project_lock", _busy)
    monkeypatch.setattr(jobs, "_reclaim_terminal_project_lock", _not_reclaimable)
    bus = _Bus()
    await jobs.run_ingest(
        {"progress": bus, "redis": _Queue()},
        str(project_id),
        str(run_id),
        "C:/source",
        {
            "scope": "incremental",
            "git_sha": "a" * 40,
            "ref": "refs/heads/main",
        },
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:4] == (
        "run_ingest",
        str(project_id),
        str(run_id),
        "C:/source",
    )
    assert args[4]["git_sha"] == "a" * 40
    assert args[4]["_project_lock_retry"] == 1
    assert kwargs["_job_id"] == f"analysis-lock-retry:{run_id}:1"
    assert kwargs["_defer_by"] == jobs._PROJECT_LOCK_RETRY_DELAY_SEC

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        assert run.status == "queued"
        assert run.started_at is None
        assert run.completed_at is None
        assert run.error_log is None
        assert run.stats["job_id"] == kwargs["_job_id"]
        assert run.stats["project_lock_wait"]["attempt"] == 1
        # A job that never owned the mutation lock cannot have mixed graph
        # generations and must not trigger the unpublished-refresh guard.
        assert (
            await find_unpublished_refresh(session, project_id=project_id)
        ) is None
    assert any(event.get("event") == "run_deferred" for event in bus.events)
    await engine.dispose()


@pytest.mark.asyncio
async def test_project_lock_retry_enqueue_failure_has_no_mixed_snapshot_block(
    monkeypatch,
):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    project_id, run_id = await _seed_run(session_factory)

    class _BrokenQueue:
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("redis unavailable")

    async def _busy(_project_id, _run_id):
        return False

    async def _not_reclaimable(_project_id):
        return False

    monkeypatch.setattr(jobs, "_acquire_project_lock", _busy)
    monkeypatch.setattr(jobs, "_reclaim_terminal_project_lock", _not_reclaimable)
    bus = _Bus()
    await jobs.run_ingest(
        {"progress": bus, "redis": _BrokenQueue()},
        str(project_id),
        str(run_id),
        "C:/source",
        {"scope": "incremental"},
    )

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.started_at is None
        assert run.completed_at is not None
        assert "lock_retry_enqueue_failed:ConnectionError" in run.error_log
        assert (
            await find_unpublished_refresh(session, project_id=project_id)
        ) is None
    await engine.dispose()


@pytest.mark.asyncio
async def test_direct_job_without_options_retries_as_incremental(monkeypatch):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    project_id, run_id = await _seed_run(session_factory)
    calls = []

    class _Queue:
        async def enqueue_job(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(job_id=kwargs["_job_id"])

    async def _busy(_project_id, _run_id):
        return False

    async def _not_reclaimable(_project_id):
        return False

    monkeypatch.setattr(jobs, "_acquire_project_lock", _busy)
    monkeypatch.setattr(jobs, "_reclaim_terminal_project_lock", _not_reclaimable)
    await jobs.run_ingest(
        {"progress": _Bus(), "redis": _Queue()},
        str(project_id),
        str(run_id),
        "C:/source",
    )

    assert calls[0][0][4]["scope"] == "incremental"
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None and run.status == "queued"
        assert run.started_at is None
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("owner_status", "expected"),
    [("completed", True), ("failed", True), ("running", False), ("cancelled", False)],
)
async def test_only_definitively_stopped_owner_lock_is_reclaimed(
    monkeypatch, owner_status, expected
):
    from app.orchestrator import jobs, redis_pool

    engine, session_factory = await _database(monkeypatch)
    project_id, _waiting_run_id = await _seed_run(session_factory)
    owner_run_id = uuid.uuid4()
    async with session_factory() as session:
        session.add(
            AnalysisRun(
                id=owner_run_id,
                project_id=project_id,
                status=owner_status,
                triggered_by="test:owner",
                git_sha="b" * 40,
                scope="incremental",
            )
        )
        await session.commit()

    class _Redis:
        async def get(self, key):
            assert key == f"mnemos:analysis-lock:{project_id}"
            return str(owner_run_id)

    async def _redis():
        return _Redis()

    released = []

    async def _release(pid, rid):
        released.append((pid, rid))

    monkeypatch.setattr(redis_pool, "get_redis", _redis)
    monkeypatch.setattr(jobs, "_release_project_lock", _release)
    assert await jobs._reclaim_terminal_project_lock(project_id) is expected
    assert released == ([(project_id, owner_run_id)] if expected else [])
    await engine.dispose()


@pytest.mark.asyncio
async def test_seen_index_init_failure_terminalizes_and_releases_project_lock(
    monkeypatch,
):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    project_id, run_id = await _seed_run(session_factory)
    released = []

    async def _acquired(_project_id, _run_id):
        return True

    async def _release(pid, rid):
        released.append((pid, rid))

    class _BrokenSeenIndex:
        def __init__(self, **_kwargs):
            raise OSError("temporary index unavailable")

    monkeypatch.setattr(jobs, "_acquire_project_lock", _acquired)
    monkeypatch.setattr(jobs, "_release_project_lock", _release)
    monkeypatch.setattr(jobs, "SeenFactIndex", _BrokenSeenIndex)
    bus = _Bus()
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(run_id),
        "C:/source",
        {"scope": "full"},
    )

    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        assert run.status == "failed"
        assert run.started_at is not None
        assert run.completed_at is not None
        assert run.error_log == "temporary index unavailable"
        assert run.stats["seen_index"]["storage"] == "not_initialized"
    assert released == [(project_id, run_id)]
    assert any(event.get("event") == "run_failed" for event in bus.events)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["failed", "completed"])
async def test_worker_stops_if_another_transition_already_terminalized_run(
    monkeypatch, terminal_status
):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    _project_id, run_id = await _seed_run(session_factory)
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.status = terminal_status
        await session.commit()

    with pytest.raises(RuntimeError, match=f"analysis_run_not_active:{terminal_status}"):
        await jobs._ensure_run_active(run_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_preserves_cooperative_cancel_signal(monkeypatch):
    from app.orchestrator import jobs

    engine, session_factory = await _database(monkeypatch)
    _project_id, run_id = await _seed_run(session_factory)
    async with session_factory() as session:
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.status = "cancelled"
        await session.commit()

    with pytest.raises(jobs.AnalysisCancelled, match="analysis_cancelled"):
        await jobs._ensure_run_active(run_id)
    await engine.dispose()


@pytest.mark.asyncio
async def test_old_queued_rows_are_failed_only_when_arq_job_is_gone(monkeypatch):
    import arq.jobs as arq_jobs

    from app.orchestrator.cron_jobs import _find_expired_queued_runs

    missing_id = uuid.uuid4()
    queued_id = uuid.uuid4()
    complete_id = uuid.uuid4()
    not_found_id = uuid.uuid4()
    redis_error_id = uuid.uuid4()
    rows = [
        (missing_id, None),
        (queued_id, {"job_id": "queued"}),
        (complete_id, {"job_id": "complete"}),
        (not_found_id, {"job_id": "not-found"}),
        (redis_error_id, {"job_id": "redis-error"}),
    ]

    class _Result:
        def all(self):
            return rows

    class _Session:
        async def execute(self, _statement):
            return _Result()

    class _Job:
        def __init__(self, job_id, _redis):
            self.job_id = job_id

        async def status(self):
            if self.job_id == "redis-error":
                raise ConnectionError("redis unavailable")
            return {
                "queued": arq_jobs.JobStatus.queued,
                "complete": arq_jobs.JobStatus.complete,
                "not-found": arq_jobs.JobStatus.not_found,
            }[self.job_id]

    monkeypatch.setattr(arq_jobs, "Job", _Job)
    expired = await _find_expired_queued_runs(_Session(), object())
    assert expired == {missing_id, complete_id, not_found_id}
