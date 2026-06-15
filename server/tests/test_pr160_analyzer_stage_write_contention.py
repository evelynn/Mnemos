"""PR-160 — the deterministic analyzer stage must commit graph rows before
it reports progress, or docker-free SQLite analysis dies with
``database is locked``.

Background. ``StageTracker.increment()`` flushes progress every 25 items by
opening its *own* ``SessionLocal`` to ``UPDATE analysis_stages`` (see
``app/orchestrator/stages.py``). SQLite allows a single writer, so if the
analyzer stage is still holding an *uncommitted* write transaction (the
graph-row INSERTs) when that flush fires, the second writer collides —
the PR-141 ``database is locked`` failure.

PR-141 fixed this for the *agent-extraction* stage (commit-before-increment)
but left the *deterministic* analyzer stage alone, because at the time the
in-repo analyzers extracted nothing in local mode so the contention never
fired. PR-153/PR-144 changed that: the docker-free in-repo ``ggoss-py`` now
emits real symbols, so any non-trivial Python project (>25 symbols) re-hit
the latent bug. This test pins the fix.

It is deterministic — it does not race on the lock. It spies on every
``StageTracker.increment`` call and reads the committed node count from an
*independent* connection: with commit-before-increment the rows are already
visible (>0); the old in-loop increment would see 0 (and, on a busy_timeout=0
engine like the one here, would raise OperationalError outright).
"""

from __future__ import annotations

import os
import uuid

import pytest

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr160")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")


class _StubBus:
    """A ProgressBus stand-in — StageTracker only ever calls publish()."""

    async def publish(self, *_a, **_k) -> None:
        return None


class _FakeRunner:
    """Streams ``n`` symbol records without spawning a real analyzer."""

    def __init__(self, n: int) -> None:
        self._n = n

    async def run(self, verb, path, **_kw):  # noqa: ANN001 - matches AnalyzerRunner.run
        from app.analyzers.runner import RunRecord

        for i in range(self._n):
            yield RunRecord(
                stream="stdout",
                payload={
                    "record_type": "symbol",
                    "source_name": "ggoss-py",
                    "data": {"id": f"py:m.py::f{i}", "name": f"f{i}", "language": "python"},
                },
            )


@pytest.mark.asyncio
async def test_analyzer_stage_commits_before_progress_flush(tmp_path, monkeypatch):
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    # Register every model so create_all resolves cross-table FKs.
    from app.models import (  # noqa: F401
        audit, auth, comments, findings, graph, onboarding,
        organization, plans, projects, runtime, samples, stages,
    )
    from app.models.base import Base
    from app.models.graph import Node
    import app.orchestrator.jobs as jobs
    import app.orchestrator.stages as stages_mod

    # A real on-disk SQLite file so the analyzer session and the StageTracker
    # flush session are genuinely separate connections (the contention setup);
    # :memory: would hand each connection its own private database.
    db = tmp_path / "stage.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # The stage + job code resolve SessionLocal from their own module globals.
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    monkeypatch.setattr(stages_mod, "SessionLocal", Session)

    n_records = 120
    monkeypatch.setattr(jobs, "runner_for", lambda _lang: _FakeRunner(n_records))

    pid = uuid.uuid4()

    # Spy: at the moment progress is reported, how many nodes are committed and
    # visible from an independent connection? Must always be > 0 — i.e. the
    # analyzer session committed before increment() ran.
    visible_at_increment: list[int] = []
    real_increment = stages_mod.StageTracker.increment

    async def _spy_increment(self, amount: int = 1) -> None:
        async with Session() as probe:
            count = (
                await probe.execute(
                    select(func.count()).select_from(Node).where(Node.project_id == pid)
                )
            ).scalar_one()
        visible_at_increment.append(count)
        await real_increment(self, amount)

    monkeypatch.setattr(stages_mod.StageTracker, "increment", _spy_increment)

    totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0, "errors": 0}
    await jobs._run_analyzer_stage(
        _StubBus(), pid, uuid.uuid4(), "python", "symbols", str(tmp_path), 1, totals
    )

    # Every streamed symbol persisted and counted.
    async with Session() as s:
        nodes = (await s.execute(select(Node).where(Node.project_id == pid))).scalars().all()
    assert len(nodes) == n_records
    assert totals["symbols"] == n_records

    # The invariant under test: progress was reported at least once, and every
    # report saw its batch already committed (never 0). Under the pre-PR-160
    # ordering this list would start with 0 — or the run would have raised
    # "database is locked" before reaching here.
    assert visible_at_increment, "expected at least one progress increment for 120 rows"
    assert all(c > 0 for c in visible_at_increment), (
        f"graph rows were not committed before progress flush: {visible_at_increment}"
    )
    assert visible_at_increment[0] >= 50, (
        f"first progress batch should report already-committed rows: {visible_at_increment}"
    )
