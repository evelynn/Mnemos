"""Data samples are authorised by, and visible only at, one graph revision."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import BigInteger, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from app.testing.sqlite_polyglot import install_polyglot

install_polyglot()

from app.api import data as data_api  # noqa: E402
from app.mcp import data_queries as mcp_data_queries  # noqa: E402
from app.mcp.data_queries import (  # noqa: E402
    get_column_stats,
    get_data_entity as mcp_get_data_entity,
    get_sample_data,
    search_data,
)
from app.models import all_models as _all_models  # noqa: E402,F401
from app.models.base import Base  # noqa: E402
from app.models.graph import AnalysisRun, GraphHead, Node  # noqa: E402
from app.models.projects import Project, ProjectDB  # noqa: E402
from app.models.samples import DataSample  # noqa: E402
from app.sample_currentness import (  # noqa: E402
    read_current_sample_policy,
    revalidate_current_sample_policy,
    sample_policy_for_project_db,
)
from app.testing.graph_publication import published_run_fields  # noqa: E402


@pytest_asyncio.fixture
async def database():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield Session
    finally:
        await engine.dispose()


async def _seed_ready_entity(
    session: AsyncSession,
    *,
    component_id: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, str]:
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    entity_id = "data:public.orders"
    now = datetime.now(tz=timezone.utc)
    session.add(
        Project(
            id=project_id,
            name="sample revision currentness",
            gitlab_project_id=uuid.uuid4().int % 2_000_000_000,
            gitlab_url="https://example.invalid/sample-currentness",
            default_branch="main",
            languages=["sql"],
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
            **published_run_fields(generation=1, published_at=now),
        )
    )
    await session.flush()
    entity_data = {"name": "orders", "is_sensitive": False}
    if component_id is not None:
        entity_data["component_id"] = component_id
    session.add_all(
        [
            GraphHead(
                project_id=project_id,
                current_run_id=run_id,
                generation=1,
                overlay_generation=0,
                state="ready",
                published_at=now,
            ),
            Node(
                id=entity_id,
                project_id=project_id,
                kind="DataEntity",
                data=entity_data,
                certainty="verified",
                created_by=["live_schema"],
                valid_from=now,
            ),
        ]
    )
    await session.commit()
    return project_id, run_id, entity_id


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/refresh_sample",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 1234),
        }
    )


async def _noop(*_args, **_kwargs):
    return None


def _project_db(
    project_id: uuid.UUID,
    component_id: str,
    *,
    db_id: uuid.UUID | None = None,
    masking_rules: dict | None = None,
    sensitive_tables: list[str] | None = None,
) -> ProjectDB:
    return ProjectDB(
        id=db_id or uuid.uuid4(),
        project_id=project_id,
        kind="postgres",
        display_name="orders database",
        component_id=component_id,
        secret_id=None,
        allow_awr=False,
        sensitive_tables=sensitive_tables or [],
        masking_rules=masking_rules or {},
        maintenance_windows=[],
    )


async def _assert_hidden_by_every_reader(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entity_id: str,
) -> None:
    assert await get_sample_data(
        session, project_id=project_id, entity_id=entity_id
    ) == {"error": "no_sample_yet"}
    assert await get_column_stats(
        session,
        project_id=project_id,
        entity_id=entity_id,
        column="order_id",
    ) == {"error": "no_stats"}
    assert await search_data(
        session, project_id=project_id, value_pattern="ordinary|7"
    ) == []
    mcp_entity = await mcp_get_data_entity(
        session, project_id=project_id, entity_id=entity_id
    )
    assert mcp_entity is not None and mcp_entity["sample_available"] is False
    api_entity = await data_api.get_data_entity(
        project_id,
        entity_id,
        SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
        db=session,
    )
    assert api_entity["sample_available"] is False
    with pytest.raises(data_api.HTTPException) as error:
        await data_api.get_sample(
            project_id,
            entity_id,
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            db=session,
        )
    assert error.value.status_code == 404


@pytest.mark.asyncio
async def test_refresh_binds_sample_and_head_change_hides_it(
    database, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(data_api, "rl_enforce", _noop)
    monkeypatch.setattr(data_api, "audit_record", _noop)

    async with database() as session:
        project_id, run_id, entity_id = await _seed_ready_entity(session)
        result = await data_api.refresh_sample(
            project_id,
            entity_id,
            data_api.SampleIngest(
                columns=["order_id", "description"],
                rows=[[1, "ordinary test value"]],
                row_count_estimate=1,
            ),
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            _request(),
            db=session,
        )

        saved = (
            await session.execute(
                select(DataSample).where(DataSample.id == uuid.UUID(result["id"]))
            )
        ).scalar_one()
        assert saved.source_run_id == run_id
        assert saved.source_git_sha == "a" * 40
        assert saved.source_graph_generation == 1
        assert saved.source_overlay_generation == 0
        assert saved.source_project_db_present is False
        assert saved.source_project_db_id is None
        assert saved.source_policy_hash is not None
        assert len(saved.source_policy_hash) == 64

        visible = await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        )
        assert visible["rows"] == [
            {"order_id": 1, "description": "ordinary test value"}
        ]
        assert visible["source_run_id"] == str(run_id)

        # A newer legacy/null-marker row must never shadow the bound sample.
        session.add(
            DataSample(
                id=uuid.uuid4(),
                project_id=project_id,
                data_entity_id=entity_id,
                sample_rows=[{"order_id": 999}],
                row_count_estimate=1,
                column_stats={},
                masking_applied=True,
            )
        )
        await session.commit()
        still_visible = await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        )
        assert still_visible["rows"] == visible["rows"]

        head = await session.get(GraphHead, project_id)
        assert head is not None
        head.overlay_generation = 1
        await session.commit()

        assert await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        ) == {"error": "no_sample_yet"}
        assert await get_column_stats(
            session,
            project_id=project_id,
            entity_id=entity_id,
            column="order_id",
        ) == {"error": "no_stats"}
        assert await search_data(
            session, project_id=project_id, value_pattern="7"
        ) == []
        mcp_entity = await mcp_get_data_entity(
            session, project_id=project_id, entity_id=entity_id
        )
        assert mcp_entity is not None
        assert mcp_entity["sample_available"] is False

        api_entity = await data_api.get_data_entity(
            project_id,
            entity_id,
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            db=session,
        )
        assert api_entity["sample_available"] is False
        with pytest.raises(data_api.HTTPException) as error:
            await data_api.get_sample(
                project_id,
                entity_id,
                SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
                db=session,
            )
        assert error.value.status_code == 404

        # Even if corrupted/imported rows agree on a non-canonical value,
        # sample readers fail closed instead of treating length as identity.
        run = await session.get(AnalysisRun, run_id)
        assert run is not None
        run.git_sha = "z" * 40
        saved.source_git_sha = "z" * 40
        await session.commit()
        assert await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        ) == {"error": "no_sample_yet"}
        entity = await mcp_get_data_entity(
            session, project_id=project_id, entity_id=entity_id
        )
        assert entity is not None and entity["sample_available"] is False


@pytest.mark.asyncio
async def test_every_sample_reader_requires_exact_canonical_git_sha(database):
    async with database() as session:
        project_id, run_id, entity_id = await _seed_ready_entity(session)
        policy = sample_policy_for_project_db(None)
        sample = DataSample(
            id=uuid.uuid4(),
            project_id=project_id,
            data_entity_id=entity_id,
            sample_rows=[{"order_id": 7}],
            row_count_estimate=1,
            column_stats={"order_id": {"distinct": 1}},
            masking_applied=True,
            source_run_id=run_id,
            source_git_sha="b" * 40,
            source_graph_generation=1,
            source_overlay_generation=0,
            source_project_db_present=policy.project_db_present,
            source_project_db_id=policy.project_db_id,
            source_policy_hash=policy.policy_hash,
        )
        session.add(sample)
        await session.commit()

        assert await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        ) == {"error": "no_sample_yet"}


@pytest.mark.asyncio
async def test_masking_policy_change_hides_existing_sample_from_every_reader(
    database, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(data_api, "rl_enforce", _noop)
    monkeypatch.setattr(data_api, "audit_record", _noop)

    async with database() as session:
        component_id = "db.postgres.orders"
        project_id, _run_id, entity_id = await _seed_ready_entity(
            session, component_id=component_id
        )
        binding = _project_db(
            project_id,
            component_id,
            masking_rules={"full": ["description"]},
        )
        session.add(binding)
        await session.commit()

        result = await data_api.refresh_sample(
            project_id,
            entity_id,
            data_api.SampleIngest(
                columns=["order_id", "description"],
                rows=[[7, "ordinary test value"]],
                row_count_estimate=1,
            ),
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            _request(),
            db=session,
        )
        sample = await session.get(DataSample, uuid.UUID(result["id"]))
        assert sample is not None
        assert sample.sample_rows == [{"order_id": 7, "description": "***"}]
        assert sample.source_project_db_present is True
        assert sample.source_project_db_id == binding.id

        binding.masking_rules = {"full": ["description", "order_id"]}
        await session.commit()

        await _assert_hidden_by_every_reader(
            session, project_id=project_id, entity_id=entity_id
        )


@pytest.mark.asyncio
async def test_project_db_deletion_hides_existing_sample(
    database, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(data_api, "rl_enforce", _noop)
    monkeypatch.setattr(data_api, "audit_record", _noop)

    async with database() as session:
        component_id = "db.postgres.orders"
        project_id, _run_id, entity_id = await _seed_ready_entity(
            session, component_id=component_id
        )
        binding = _project_db(project_id, component_id)
        session.add(binding)
        await session.commit()
        await data_api.refresh_sample(
            project_id,
            entity_id,
            data_api.SampleIngest(columns=["order_id"], rows=[[7]]),
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            _request(),
            db=session,
        )

        await session.execute(delete(ProjectDB).where(ProjectDB.id == binding.id))
        await session.commit()

        await _assert_hidden_by_every_reader(
            session, project_id=project_id, entity_id=entity_id
        )


@pytest.mark.asyncio
async def test_stronger_sensitive_table_policy_hides_and_blocks_recapture(
    database, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(data_api, "rl_enforce", _noop)
    monkeypatch.setattr(data_api, "audit_record", _noop)

    async with database() as session:
        component_id = "db.postgres.orders"
        project_id, _run_id, entity_id = await _seed_ready_entity(
            session, component_id=component_id
        )
        binding = _project_db(project_id, component_id)
        session.add(binding)
        await session.commit()
        await data_api.refresh_sample(
            project_id,
            entity_id,
            data_api.SampleIngest(
                columns=["order_id", "description"],
                rows=[[7, "ordinary test value"]],
            ),
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            _request(),
            db=session,
        )

        binding.sensitive_tables = ["ORDERS"]
        await session.commit()
        await _assert_hidden_by_every_reader(
            session, project_id=project_id, entity_id=entity_id
        )

        with pytest.raises(data_api.HTTPException) as error:
            await data_api.refresh_sample(
                project_id,
                entity_id,
                data_api.SampleIngest(columns=["order_id"], rows=[[8]]),
                SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
                _request(),
                db=session,
            )
        assert error.value.status_code == 403
        assert error.value.detail == "sensitive_table_blocked"
        sample_count = (
            await session.execute(select(func.count()).select_from(DataSample))
        ).scalar_one()
        assert sample_count == 1


def test_policy_hash_is_stable_and_binds_default_engine_version(
    monkeypatch: pytest.MonkeyPatch,
):
    db_id = uuid.uuid4()
    left = SimpleNamespace(
        id=db_id,
        masking_rules={"partial": ["name", "email"], "full": ["token", "ssn"]},
        sensitive_tables=["Orders", "USERS"],
    )
    right = SimpleNamespace(
        id=db_id,
        masking_rules={
            "full": ["ssn", "token", "ssn"],
            "partial": ["email", "name"],
        },
        sensitive_tables=["users", "orders", "orders"],
    )
    first = sample_policy_for_project_db(left)  # type: ignore[arg-type]
    second = sample_policy_for_project_db(right)  # type: ignore[arg-type]
    assert first.policy_hash == second.policy_hash
    assert first.project_db_present is True
    assert first.project_db_id == db_id

    monkeypatch.setattr(
        "app.sample_currentness.MASKING_ENGINE_POLICY_VERSION",
        "mnemos.masking-engine.v2",
    )
    assert sample_policy_for_project_db(left).policy_hash != first.policy_hash  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_final_policy_revalidation_returns_no_sample_payload(
    database, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(data_api, "rl_enforce", _noop)
    monkeypatch.setattr(data_api, "audit_record", _noop)

    async with database() as session:
        project_id, _run_id, entity_id = await _seed_ready_entity(session)
        await data_api.refresh_sample(
            project_id,
            entity_id,
            data_api.SampleIngest(
                columns=["order_id", "description"],
                rows=[[7, "ordinary test value"]],
            ),
            SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
            _request(),
            db=session,
        )

        async def _changed(*_args, **_kwargs) -> bool:
            return False

        monkeypatch.setattr(
            mcp_data_queries, "revalidate_current_sample_policy", _changed
        )
        assert await get_sample_data(
            session, project_id=project_id, entity_id=entity_id
        ) == {"error": "no_sample_yet"}
        assert await get_column_stats(
            session,
            project_id=project_id,
            entity_id=entity_id,
            column="order_id",
        ) == {"error": "no_stats"}
        assert await search_data(
            session, project_id=project_id, value_pattern="ordinary"
        ) == []
        entity = await mcp_get_data_entity(
            session, project_id=project_id, entity_id=entity_id
        )
        assert entity is not None and entity["sample_available"] is False

        monkeypatch.setattr(data_api, "revalidate_current_sample_policy", _changed)
        with pytest.raises(data_api.HTTPException) as sample_error:
            await data_api.get_sample(
                project_id,
                entity_id,
                SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
                db=session,
            )
        assert sample_error.value.status_code == 409
        assert sample_error.value.detail["error"] == "sample_policy_changed_retry"
        with pytest.raises(data_api.HTTPException) as entity_error:
            await data_api.get_data_entity(
                project_id,
                entity_id,
                SimpleNamespace(id=uuid.uuid4()),  # type: ignore[arg-type]
                db=session,
            )
        assert entity_error.value.status_code == 409


@pytest.mark.asyncio
async def test_revalidation_refreshes_cached_binding_and_detects_create_update_delete(
    database,
):
    component_id = "db.postgres.orders"
    async with database() as session_a, database() as session_b:
        project_id, _run_id, _entity_id = await _seed_ready_entity(
            session_a, component_id=component_id
        )

        absent = await read_current_sample_policy(
            session_a, project_id=project_id, component_id=component_id
        )
        assert absent.project_db_present is False
        binding = _project_db(project_id, component_id)
        session_b.add(binding)
        await session_b.commit()
        assert not await revalidate_current_sample_policy(
            session_a,
            project_id=project_id,
            component_id=component_id,
            expected=absent,
        )

        before_update = await read_current_sample_policy(
            session_a, project_id=project_id, component_id=component_id
        )
        cached_binding = await session_a.get(ProjectDB, binding.id)
        assert cached_binding is not None
        writer_binding = await session_b.get(ProjectDB, binding.id)
        assert writer_binding is not None
        writer_binding.masking_rules = {"full": ["order_id"]}
        await session_b.commit()
        assert not await revalidate_current_sample_policy(
            session_a,
            project_id=project_id,
            component_id=component_id,
            expected=before_update,
        )
        assert cached_binding.masking_rules == {"full": ["order_id"]}

        before_delete = await read_current_sample_policy(
            session_a, project_id=project_id, component_id=component_id
        )
        await session_b.execute(delete(ProjectDB).where(ProjectDB.id == binding.id))
        await session_b.commit()
        assert not await revalidate_current_sample_policy(
            session_a,
            project_id=project_id,
            component_id=component_id,
            expected=before_delete,
        )


def test_data_sample_model_requires_all_or_no_revision_markers():
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in DataSample.__table__.constraints
        if constraint.name and hasattr(constraint, "sqltext")
    }
    shape = checks["ck_data_samples_source_revision_shape"]
    assert "source_run_id IS NULL" in shape
    assert "source_overlay_generation >= 0" in shape
    assert "source_graph_generation > 0" in shape
    assert "source_project_db_present IS NULL" in shape
    assert "source_project_db_present = true" in shape
    assert "source_project_db_present = false" in shape
    assert "length(source_policy_hash) = 64" in shape
    assert isinstance(DataSample.__table__.c.row_count_estimate.type, BigInteger)
