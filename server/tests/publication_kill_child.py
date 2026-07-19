"""Real-subprocess child for the publication hard-kill fault-injection test.

Invoked by ``test_publication_hard_kill.py`` as ``python publication_kill_child.py
<db_path> <scenario> <project_hex> <run_hex>``.  Runs the genuine staging →
seal → promote path against a file-backed WAL SQLite database and dies with
``os._exit`` (no rollback, no connection close, no atexit) at the scenario's
kill point.  Exit codes tell the parent the kill point was actually reached.

Not a pytest file — the name deliberately has no ``test_`` prefix.
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "publication-kill-child")

import asyncio
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

EXIT_AFTER_STAGE = 21
EXIT_AFTER_SEAL = 22
EXIT_MID_PROMOTE = 23
EXIT_AFTER_PUBLISH = 24


async def _main(
    db_path: str, scenario: str, project_id: uuid.UUID, run_id: uuid.UUID
) -> None:
    from app.models import (  # noqa: F401 — register every mapper for create_all
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
    import app.graph_publication as graph_publication
    from app.models.base import Base
    from app.models.graph import AnalysisRun
    from app.models.projects import Project
    from app.testing.sqlite_polyglot import install_polyglot

    install_polyglot()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(
            Project(
                id=project_id,
                name="hard-kill",
                gitlab_project_id=91001,
                gitlab_url="https://example.invalid/hard-kill",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="running",
                triggered_by="test:hard-kill",
                git_sha="c" * 40,
                scope="full",
                started_at=datetime.now(tz=timezone.utc),
            )
        )
        await session.flush()
        await graph_publication.bootstrap_graph_head(session, project_id=project_id)
        await graph_publication.capture_graph_base_generation(
            session, project_id=project_id, run_id=run_id
        )
        await session.commit()

    async with session_factory() as session:
        for node_id in ("py:alpha", "py:beta"):
            assert await graph_publication.stage_node(
                session,
                project_id=project_id,
                run_id=run_id,
                node_id=node_id,
                kind="Symbol",
                data={"id": node_id, "name": node_id.split(":", 1)[1]},
                certainty="asserted",
                source_name="ggoss-py",
            )
        assert await graph_publication.stage_edge(
            session,
            project_id=project_id,
            run_id=run_id,
            source_id="py:alpha",
            target_id="py:beta",
            kind="CALLS",
            data={},
            certainty="asserted",
            source_name="ggoss-py",
        )
        await session.commit()

    if scenario == "stage":
        os._exit(EXIT_AFTER_STAGE)

    async with session_factory() as session:
        await graph_publication.seal_graph_coverage(
            session,
            project_id=project_id,
            run_id=run_id,
            authoritative_sources={"ggoss-py"},
        )
        await session.commit()

    if scenario == "seal":
        os._exit(EXIT_AFTER_SEAL)

    if scenario == "mid_promote":

        async def _die_inside_promotion(*args, **kwargs):  # noqa: ANN002, ANN003
            # Hard death inside the still-open publication transaction: the
            # head CAS UPDATE and node promotion were issued but never
            # committed, so recovery must show none of it.
            os._exit(EXIT_MID_PROMOTE)

        graph_publication._promote_edges = _die_inside_promotion

    async with session_factory() as session:
        await graph_publication.promote_staged_graph(
            session,
            project_id=project_id,
            run_id=run_id,
            allow_full_rebuild=True,
        )

    if scenario == "published":
        os._exit(EXIT_AFTER_PUBLISH)

    raise SystemExit(f"unknown scenario {scenario!r}")


def main() -> None:
    db_path, scenario, project_hex, run_hex = sys.argv[1:5]
    asyncio.run(
        _main(db_path, scenario, uuid.UUID(project_hex), uuid.UUID(run_hex))
    )


if __name__ == "__main__":
    main()
