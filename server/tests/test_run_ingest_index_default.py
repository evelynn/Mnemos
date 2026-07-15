from __future__ import annotations

import json
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "run-ingest-index-default-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.config import get_settings  # noqa: E402
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
from app.models.findings import LLMCall, Summary  # noqa: E402
from app.models.graph import AnalysisRun, Edge, Node  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


class RecordingBus:
    def __init__(self) -> None:
        self.events = []

    async def publish(self, run_id, event):
        self.events.append((run_id, event))


@pytest.mark.asyncio
async def test_full_index_is_zero_llm_and_same_content_incremental_is_noop(
    tmp_path, monkeypatch
):
    from app.orchestrator import jobs, stages

    monkeypatch.setenv("MNEMOS_INREPO_ANALYZERS", "1")
    # run_ingest deliberately fails closed when its distributed project lock
    # cannot reach Redis.  This isolated SQLite contract test exercises the
    # supported in-process local-mode Redis replacement instead.
    monkeypatch.setenv("MNEMOS_LOCAL_MODE", "1")
    (tmp_path / "service.py").write_text(
        "def callee():\n    return 1\n\ndef caller():\n    return callee()\n",
        encoding="utf-8",
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(jobs, "SessionLocal", Session)
    monkeypatch.setattr(stages, "SessionLocal", Session)

    async def no_audit(**_kwargs):
        return None

    monkeypatch.setattr(jobs, "audit_record", no_audit)
    project_id = uuid.uuid4()
    first_run_id = uuid.uuid4()
    settings = get_settings()
    monkeypatch.setattr(settings, "source_allowed_root", str(tmp_path))
    monkeypatch.setattr(
        settings,
        "source_project_roots",
        json.dumps({str(project_id): "."}),
    )
    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="index-default",
                gitlab_project_id=991001,
                gitlab_url="https://example.invalid/index-default",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=first_run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test",
                git_sha="HEAD",
                scope="full",
            )
        )
        await session.commit()

    bus = RecordingBus()
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(first_run_id),
        str(tmp_path),
        {"scope": "full"},
    )

    async with Session() as session:
        first = await session.get(AnalysisRun, first_run_id)
        node_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Node).where(
                        Node.project_id == project_id, Node.valid_to.is_(None)
                    )
                )
            ).scalar_one()
        )
        edge_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Edge).where(
                        Edge.project_id == project_id, Edge.valid_to.is_(None)
                    )
                )
            ).scalar_one()
        )
        assert first is not None and first.status == "completed"
        assert first.stats["ai_narration"]["enabled"] is False
        assert first.stats["ai_narration"]["initial_llm_tokens_when_disabled"] == 0
        assert node_count >= 2
        assert edge_count >= 1
        assert int((await session.execute(select(func.count()).select_from(Summary))).scalar_one()) == 0
        assert int((await session.execute(select(func.count()).select_from(LLMCall))).scalar_one()) == 0
        first_history = int(
            (
                await session.execute(
                    select(func.count()).select_from(Node).where(Node.valid_to.is_not(None))
                )
            ).scalar_one()
        )

        second_run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=second_run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test",
                git_sha="HEAD",
                scope="incremental",
            )
        )
        await session.commit()

    real_analyzer_stage = jobs._run_analyzer_stage

    async def analyzer_must_not_run(*_args, **_kwargs):
        raise AssertionError("same-content incremental spawned an analyzer")

    monkeypatch.setattr(jobs, "_run_analyzer_stage", analyzer_must_not_run)
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(second_run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        second = await session.get(AnalysisRun, second_run_id)
        history_after = int(
            (
                await session.execute(
                    select(func.count()).select_from(Node).where(Node.valid_to.is_not(None))
                )
            ).scalar_one()
        )
        assert second is not None and second.status == "completed"
        assert second.stats["refresh"]["content_noop"] is True
        assert second.stats["refresh"]["changed_analyzers"] == []
        assert history_after == first_history

        third_run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=third_run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test",
                    git_sha="HEAD",
                scope="incremental",
            )
        )
        await session.commit()

    # Removing the callee changes only Python.  The refreshed analyzer output
    # is authoritative, so the old symbol and CALLS edge must leave current
    # state rather than remaining as stale source references.
    (tmp_path / "service.py").write_text(
        "def caller():\n    return 1\n", encoding="utf-8"
    )
    monkeypatch.setattr(jobs, "_run_analyzer_stage", real_analyzer_stage)
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(third_run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        third = await session.get(AnalysisRun, third_run_id)
        current_nodes = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id, Node.valid_to.is_(None)
                )
            )
        ).scalars().all()
        current_names = {(node.data or {}).get("name") for node in current_nodes}
        current_edge_count = int(
            (
                await session.execute(
                    select(func.count()).select_from(Edge).where(
                        Edge.project_id == project_id, Edge.valid_to.is_(None)
                    )
                )
            ).scalar_one()
        )
        assert third is not None and third.status == "completed"
        assert third.stats["refresh"]["changed_analyzers"] == ["ggoss-py"]
        assert "caller" in current_names
        assert "callee" not in current_names
        assert current_edge_count == 0
        assert third.stats["stale_nodes_closed"] >= 1
        assert third.stats["stale_edges_closed"] >= 1

        fourth_run_id = uuid.uuid4()
        session.add(
            AnalysisRun(
                id=fourth_run_id,
                project_id=project_id,
                status="queued",
                triggered_by="test",
                git_sha="HEAD",
                scope="incremental",
            )
        )
        await session.commit()

    # A producer only has deletion authority after *every* required verb is
    # authoritative.  Simulate a calls-stage crash after the source changed:
    # the symbols stage can publish partial additions for now, but omission
    # from that incomplete refresh must never close the last good symbol.
    (tmp_path / "service.py").write_text(
        "def replacement():\n    return 2\n", encoding="utf-8"
    )

    async def analyzer_with_partial_calls(
        bus,
        pid,
        rid,
        language,
        verb,
        path,
        position,
        totals,
        **kwargs,
    ):
        if verb == "calls":
            return jobs.AnalyzerStageOutcome(
                producer="ggoss-py",
                verb=verb,
                authoritative=False,
                errors=("analyzer_process_failed",),
            )
        return await real_analyzer_stage(
            bus,
            pid,
            rid,
            language,
            verb,
            path,
            position,
            totals,
            **kwargs,
        )

    monkeypatch.setattr(jobs, "_run_analyzer_stage", analyzer_with_partial_calls)
    await jobs.run_ingest(
        {"progress": bus},
        str(project_id),
        str(fourth_run_id),
        str(tmp_path),
        {"scope": "incremental"},
    )

    async with Session() as session:
        fourth = await session.get(AnalysisRun, fourth_run_id)
        current_nodes = (
            await session.execute(
                select(Node).where(
                    Node.project_id == project_id, Node.valid_to.is_(None)
                )
            )
        ).scalars().all()
        current_names = {(node.data or {}).get("name") for node in current_nodes}
        assert fourth is not None and fourth.status == "failed"
        assert "no_usable_source_analyzer" in (fourth.error_log or "")
        assert "caller" in current_names, (
            "partial producer output must not delete last-known-good facts"
        )

    await engine.dispose()
