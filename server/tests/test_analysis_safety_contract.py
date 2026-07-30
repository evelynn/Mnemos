from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "analysis-safety-contract-test-key")

from app.api.analysis import AnalysisTriggerRequest  # noqa: E402
from app.models.graph import Edge, Node  # noqa: E402
from app.orchestrator.jobs import (  # noqa: E402
    WorkerSettings,
    _close_missing_deterministic_facts,
    _record_payload,
)
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


def test_analysis_defaults_to_zero_token_indexing():
    request = AnalysisTriggerRequest(source_path="C:/repo")
    assert request.summarize is False
    assert request.agent_extract_limit == 0
    assert request.scope == "incremental"


def test_continuation_without_narration_is_rejected_as_no_work():
    with pytest.raises(ValidationError, match="continuation_requires_summarize"):
        AnalysisTriggerRequest(source_path="C:/repo", scope="continuation")
    request = AnalysisTriggerRequest(
        source_path="C:/repo", scope="continuation", summarize=True
    )
    assert request.scope == "continuation"


def test_worker_does_not_retry_or_parallelize_graph_mutations():
    assert WorkerSettings.max_jobs == 1
    assert WorkerSettings.max_tries == 1
    assert WorkerSettings.retry_jobs is False
    assert WorkerSettings.allow_abort_jobs is True
    assert WorkerSettings.job_timeout == 8 * 60 * 60


def test_runtime_image_pins_a_node20_capable_distribution_and_asserts_it():
    root = Path(__file__).resolve().parents[2]
    dockerfile = (root / "server" / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (root / ".dockerignore").read_text(encoding="utf-8").splitlines()

    assert dockerfile.startswith("FROM python:3.12-slim-trixie\n")
    assert "process.versions.node" in dockerfile
    assert "major < 20" in dockerfile
    assert "COPY docs/ /docs/" in dockerfile
    assert "python -m pip uninstall --yes pip" in dockerfile
    assert "docs" not in dockerignore


@pytest.mark.asyncio
async def test_successful_refresh_closes_deleted_facts_but_not_runtime_facts():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Node.__table__.create)
        await connection.run_sync(Edge.__table__.create)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    project_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    async with Session() as session:
        session.add_all(
            [
                Node(
                    id="sym:keep",
                    project_id=project_id,
                    kind="Symbol",
                    data={"file": "keep.py"},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=now,
                ),
                Node(
                    id="sym:deleted",
                    project_id=project_id,
                    kind="Symbol",
                    data={"file": "deleted.py"},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=now,
                ),
                Node(
                    id="runtime:observation",
                    project_id=project_id,
                    kind="Symbol",
                    data={},
                    certainty="verified",
                    created_by=["otlp-runtime"],
                    valid_from=now,
                ),
                Edge(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    source_id="sym:keep",
                    target_id="sym:deleted",
                    kind="CALLS",
                    data={},
                    certainty="asserted",
                    created_by=["ggoss-py"],
                    valid_from=now,
                ),
            ]
        )
        await session.commit()

        closed = await _close_missing_deterministic_facts(
            session,
            project_id=project_id,
            source_names={"ggoss-py"},
            seen_nodes={"sym:keep"},
            seen_edges=set(),
        )
        await session.commit()

        current_nodes = set(
            (
                await session.execute(
                    select(Node.id).where(
                        Node.project_id == project_id, Node.valid_to.is_(None)
                    )
                )
            ).scalars()
        )
        current_edges = (
            await session.execute(
                select(Edge.id).where(
                    Edge.project_id == project_id, Edge.valid_to.is_(None)
                )
            )
        ).scalars().all()

    await engine.dispose()
    assert closed == {"nodes": 1, "edges": 1}
    assert current_nodes == {"sym:keep", "runtime:observation"}
    assert current_edges == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {
            "record_type": "symbol",
            "data": {"id": "missing-producer", "certainty": "asserted"},
        },
        {
            "record_type": "symbol",
            "source_name": "ggoss-py",
            "data": {"id": "x" * 4097, "certainty": "asserted"},
        },
        {
            "record_type": "symbol",
            "source_name": "ggoss-py",
            "data": {"id": "sym:bad", "certainty": "model-says-verified"},
        },
        {
            "record_type": "edge",
            "source_name": "ggoss-py",
            "data": {
                "source_id": "sym:a",
                "target_id": "sym:b",
                "kind": "CALLS",
                "metadata": ["not", "an", "object"],
            },
        },
    ],
)
async def test_graph_payload_boundary_rejects_unbounded_or_unproven_fields(
    payload,
):
    totals = {"symbols": 0, "edges": 0, "contracts": 0, "data_entities": 0}
    accepted = await _record_payload(
        object(),
        project_id=uuid.uuid4(),
        payload=payload,
        accept_kinds={payload["record_type"]},
        totals=totals,
    )

    assert accepted is False
    assert sum(totals.values()) == 0
