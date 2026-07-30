"""Hard process-kill fault injection for the source publication contract.

Unlike the in-process crash coverage in ``test_graph_publication_phase_a/b``
(raised exceptions, task cancellation, monkeypatched pauses), these tests run
the genuine staging → seal → promote path in a **real OS process**
(``publication_kill_child.py``) that dies with ``os._exit`` — no rollback, no
connection close — at chosen kill points, then assert the durable state a
fresh process observes:

- killed before promotion (after staging / after seal): the head is still
  fail-closed ``needs_rebuild`` at generation 0, ``nodes``/``edges`` are
  empty, staged rows are intact, and the run is still ``running`` (the stale
  reaper's contract input).
- killed **inside the open promotion transaction** (after the head CAS and
  node promotion were issued): recovery shows none of it, and a retry
  promotion from a fresh process publishes successfully.
- killed right after promotion committed: the head, current rows, receipt,
  and ``read_graph_stamp`` all survive the death of their writer.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "publication-hard-kill-test")

import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
from app.graph_publication import (  # noqa: E402
    promote_staged_graph,
    read_graph_stamp,
)
from app.models.graph import (  # noqa: E402
    AnalysisRun,
    Edge,
    GraphEdgeStage,
    GraphHead,
    GraphNodeStage,
    Node,
)
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402
from publication_kill_child import (  # noqa: E402
    EXIT_AFTER_PUBLISH,
    EXIT_AFTER_SEAL,
    EXIT_AFTER_STAGE,
    EXIT_MID_PROMOTE,
)

install_polyglot()

_CHILD = pathlib.Path(__file__).with_name("publication_kill_child.py")


def _run_child(
    db_path: str, scenario: str, project_id: uuid.UUID, run_id: uuid.UUID
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CHILD), db_path, scenario, project_id.hex, run_id.hex],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _assert_reached_kill_point(
    proc: subprocess.CompletedProcess, expected_exit: int
) -> None:
    assert proc.returncode == expected_exit, (
        f"child exited {proc.returncode}, expected {expected_exit}\n"
        f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
    )


def _engine(db_path: str):
    return create_async_engine(f"sqlite+aiosqlite:///{db_path}")


async def _count(session, model) -> int:
    return (
        await session.execute(select(func.count()).select_from(model))
    ).scalar_one()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario,expected_exit,sealed",
    [
        ("stage", EXIT_AFTER_STAGE, False),
        ("seal", EXIT_AFTER_SEAL, True),
        ("mid_promote", EXIT_MID_PROMOTE, True),
    ],
)
async def test_hard_kill_before_commit_leaves_graph_untouched(
    tmp_path, scenario, expected_exit, sealed
):
    db_path = (tmp_path / f"{scenario}.db").as_posix()
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    _assert_reached_kill_point(
        _run_child(db_path, scenario, project_id, run_id), expected_exit
    )

    engine = _engine(db_path)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            integrity = (
                await session.execute(text("PRAGMA integrity_check"))
            ).scalar_one()
            assert integrity == "ok"

            head = await session.get(GraphHead, project_id)
            assert head is not None
            assert head.state == "needs_rebuild"
            assert head.generation == 0
            assert head.current_run_id is None
            assert head.published_at is None

            assert await _count(session, Node) == 0
            assert await _count(session, Edge) == 0
            assert await _count(session, GraphNodeStage) == 2
            assert await _count(session, GraphEdgeStage) == 1

            run = await session.get(AnalysisRun, run_id)
            assert run is not None
            assert run.status == "running"
            assert run.graph_base_generation == 0
            assert (run.graph_coverage_sealed_at is not None) is sealed
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_kill_mid_promotion_then_retry_publishes(tmp_path):
    db_path = (tmp_path / "mid_promote_retry.db").as_posix()
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    _assert_reached_kill_point(
        _run_child(db_path, "mid_promote", project_id, run_id), EXIT_MID_PROMOTE
    )

    engine = _engine(db_path)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        # Staged rows and the sealed run survived the crash; the aborted
        # promotion left no trace, so a fresh process simply retries.
        async with Session() as session:
            await promote_staged_graph(
                session,
                project_id=project_id,
                run_id=run_id,
                allow_full_rebuild=True,
            )
        async with Session() as session:
            stamp = await read_graph_stamp(session, project_id=project_id)
            assert stamp.generation == 1
            assert stamp.current_run_id == run_id
            assert await _count(session, Node) == 2
            assert await _count(session, Edge) == 1
            assert await _count(session, GraphNodeStage) == 0
            assert await _count(session, GraphEdgeStage) == 0
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_hard_kill_after_publication_preserves_receipt_and_graph(tmp_path):
    db_path = (tmp_path / "published.db").as_posix()
    project_id, run_id = uuid.uuid4(), uuid.uuid4()
    _assert_reached_kill_point(
        _run_child(db_path, "published", project_id, run_id), EXIT_AFTER_PUBLISH
    )

    engine = _engine(db_path)
    try:
        Session = async_sessionmaker(engine, expire_on_commit=False)
        async with Session() as session:
            integrity = (
                await session.execute(text("PRAGMA integrity_check"))
            ).scalar_one()
            assert integrity == "ok"

            head = await session.get(GraphHead, project_id)
            assert head is not None
            assert head.state == "ready"
            assert head.generation == 1
            assert head.current_run_id == run_id
            assert head.published_at is not None

            assert await _count(session, Node) == 2
            assert await _count(session, Edge) == 1
            assert await _count(session, GraphNodeStage) == 0
            assert await _count(session, GraphEdgeStage) == 0

            run = await session.get(AnalysisRun, run_id)
            assert run is not None
            assert run.status == "published"
            assert run.completed_at is None
            assert isinstance((run.stats or {}).get("graph_publication"), dict)

            # read_graph_stamp validates the persisted receipt — a validated
            # stamp is the strongest single durability assertion available.
            stamp = await read_graph_stamp(session, project_id=project_id)
            assert stamp.generation == 1
            assert stamp.current_run_id == run_id
    finally:
        await engine.dispose()
