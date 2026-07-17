from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "summary-generation-pinning-test-key")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

from app.artifacts.agent_context import (  # noqa: E402
    _current_summaries,
    _summary_quality,
)
from app.extractor.agent_flow import (  # noqa: E402
    FLOW_RESULT_CONTRACT,
    _normalise,
    build_flow_summary_content,
)
from app.mcp.queries import get_module_summary, list_flows  # noqa: E402
from app.models.findings import LLMCall, Summary  # noqa: E402
from app.models.graph import AnalysisRun, Edge, GraphHead, Node  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.projects import Project  # noqa: E402
from app.testing.graph_publication import published_run_fields  # noqa: E402
from app.testing.sqlite_polyglot import install_polyglot  # noqa: E402

install_polyglot()


def _migration_0030_dedupe_sql() -> str:
    migration = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "0030_summary_graph_generation.py"
    ).read_text(encoding="utf-8")
    start = migration.index('        WITH ranked AS (')
    end = migration.index('        """', start)
    return migration[start:end].strip()


@pytest.mark.asyncio
async def test_0030_reconciles_legacy_unsuperseded_duplicates_without_deletion():
    """The migration leaves one deterministic cache predecessor per target."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    project_id = str(uuid.uuid4())
    oldest_id, middle_id, newest_id, other_id = (
        str(uuid.uuid4()) for _ in range(4)
    )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "CREATE TABLE summaries ("
                "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, "
                "target_id TEXT NOT NULL, level INTEGER NOT NULL, "
                "generated_at TEXT NOT NULL, superseded_by TEXT NULL)"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO summaries "
                "(id,project_id,target_id,level,generated_at,superseded_by) "
                "VALUES "
                "(:oldest,:project,'module:x',2,'2026-01-01T00:00:00Z',NULL),"
                "(:middle,:project,'module:x',2,'2026-02-01T00:00:00Z',NULL),"
                "(:newest,:project,'module:x',2,'2026-03-01T00:00:00Z',NULL),"
                "(:other,:project,'module:y',2,'2026-01-01T00:00:00Z',NULL)"
            ),
            {
                "oldest": oldest_id,
                "middle": middle_id,
                "newest": newest_id,
                "other": other_id,
                "project": project_id,
            },
        )
        await connection.execute(text(_migration_0030_dedupe_sql()))
        rows = (
            await connection.execute(
                text("SELECT id, superseded_by FROM summaries")
            )
        ).all()
    await engine.dispose()

    dispositions = dict(rows)
    assert dispositions[newest_id] is None
    assert dispositions[oldest_id] == newest_id
    assert dispositions[middle_id] == newest_id
    assert dispositions[other_id] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_0030_reconciles_real_legacy_duplicate_rows():
    """Execute the production backfill SQL against PostgreSQL UUID rows."""

    raw_url = os.getenv("DATABASE_URL", "")
    if not raw_url.startswith(("postgres://", "postgresql://", "postgresql+")):
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    if raw_url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + raw_url.removeprefix("postgres://")
    elif raw_url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + raw_url.removeprefix("postgresql://")
    elif raw_url.startswith("postgresql+asyncpg://"):
        url = raw_url
    else:
        _prefix, rest = raw_url.split("://", 1)
        url = "postgresql+asyncpg://" + rest

    engine = create_async_engine(url, poolclass=NullPool)
    project_id = uuid.uuid4()
    oldest_id, newest_id, other_id = (uuid.uuid4() for _ in range(3))
    try:
        async with engine.begin() as connection:
            # A temporary table shadows the migrated public table, keeping the
            # CI application schema and all other tests untouched.
            await connection.execute(
                text(
                    "CREATE TEMP TABLE summaries ("
                    "id uuid PRIMARY KEY, project_id uuid NOT NULL, "
                    "target_id text NOT NULL, level integer NOT NULL, "
                    "generated_at timestamptz NOT NULL, superseded_by uuid NULL)"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO summaries "
                    "(id,project_id,target_id,level,generated_at,superseded_by) "
                    "VALUES "
                    "(:oldest,:project,'module:x',2,'2026-01-01T00:00:00Z',NULL),"
                    "(:newest,:project,'module:x',2,'2026-03-01T00:00:00Z',NULL),"
                    "(:other,:project,'module:y',2,'2026-01-01T00:00:00Z',NULL)"
                ),
                {
                    "oldest": oldest_id,
                    "newest": newest_id,
                    "other": other_id,
                    "project": project_id,
                },
            )
            await connection.execute(text(_migration_0030_dedupe_sql()))
            rows = (
                await connection.execute(
                    text("SELECT id, superseded_by FROM summaries")
                )
            ).all()
    finally:
        await engine.dispose()

    dispositions = dict(rows)
    assert dispositions[newest_id] is None
    assert dispositions[oldest_id] == newest_id
    assert dispositions[other_id] is None


def _flow_sections(run_id: uuid.UUID) -> list[dict]:
    flow = _normalise(
        {
            "summary": "Create an order.",
            "detailed": "The handler inserts one order row.",
            "steps": [
                {
                    "order": 1,
                    "tier": "backend",
                    "component": "orders.handler",
                    "action": "insert the row",
                    "signal": {
                        "kind": "sql",
                        "name": "INSERT orders",
                        "fields": [],
                    },
                }
            ],
            "flags": [],
            "data_touched": [
                {"entity": "orders", "op": "insert", "columns": ["id"]}
            ],
            "open_questions": [],
        }
    )
    assert flow["contract"] == FLOW_RESULT_CONTRACT
    return build_flow_summary_content(
        flow,
        analysis_run_id=run_id,
        revision="a" * 40,
        source_scope={
            "provided_files": ["orders.py"],
            "files": [
                {
                    "label": "orders.py",
                    "tier": "backend",
                    "language": "python",
                    "provided_chars": 20,
                    "included_chars": 20,
                    "prompt_truncated": False,
                    "input_truncated": False,
                }
            ],
            "omitted_files": [],
            "included_source_chars": 20,
            "max_source_chars": 28_000,
            "truncated": False,
        },
    ).sections


async def _database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            Organization.__table__,
            Project.__table__,
            AnalysisRun.__table__,
            Node.__table__,
            Edge.__table__,
            GraphHead.__table__,
            LLMCall.__table__,
            Summary.__table__,
        ):
            await connection.run_sync(table.create)
    return engine, sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


@pytest.mark.asyncio
async def test_validation_marker_rejects_partial_revision_pairs():
    engine, Session = await _database()
    async with Session() as session:
        session.add(
            Summary(
                id=uuid.uuid4(),
                project_id=uuid.uuid4(),
                target_id="sym:partial-marker",
                level=1,
                validated_graph_generation=1,
                validated_overlay_generation=None,
                validated_at=datetime.now(tz=timezone.utc),
                summary="Must not persist.",
                model_used="test-provider",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_partial_unique_index_allows_history_but_only_one_validated_current():
    """Historical narratives coexist; validated current lineage is singular."""

    engine, Session = await _database()
    project_id = uuid.uuid4()
    target_id = "module:orders"
    now = datetime.now(tz=timezone.utc)
    current_id = uuid.uuid4()
    historical_ids = {uuid.uuid4(), uuid.uuid4()}

    def _summary(
        row_id: uuid.UUID,
        *,
        generation: int | None,
        overlay_generation: int | None,
    ) -> Summary:
        return Summary(
            id=row_id,
            project_id=project_id,
            target_id=target_id,
            level=3,
            validated_graph_generation=generation,
            validated_overlay_generation=overlay_generation,
            validated_at=now if generation is not None else None,
            summary=f"summary:{row_id}",
            model_used="test-provider",
        )

    async with Session() as session:
        session.add(
            _summary(current_id, generation=1, overlay_generation=0)
        )
        session.add_all(
            [
                _summary(row_id, generation=None, overlay_generation=None)
                for row_id in historical_ids
            ]
        )
        await session.commit()

        # The uniqueness scope is lineage-wide, not generation-specific: a
        # stale validated row must be retired before a replacement is added.
        duplicate_id = uuid.uuid4()
        session.add(
            _summary(duplicate_id, generation=2, overlay_generation=1)
        )
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

        replacement_id = uuid.uuid4()
        await session.execute(
            update(Summary)
            .where(Summary.id == current_id)
            .values(superseded_by=replacement_id)
        )
        session.add(
            _summary(replacement_id, generation=2, overlay_generation=1)
        )
        await session.commit()

        rows = (
            await session.execute(
                select(Summary).where(
                    Summary.project_id == project_id,
                    Summary.target_id == target_id,
                    Summary.level == 3,
                )
            )
        ).scalars().all()

    await engine.dispose()
    validated_current = [
        row
        for row in rows
        if row.validated_graph_generation is not None
        and row.superseded_by is None
    ]
    assert [row.id for row in validated_current] == [replacement_id]
    assert {
        row.id
        for row in rows
        if row.validated_graph_generation is None
        and row.superseded_by is None
    } == historical_ids
    assert next(row for row in rows if row.id == current_id).superseded_by == (
        replacement_id
    )


@pytest.mark.asyncio
async def test_current_consumers_require_exact_source_and_overlay_generation():
    engine, Session = await _database()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    base = {
        "project_id": project_id,
        "level": 1,
        "analysis_run_id": run_id,
        "summary": "Pinned summary.",
        "detailed": "Pinned summary.",
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
                name="summary pinning",
                gitlab_project_id=1,
                gitlab_url="https://example.invalid/project",
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
                **published_run_fields(generation=2, published_at=now),
            )
        )
        await session.commit()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=2,
                overlay_generation=3,
                state="ready",
                published_at=now,
            )
        )
        session.add_all(
            [
                Summary(
                    id=uuid.uuid4(),
                    target_id="module:current",
                    validated_graph_generation=2,
                    validated_overlay_generation=3,
                    **base,
                ),
                Summary(
                    id=uuid.uuid4(),
                    target_id="module:stale-source",
                    validated_graph_generation=1,
                    validated_overlay_generation=3,
                    **base,
                ),
                Summary(
                    id=uuid.uuid4(),
                    target_id="module:stale-overlay",
                    validated_graph_generation=2,
                    validated_overlay_generation=2,
                    **base,
                ),
                Summary(
                    id=uuid.uuid4(),
                    target_id="module:historical",
                    validated_graph_generation=None,
                    validated_overlay_generation=None,
                    validated_at=None,
                    **{key: value for key, value in base.items() if key != "validated_at"},
                ),
            ]
        )
        await session.commit()

        assert await get_module_summary(
            session,
            project_id=project_id,
            target_id="module:current",
            level=1,
        ) is not None
        for target_id in (
            "module:stale-source",
            "module:stale-overlay",
            "module:historical",
        ):
            assert await get_module_summary(
                session,
                project_id=project_id,
                target_id=target_id,
                level=1,
            ) is None
        refs, truncated = await _current_summaries(
            session, project_id, "module:current"
        )
        assert not truncated
        assert refs[0]["validated_graph_generation"] == 2
        assert refs[0]["validated_overlay_generation"] == 3
        assert (await _summary_quality(session, project_id))["summaries_by_model"] == {
            "test-provider": 1
        }

        # A head change alone revokes the cache immediately; no cleanup or
        # supersession transaction is required to hide old prose.
        await session.execute(
            update(GraphHead)
            .where(GraphHead.project_id == project_id)
            .values(generation=3)
        )
        await session.commit()
        assert await get_module_summary(
            session,
            project_id=project_id,
            target_id="module:current",
            level=1,
        ) is None

    await engine.dispose()


@pytest.mark.asyncio
async def test_mcp_flow_listing_excludes_historical_and_stale_flow_rows():
    engine, Session = await _database()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    sections = _flow_sections(run_id)

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="flow pinning",
                gitlab_project_id=2,
                gitlab_url="https://example.invalid/flow",
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
                **published_run_fields(generation=4, published_at=now),
            )
        )
        await session.commit()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=4,
                overlay_generation=1,
                state="ready",
                published_at=now,
            )
        )
        for target_id, source_generation, overlay_generation, validated_at in (
            ("flow:current", 4, 1, now),
            ("flow:old-source", 3, 1, now),
            ("flow:old-overlay", 4, 0, now),
            ("flow:historical", None, None, None),
        ):
            session.add(
                Summary(
                    id=uuid.uuid4(),
                    project_id=project_id,
                    target_id=target_id,
                    level=4,
                    analysis_run_id=run_id,
                    validated_graph_generation=source_generation,
                    validated_overlay_generation=overlay_generation,
                    validated_at=validated_at,
                    summary="Create an order.",
                    detailed="The handler inserts one order row.",
                    claims=sections,
                    open_questions=[],
                    model_used="test-provider",
                    generated_at=now,
                )
            )
        await session.commit()
        flows = await list_flows(session, project_id=project_id)

    await engine.dispose()
    assert [flow["target_id"] for flow in flows] == ["flow:current"]
    assert flows[0]["validated_graph_generation"] == 4
    assert flows[0]["validated_overlay_generation"] == 1


@pytest.mark.asyncio
async def test_current_l4_retires_validated_lineage_but_preserves_history(
    monkeypatch: pytest.MonkeyPatch,
):
    """A current flow replaces stale validated prose, never explicit history."""

    from app.api import flow as flow_api
    from app.graph_publication import GraphReadStamp
    from app.llm.contracts import ResultStatus
    from app.source_snapshot import GitSourceSnapshot

    engine, Session = await _database()
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    now = datetime.now(tz=timezone.utc)
    stale_id = uuid.uuid4()
    historical_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    canonical_flow = _normalise(
        {
            "summary": "Create an order.",
            "detailed": "The handler inserts one order row.",
            "steps": [
                {
                    "order": 1,
                    "tier": "backend",
                    "component": "orders.handler",
                    "action": "insert the row",
                    "signal": {
                        "kind": "sql",
                        "name": "INSERT orders",
                        "fields": [],
                    },
                }
            ],
            "flags": [],
            "data_touched": [
                {"entity": "orders", "op": "insert", "columns": ["id"]}
            ],
            "open_questions": [],
        }
    )
    source_code = "def create_order(): pass"
    source_scope = {
        "provided_files": ["orders.py"],
        "files": [
            {
                "label": "orders.py",
                "tier": "backend",
                "language": "python",
                "provided_chars": len(source_code),
                "included_chars": len(source_code),
                "prompt_truncated": False,
                "input_truncated": False,
            }
        ],
        "omitted_files": [],
        "included_source_chars": len(source_code),
        "max_source_chars": 28_000,
        "truncated": False,
    }

    async def _analyze(**kwargs):  # noqa: ANN003
        kwargs["attempt_state"].attempt_id = attempt_id
        return {"flow": canonical_flow, "source_scope": source_scope}

    async def _lock_candidate(_db, **_kwargs):  # noqa: ANN001, ANN003
        return SimpleNamespace(
            candidate_status="candidate",
            result_status=ResultStatus.PENDING,
            payload=canonical_flow,
            resolved_model="test-provider",
            requested_model="test-provider",
            total_tokens=None,
        )

    async def _classify(_db, **_kwargs):  # noqa: ANN001, ANN003
        return None

    async def _audit(**_kwargs):  # noqa: ANN003
        return None

    monkeypatch.setattr(flow_api, "analyze_flow_via_agent_sdk", _analyze)
    monkeypatch.setattr(
        flow_api,
        "lock_semantic_candidate_for_product",
        _lock_candidate,
    )
    monkeypatch.setattr(flow_api, "classify_attempt_result", _classify)
    monkeypatch.setattr(flow_api, "audit_record", _audit)

    async with Session() as session:
        session.add(
            Project(
                id=project_id,
                name="L4 lineage",
                gitlab_project_id=3,
                gitlab_url="https://example.invalid/l4-lineage",
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
                git_sha="c" * 40,
                scope="full",
                started_at=now,
                completed_at=now,
                **published_run_fields(generation=2, published_at=now),
            )
        )
        await session.commit()
        session.add(
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=2,
                overlay_generation=3,
                state="ready",
                published_at=now,
            )
        )
        session.add(
            LLMCall(
                id=attempt_id,
                project_id=project_id,
                analysis_run_id=None,
                target_id="flow:create-order",
                level=4,
                model_used="test-provider",
                status="completed",
            )
        )
        session.add_all(
            [
                Summary(
                    id=stale_id,
                    project_id=project_id,
                    target_id="flow:create-order",
                    level=4,
                    analysis_run_id=run_id,
                    validated_graph_generation=1,
                    validated_overlay_generation=0,
                    validated_at=now,
                    summary="Stale validated flow.",
                    model_used="test-provider",
                ),
                Summary(
                    id=historical_id,
                    project_id=project_id,
                    target_id="flow:create-order",
                    level=4,
                    analysis_run_id=uuid.uuid4(),
                    summary="Explicit historical flow.",
                    model_used="test-provider",
                ),
            ]
        )
        await session.commit()

        snapshot = GitSourceSnapshot(
            run_id=run_id,
            revision="c" * 40,
            mirrors=(),
            tree_prefix="",
            graph_stamp=GraphReadStamp(
                project_id=project_id,
                generation=2,
                overlay_generation=3,
                current_run_id=run_id,
                published_at=now,
            ),
        )
        result = await flow_api._analyze_and_persist(
            session,
            project_id,
            SimpleNamespace(id=uuid.uuid4()),
            "create order",
            [
                {
                    "tier": "backend",
                    "language": "python",
                    "label": "orders.py",
                    "code": source_code,
                }
            ],
            True,
            [],
            snapshot,
        )
        rows = (
            await session.execute(
                select(Summary).where(
                    Summary.project_id == project_id,
                    Summary.target_id == "flow:create-order",
                    Summary.level == 4,
                )
            )
        ).scalars().all()

    await engine.dispose()
    replacement_id = uuid.UUID(result["summary_id"])
    stale = next(row for row in rows if row.id == stale_id)
    historical = next(row for row in rows if row.id == historical_id)
    replacement = next(row for row in rows if row.id == replacement_id)
    assert stale.superseded_by == replacement_id
    assert historical.superseded_by is None
    assert historical.validated_graph_generation is None
    assert historical.validated_overlay_generation is None
    assert replacement.superseded_by is None
    assert replacement.llm_attempt_id == attempt_id
    assert replacement.validated_graph_generation == 2
    assert replacement.validated_overlay_generation == 3
    assert [
        row.id
        for row in rows
        if row.validated_graph_generation is not None
        and row.superseded_by is None
    ] == [replacement_id]
