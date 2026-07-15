"""One canonical publication receipt contract across readers and writers."""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.graph_overlays import (
    GraphOverlayUnavailable,
    record_human_confirmation,
)
from app.graph_publication import (
    GraphPublicationInvariantError,
    lock_validated_graph_head,
    read_graph_stamp,
    validate_graph_publication_receipt,
)
from app.merge.findings import FindingRebuildUnavailable, run_all
from app.models import all_models as _all_models  # noqa: F401
from app.models.base import Base
from app.models.graph import AnalysisRun, GraphHead
from app.models.projects import Project
from app.summary_freshness import lock_ready_summary_generation
from app.testing.graph_publication import published_run_fields
from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()


_PUBLISHED_AT = datetime(2026, 7, 15, 6, 0, tzinfo=timezone.utc)


def _valid_run(*, generation: int = 3) -> SimpleNamespace:
    fields = published_run_fields(
        generation=generation,
        published_at=_PUBLISHED_AT,
        authoritative_sources=["ggoss-py", "ggoss-ts"],
        coverage_sealed_at=_PUBLISHED_AT - timedelta(seconds=1),
    )
    return SimpleNamespace(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status="completed",
        scope="full",
        started_at=_PUBLISHED_AT - timedelta(seconds=2),
        completed_at=_PUBLISHED_AT,
        **fields,
    )


def _valid_head(run: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        project_id=run.project_id,
        current_run_id=run.id,
        generation=run.stats["graph_publication"]["generation"],
        overlay_generation=0,
        state="ready",
        published_at=_PUBLISHED_AT,
    )


def test_canonical_receipt_round_trips_exact_v1_payload() -> None:
    run = _valid_run()

    publication = validate_graph_publication_receipt(
        run,
        head=_valid_head(run),
    )

    assert publication.generation == 3
    assert publication.base_generation == 2
    assert publication.published_at == _PUBLISHED_AT
    assert publication.to_payload() == run.stats["graph_publication"]


def _set_receipt(run: SimpleNamespace, **changes: Any) -> None:
    run.stats = copy.deepcopy(run.stats)
    run.stats["graph_publication"].update(changes)


def _bool_count(run: SimpleNamespace) -> None:
    _set_receipt(run)
    run.stats["graph_publication"]["counts"]["nodes_inserted"] = True


def _missing_count(run: SimpleNamespace) -> None:
    _set_receipt(run)
    del run.stats["graph_publication"]["counts"]["nodes_inserted"]


def _extra_count(run: SimpleNamespace) -> None:
    _set_receipt(run)
    run.stats["graph_publication"]["counts"]["future_count"] = 0


def _duplicate_sources(run: SimpleNamespace) -> None:
    run.graph_authoritative_sources = ["ggoss-py", "ggoss-py"]


def _extra_receipt_key(run: SimpleNamespace) -> None:
    _set_receipt(run, synthetic_seed=True)


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda run: _set_receipt(run, generation=True), "persisted base"),
        (lambda run: _set_receipt(run, base_generation=True), "persisted base"),
        (_bool_count, "real integers"),
        (_missing_count, "invalid shape"),
        (_extra_count, "invalid shape"),
        (
            lambda run: _set_receipt(
                run,
                published_at="2026-07-15T06:00:00",
            ),
            "timezone-aware",
        ),
        (
            lambda run: _set_receipt(
                run,
                published_at="2026-07-15T15:00:00+09:00",
            ),
            "must be UTC",
        ),
        (_duplicate_sources, "not canonical"),
        (_extra_receipt_key, "invalid shape"),
    ],
)
def test_canonical_receipt_rejects_contract_drift(
    mutation: Callable[[SimpleNamespace], None],
    reason: str,
) -> None:
    run = _valid_run()
    mutation(run)

    with pytest.raises(GraphPublicationInvariantError, match=reason):
        validate_graph_publication_receipt(run)


@pytest.mark.asyncio
async def test_receiptless_ready_head_is_rejected_by_read_and_write_boundaries() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="receiptless-ready-head",
                gitlab_project_id=1,
                gitlab_url="https://example.invalid/receiptless",
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
                started_at=_PUBLISHED_AT - timedelta(seconds=1),
                completed_at=_PUBLISHED_AT,
                stats={},
            )
        )
        await session.flush()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=_PUBLISHED_AT,
            )
        )
        await session.commit()

        with pytest.raises(
            GraphPublicationInvariantError,
            match="receipt is missing",
        ):
            await read_graph_stamp(session, project_id=project_id)
        await session.rollback()

        with pytest.raises(GraphPublicationInvariantError, match="receipt is missing"):
            await lock_ready_summary_generation(session, project_id=project_id)
        await session.rollback()

        with pytest.raises(FindingRebuildUnavailable):
            await run_all(session, project_id)
        await session.rollback()

        with pytest.raises(GraphOverlayUnavailable):
            await record_human_confirmation(
                session,
                project_id=project_id,
                target_kind="node",
                target_id="sym:missing",
                action="confirm",
                actor="user:test",
                rationale="Receipt-less ready heads cannot authorize evidence.",
            )
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_stamp_and_lock_refresh_identity_mapped_head_and_receipt(tmp_path) -> None:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'identity-map.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    async with Session() as seed:
        seed.add(
            Project(
                id=project_id,
                name="identity-map-currentness",
                gitlab_project_id=2,
                gitlab_url="https://example.invalid/identity-map",
                default_branch="main",
                languages=["python"],
            )
        )
        seed.add(
            AnalysisRun(
                id=run_id,
                project_id=project_id,
                status="completed",
                triggered_by="test",
                git_sha="b" * 40,
                scope="full",
                started_at=_PUBLISHED_AT - timedelta(seconds=2),
                completed_at=_PUBLISHED_AT,
                **published_run_fields(
                    generation=1,
                    published_at=_PUBLISHED_AT,
                    coverage_sealed_at=_PUBLISHED_AT - timedelta(seconds=1),
                ),
            )
        )
        await seed.flush()
        seed.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=_PUBLISHED_AT,
            )
        )
        await seed.commit()

    async with Session() as reader, Session() as writer:
        # Retain both ORM identities across transaction boundaries.
        cached_head = await reader.get(GraphHead, project_id)
        cached_run = await reader.get(AnalysisRun, run_id)
        assert cached_head is not None and cached_head.overlay_generation == 0
        assert cached_run is not None and cached_run.stats
        await reader.commit()

        writer_head = await writer.get(GraphHead, project_id)
        assert writer_head is not None
        writer_head.overlay_generation = 1
        await writer.commit()

        stamp = await read_graph_stamp(reader, project_id=project_id)
        assert stamp.overlay_generation == 1
        assert cached_head.overlay_generation == 1
        await reader.rollback()

        writer_run = await writer.get(AnalysisRun, run_id)
        assert writer_run is not None
        writer_run.stats = {}
        await writer.commit()

        with pytest.raises(
            GraphPublicationInvariantError, match="receipt is missing"
        ):
            await lock_validated_graph_head(reader, project_id=project_id)
        assert cached_run.stats == {}
        await reader.rollback()

    await engine.dispose()
