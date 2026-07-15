from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "summary-http-generation-pinning-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.api.analysis import (  # noqa: E402
    list_summaries,
    project_llm_cost,
    project_llm_fallback_breakdown,
)
from app.models.findings import LLMCall, Summary  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


@pytest.mark.asyncio
async def test_http_summary_views_require_exact_source_and_overlay_revision():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            Organization.__table__,
            Project.__table__,
            AnalysisRun.__table__,
            GraphHead.__table__,
            Summary.__table__,
            LLMCall.__table__,
        ):
            await connection.run_sync(table.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    common = {
        "project_id": project_id,
        "level": 1,
        "analysis_run_id": run_id,
        "summary": "Revision-pinned summary.",
        "detailed": "Revision-pinned summary.",
        "claims": [],
        "open_questions": [],
        "model_used": "test-provider",
        "generated_at": now,
        "validated_at": now,
    }

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="summary HTTP revision",
                gitlab_project_id=3,
                gitlab_url="https://example.invalid/summary-http",
                default_branch="main",
                languages=["python"],
            )
        )
        session.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha="a" * 40,
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(generation=7, published_at=now),
            )
        )
        await session.commit()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=7,
                overlay_generation=4,
                state="ready",
                published_at=now,
            )
        )
        session.add_all(
            [
                Summary(
                    id=uuid.uuid4(),
                    target_id="sym:current",
                    validated_graph_generation=7,
                    validated_overlay_generation=4,
                    fallback_reason=None,
                    **common,
                ),
                Summary(
                    id=uuid.uuid4(),
                    target_id="sym:stale-source",
                    validated_graph_generation=6,
                    validated_overlay_generation=4,
                    fallback_reason="stale_source",
                    **common,
                ),
                Summary(
                    id=uuid.uuid4(),
                    target_id="sym:stale-overlay",
                    validated_graph_generation=7,
                    validated_overlay_generation=3,
                    fallback_reason="stale_overlay",
                    **common,
                ),
            ]
        )
        await session.commit()

        visible = await list_summaries(
            project_id=project_id,
            _=None,  # type: ignore[arg-type]
            db=session,
        )
        cost = await project_llm_cost(
            project_id=project_id,
            _=None,  # type: ignore[arg-type]
            db=session,
        )
        fallback = await project_llm_fallback_breakdown(
            project_id=project_id,
            _=None,  # type: ignore[arg-type]
            db=session,
        )
        assert [row["target_id"] for row in visible] == ["sym:current"]
        assert cost["summary_count"] == 1
        assert fallback == {
            "ok": 1,
            "fallbacks": {},
            "total": 1,
            "fallback_rate": 0.0,
            "window_days": None,
        }

        await session.execute(
            update(GraphHead)
            .where(GraphHead.project_id == project_id)
            .values(overlay_generation=5)
        )
        await session.commit()
        assert not await list_summaries(
            project_id=project_id,
            _=None,  # type: ignore[arg-type]
            db=session,
        )
        assert (
            await project_llm_cost(
                project_id=project_id,
                _=None,  # type: ignore[arg-type]
                db=session,
            )
        )["summary_count"] == 0
        assert (
            await project_llm_fallback_breakdown(
                project_id=project_id,
                _=None,  # type: ignore[arg-type]
                db=session,
            )
        )["total"] == 0

    await engine.dispose()
