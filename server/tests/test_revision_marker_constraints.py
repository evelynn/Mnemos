"""Database-level all-or-none guards for graph revision markers.

SQL ``CHECK`` constraints accept both TRUE and UNKNOWN.  A shape such as
``run_id + git_sha + overlay_generation`` with a NULL source generation must
therefore use explicit ``IS NOT NULL`` predicates; a numeric comparison alone
evaluates to UNKNOWN and does not reject the row.  These tests execute the
model and migration expressions against SQLite, then repeat them on PostgreSQL
when the integration service is available.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import CheckConstraint, Table, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.models.findings import Finding
from app.models.plans import DiffBreakGlassGrant, DiffSubmission, Plan
from app.models.samples import DataSample


_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


@dataclass(frozen=True)
class _ConstraintSpec:
    name: str
    model_table: Table
    migration_file: str
    columns: tuple[tuple[str, str], ...]
    complete_values: tuple[Any, ...]


_FINDING_COLUMNS = (
    ("validated_graph_generation", "BIGINT"),
    ("validated_overlay_generation", "BIGINT"),
    ("validated_at", "TEXT"),
)
_SOURCE_COLUMNS = (
    ("source_run_id", "TEXT"),
    ("source_git_sha", "TEXT"),
    ("source_graph_generation", "BIGINT"),
    ("source_overlay_generation", "BIGINT"),
)
_SAMPLE_SOURCE_COLUMNS = (
    *_SOURCE_COLUMNS,
    ("source_project_db_present", "BOOLEAN"),
    ("source_project_db_id", "TEXT"),
    ("source_policy_hash", "TEXT"),
)
_REVIEW_COLUMNS = (
    ("review_run_id", "TEXT"),
    ("review_source_generation", "BIGINT"),
    ("review_overlay_generation", "BIGINT"),
    ("review_git_sha", "TEXT"),
)

_SPECS = (
    _ConstraintSpec(
        name="ck_findings_graph_validation_marker",
        model_table=Finding.__table__,
        migration_file="0031_finding_graph_currentness.py",
        columns=_FINDING_COLUMNS,
        complete_values=(1, 0, "2026-07-15T00:00:00+00:00"),
    ),
    _ConstraintSpec(
        name="ck_plans_source_revision_shape",
        model_table=Plan.__table__,
        migration_file="0032_plan_revision_provenance.py",
        columns=_SOURCE_COLUMNS,
        complete_values=("run-1", "a" * 40, 1, 0),
    ),
    _ConstraintSpec(
        name="ck_diff_submissions_review_revision",
        model_table=DiffSubmission.__table__,
        migration_file="0033_review_revision_provenance.py",
        columns=_REVIEW_COLUMNS,
        complete_values=("run-1", 1, 0, "b" * 40),
    ),
    _ConstraintSpec(
        name="ck_diff_break_glass_review_revision",
        model_table=DiffBreakGlassGrant.__table__,
        migration_file="0033_review_revision_provenance.py",
        columns=_REVIEW_COLUMNS,
        complete_values=("run-1", 1, 0, "c" * 40),
    ),
    _ConstraintSpec(
        name="ck_data_samples_source_revision_shape",
        model_table=DataSample.__table__,
        migration_file="0034_data_sample_revision_provenance.py",
        columns=_SAMPLE_SOURCE_COLUMNS,
        complete_values=("run-1", "d" * 40, 1, 0, True, "db-1", "e" * 64),
    ),
)


class _RecordedOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return _record


def _model_constraint_sql(spec: _ConstraintSpec) -> str:
    matches = [
        constraint
        for constraint in spec.model_table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == spec.name
    ]
    assert len(matches) == 1
    return str(matches[0].sqltext)


def _migration_constraint_sql(spec: _ConstraintSpec) -> str:
    path = _VERSIONS / spec.migration_file
    module_name = f"_revision_marker_{path.stem}_{spec.name}"
    module_spec = importlib.util.spec_from_file_location(module_name, path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    recorder = _RecordedOp()
    module.op = recorder
    module.upgrade()
    matches = [
        args[2]
        for name, args, _kwargs in recorder.calls
        if name == "create_check_constraint" and args[0] == spec.name
    ]
    assert len(matches) == 1
    return str(matches[0])


def _normalize_sql(value: str) -> str:
    return " ".join(value.lower().split())


def _create_table_sql(table_name: str, spec: _ConstraintSpec, check_sql: str) -> str:
    columns = ", ".join(f"{name} {type_}" for name, type_ in spec.columns)
    return (
        f"CREATE TEMP TABLE {table_name} ({columns}, "
        f"CONSTRAINT {spec.name} CHECK ({check_sql}))"
    )


def _insert_sql(table_name: str, spec: _ConstraintSpec) -> str:
    names = [name for name, _type in spec.columns]
    return (
        f"INSERT INTO {table_name} ({', '.join(names)}) "
        f"VALUES ({', '.join(':' + name for name in names)})"
    )


def _params(spec: _ConstraintSpec, values: tuple[Any, ...]) -> dict[str, Any]:
    return {
        name: value
        for (name, _type), value in zip(spec.columns, values, strict=True)
    }


@pytest.mark.parametrize("spec", _SPECS, ids=lambda spec: spec.name)
def test_model_and_migration_revision_marker_checks_match(
    spec: _ConstraintSpec,
) -> None:
    assert _normalize_sql(_model_constraint_sql(spec)) == _normalize_sql(
        _migration_constraint_sql(spec)
    )


@pytest.mark.parametrize("spec", _SPECS, ids=lambda spec: spec.name)
@pytest.mark.parametrize("source", ("model", "migration"))
def test_sqlite_rejects_every_partially_null_revision_shape(
    spec: _ConstraintSpec,
    source: str,
) -> None:
    check_sql = (
        _model_constraint_sql(spec)
        if source == "model"
        else _migration_constraint_sql(spec)
    )
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(_create_table_sql("revision_marker", spec, check_sql))
        statement = _insert_sql("revision_marker", spec)

        # The explicitly legacy all-NULL shape and the fully populated shape
        # remain valid.
        connection.execute(
            statement,
            _params(spec, (None,) * len(spec.columns)),
        )
        connection.execute(statement, _params(spec, spec.complete_values))

        for missing_index in range(len(spec.columns)):
            partial = list(spec.complete_values)
            partial[missing_index] = None
            with pytest.raises(
                sqlite3.IntegrityError,
                match=spec.name,
            ):
                connection.execute(statement, _params(spec, tuple(partial)))
    finally:
        connection.close()


def _async_postgres_url() -> str | None:
    raw = os.getenv("DATABASE_URL", "")
    if not raw.startswith(("postgres://", "postgresql://", "postgresql+")):
        return None
    if raw.startswith("postgres://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    if raw.startswith("postgresql://"):
        return "postgresql+asyncpg://" + raw.removeprefix("postgresql://")
    if raw.startswith("postgresql+asyncpg://"):
        return raw
    prefix, rest = raw.split("://", 1)
    if prefix.startswith("postgresql+"):
        return "postgresql+asyncpg://" + rest
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_rejects_every_partially_null_revision_shape() -> None:
    url = _async_postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                table_number = 0
                for spec in _SPECS:
                    for check_sql in (
                        _model_constraint_sql(spec),
                        _migration_constraint_sql(spec),
                    ):
                        table_number += 1
                        table_name = f"revision_marker_{table_number}"
                        await connection.execute(
                            text(_create_table_sql(table_name, spec, check_sql))
                        )
                        statement = text(_insert_sql(table_name, spec))
                        await connection.execute(
                            statement,
                            _params(spec, (None,) * len(spec.columns)),
                        )
                        await connection.execute(
                            statement,
                            _params(spec, spec.complete_values),
                        )

                        for missing_index in range(len(spec.columns)):
                            partial = list(spec.complete_values)
                            partial[missing_index] = None
                            savepoint = await connection.begin_nested()
                            try:
                                with pytest.raises(DBAPIError):
                                    await connection.execute(
                                        statement,
                                        _params(spec, tuple(partial)),
                                    )
                            finally:
                                await savepoint.rollback()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
