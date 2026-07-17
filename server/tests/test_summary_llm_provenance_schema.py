"""Schema contract for durable semantic-candidate Summary provenance.

0041 deliberately does not change a product writer.  It establishes the
expand-only database boundary that a writer must cross atomically: one LLM
attempt id, its semantic binding fingerprint, and the digest of the grounded
Summary projection.  The tests execute the real migration on SQLite and, when
available, PostgreSQL instead of accepting a migration-text mock as evidence.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import CheckConstraint, Index, create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models.findings import Summary
from app.testing.sqlite_polyglot import install_polyglot


_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0041_summary_llm_provenance.py"
)
_PROVENANCE_COLUMNS = {
    "llm_attempt_id",
    "semantic_binding_fingerprint",
    "projection_sha256",
}
_CHECK_NAMES = {
    "ck_summaries_llm_provenance_shape",
    "ck_summaries_llm_provenance_sha256",
}
_VALID_BINDING = "a" * 64
_VALID_PROJECTION = "0123456789abcdef" * 4


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_test_0041_summary_llm_provenance",
        _MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _normalized(value: object) -> str:
    return " ".join(str(value).lower().split())


def _model_checks() -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Summary.__table__.constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name in _CHECK_NAMES
    }


def _attempt_index() -> Index:
    matches = [
        index
        for index in Summary.__table__.indexes
        if index.name == "uq_summaries_llm_attempt"
    ]
    assert len(matches) == 1
    return matches[0]


def test_model_and_0041_expose_the_same_expand_only_contract() -> None:
    migration = _load_migration()
    columns = Summary.__table__.columns

    assert migration.revision == "0041_summary_llm_provenance"
    assert migration.down_revision == "0040_llm_semantic_products"
    assert _PROVENANCE_COLUMNS <= set(columns.keys())
    assert all(columns[name].nullable for name in _PROVENANCE_COLUMNS)

    foreign_keys = list(columns.llm_attempt_id.foreign_keys)
    assert len(foreign_keys) == 1
    assert foreign_keys[0].target_fullname == "llm_calls.id"
    assert foreign_keys[0].ondelete == "RESTRICT"

    checks = _model_checks()
    assert set(checks) == _CHECK_NAMES
    assert _normalized(checks["ck_summaries_llm_provenance_shape"]) == (
        _normalized(migration._PROVENANCE_SHAPE)
    )
    assert _normalized(checks["ck_summaries_llm_provenance_sha256"]) == (
        _normalized(migration._PROVENANCE_SHA256)
    )

    index = _attempt_index()
    assert index.unique is True
    assert [column.name for column in index.columns] == ["llm_attempt_id"]
    for dialect in ("postgresql", "sqlite"):
        assert _normalized(index.dialect_options[dialect]["where"]) == (
            "llm_attempt_id is not null"
        )


def _run_upgrade(connection, migration: ModuleType) -> None:  # noqa: ANN001
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.upgrade()


def _run_downgrade(connection, migration: ModuleType) -> None:  # noqa: ANN001
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        migration.downgrade()


def _insert_summary(
    connection,  # noqa: ANN001
    *,
    row_id: str,
    attempt_id: str | None,
    binding: str | None,
    projection: str | None,
) -> None:
    connection.execute(
        text(
            "INSERT INTO summaries ("
            "id, marker, llm_attempt_id, semantic_binding_fingerprint, "
            "projection_sha256) VALUES ("
            ":id, 'kept', :attempt, :binding, :projection)"
        ),
        {
            "id": row_id,
            "attempt": attempt_id,
            "binding": binding,
            "projection": projection,
        },
    )


def _sqlite_rejects(connection, *, match: str, **values: str | None) -> None:  # noqa: ANN001
    savepoint = connection.begin_nested()
    try:
        with pytest.raises(IntegrityError, match=match):
            _insert_summary(connection, **values)
    finally:
        savepoint.rollback()


def test_sqlite_real_migration_enforces_shape_digests_and_single_ownership() -> None:
    install_polyglot()
    migration = _load_migration()
    engine = create_engine("sqlite:///:memory:")
    attempt_1 = str(uuid.uuid4())
    attempt_2 = str(uuid.uuid4())

    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE llm_calls (id CHAR(36) PRIMARY KEY)")
            )
            connection.execute(
                text(
                    "CREATE TABLE summaries ("
                    "id CHAR(36) PRIMARY KEY, marker TEXT NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO summaries VALUES ('legacy', 'kept')")
            )
            connection.execute(
                text("INSERT INTO llm_calls (id) VALUES (:a), (:b)"),
                {"a": attempt_1, "b": attempt_2},
            )

            _run_upgrade(connection, migration)
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(summaries)"))
            }
            assert _PROVENANCE_COLUMNS <= columns
            assert connection.execute(
                text(
                    "SELECT llm_attempt_id, semantic_binding_fingerprint, "
                    "projection_sha256 FROM summaries WHERE id = 'legacy'"
                )
            ).one() == (None, None, None)

            # Legacy/deterministic rows can remain unbound indefinitely.
            _insert_summary(
                connection,
                row_id="deterministic",
                attempt_id=None,
                binding=None,
                projection=None,
            )

            partials = (
                (attempt_1, None, None),
                (None, _VALID_BINDING, None),
                (None, None, _VALID_PROJECTION),
                (attempt_1, _VALID_BINDING, None),
                (attempt_1, None, _VALID_PROJECTION),
                (None, _VALID_BINDING, _VALID_PROJECTION),
            )
            for position, (attempt, binding, projection) in enumerate(partials):
                _sqlite_rejects(
                    connection,
                    match="ck_summaries_llm_provenance_shape",
                    row_id=f"partial-{position}",
                    attempt_id=attempt,
                    binding=binding,
                    projection=projection,
                )

            for position, (binding, projection) in enumerate(
                (
                    ("A" * 64, _VALID_PROJECTION),
                    ("g" * 64, _VALID_PROJECTION),
                    ("a" * 63, _VALID_PROJECTION),
                    (_VALID_BINDING, "F" * 64),
                    (_VALID_BINDING, "z" * 64),
                    (_VALID_BINDING, "f" * 65),
                )
            ):
                _sqlite_rejects(
                    connection,
                    match="ck_summaries_llm_provenance_sha256",
                    row_id=f"bad-digest-{position}",
                    attempt_id=attempt_1,
                    binding=binding,
                    projection=projection,
                )

            _insert_summary(
                connection,
                row_id="product-1",
                attempt_id=attempt_1,
                binding=_VALID_BINDING,
                projection=_VALID_PROJECTION,
            )
            _sqlite_rejects(
                connection,
                match="UNIQUE constraint failed: summaries.llm_attempt_id",
                row_id="duplicate-product",
                attempt_id=attempt_1,
                binding="b" * 64,
                projection="c" * 64,
            )
            _insert_summary(
                connection,
                row_id="product-2",
                attempt_id=attempt_2,
                binding="d" * 64,
                projection="e" * 64,
            )
            _sqlite_rejects(
                connection,
                match="FOREIGN KEY constraint failed",
                row_id="missing-attempt",
                attempt_id=str(uuid.uuid4()),
                binding="d" * 64,
                projection="e" * 64,
            )

            # Populated provenance is a semantic downgrade blocker, and the
            # guard runs before the columns/index are changed.
            with pytest.raises(
                RuntimeError,
                match="cannot downgrade 0041: durable Summary LLM provenance",
            ):
                _run_downgrade(connection, migration)
            assert _PROVENANCE_COLUMNS <= {
                row[1] for row in connection.execute(text("PRAGMA table_info(summaries)"))
            }

            connection.execute(
                text("DELETE FROM summaries WHERE llm_attempt_id IS NOT NULL")
            )
            _run_downgrade(connection, migration)
            columns = {
                row[1] for row in connection.execute(text("PRAGMA table_info(summaries)"))
            }
            assert not (_PROVENANCE_COLUMNS & columns)
            assert connection.execute(
                text("SELECT marker FROM summaries ORDER BY id")
            ).scalars().all() == ["kept", "kept"]
    finally:
        engine.dispose()


def _postgres_url() -> str | None:
    raw = os.getenv("DATABASE_URL", "")
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    if raw.startswith("postgresql+"):
        _prefix, rest = raw.split("://", 1)
        return "postgresql+asyncpg://" + rest
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_real_0041_ddl_and_downgrade_guard() -> None:
    url = _postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")
    migration = _load_migration()
    engine = create_async_engine(url, poolclass=NullPool)
    schema = f"mnemos_migration_0041_{uuid.uuid4().hex}"
    attempt_id = uuid.uuid4()

    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as connection:
            await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await connection.execute(text("CREATE TABLE llm_calls (id uuid PRIMARY KEY)"))
            await connection.execute(
                text(
                    "CREATE TABLE summaries ("
                    "id text PRIMARY KEY, marker text NOT NULL)"
                )
            )
            await connection.execute(
                text("INSERT INTO summaries VALUES ('legacy', 'kept')")
            )
            await connection.execute(
                text("INSERT INTO llm_calls (id) VALUES (:id)"),
                {"id": attempt_id},
            )
            await connection.run_sync(_run_upgrade, migration)

            assert await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'summaries' "
                    "AND column_name IN ('llm_attempt_id', "
                    "'semantic_binding_fingerprint', 'projection_sha256')"
                ),
                {"schema": schema},
            ) == 3
            await connection.execute(
                text(
                    "INSERT INTO summaries ("
                    "id, marker, llm_attempt_id, semantic_binding_fingerprint, "
                    "projection_sha256) VALUES ("
                    "'product', 'kept', :attempt, :binding, :projection)"
                ),
                {
                    "attempt": attempt_id,
                    "binding": _VALID_BINDING,
                    "projection": _VALID_PROJECTION,
                },
            )

            savepoint = await connection.begin_nested()
            try:
                with pytest.raises(DBAPIError, match="cannot downgrade 0041"):
                    await connection.run_sync(_run_downgrade, migration)
            finally:
                await savepoint.rollback()

            assert await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'summaries' "
                    "AND column_name = 'llm_attempt_id'"
                ),
                {"schema": schema},
            ) == 1
            await connection.execute(text("DELETE FROM summaries WHERE id = 'product'"))
            await connection.run_sync(_run_downgrade, migration)
            assert await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = :schema AND table_name = 'summaries' "
                    "AND column_name IN ('llm_attempt_id', "
                    "'semantic_binding_fingerprint', 'projection_sha256')"
                ),
                {"schema": schema},
            ) == 0
            assert await connection.scalar(
                text("SELECT marker FROM summaries WHERE id = 'legacy'")
            ) == "kept"
    finally:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
        finally:
            await engine.dispose()
