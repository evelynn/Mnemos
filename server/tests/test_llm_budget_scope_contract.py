"""Schema contract for crash-durable optional-LLM budget scopes."""

from __future__ import annotations

import importlib.util
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from app.models.findings import LLMCall, LLMBudgetScope


_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0037_llm_budget_scopes.py"
)

_COLUMNS = {
    "id",
    "project_id",
    "analysis_run_id",
    "scope_kind",
    "policy_version",
    "max_calls",
    "max_input_tokens",
    "max_output_tokens",
    "wall_time_sec",
    "calls_reserved",
    "input_tokens_reserved",
    "output_tokens_reserved",
    "exhausted_reason",
    "started_at",
    "deadline_at",
}

_CONSTRAINTS = {
    "ck_llm_budget_scopes_scope_kind",
    "ck_llm_budget_scopes_identity",
    "ck_llm_budget_scopes_policy_version",
    "ck_llm_budget_scopes_limits",
    "ck_llm_budget_scopes_reservations",
    "ck_llm_budget_scopes_deadline",
    "ck_llm_budget_scopes_exhausted_reason",
}

_SCOPE_INDEXES = {
    "ix_llm_budget_scopes_project_started",
    "ix_llm_budget_scopes_analysis_run",
    "ix_llm_budget_scopes_deadline",
}
_OPEN_ATTEMPT_INDEX = "ix_llm_calls_open_budget_started"


def _valid_scope(**overrides: Any) -> dict[str, Any]:
    started_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    scope_id = uuid.uuid4()
    values: dict[str, Any] = {
        "id": scope_id,
        "project_id": uuid.uuid4(),
        "analysis_run_id": scope_id,
        "scope_kind": "analysis_run",
        "policy_version": "llm-hard-budget.v1",
        "max_calls": 4,
        "max_input_tokens": 120_000,
        "max_output_tokens": 4_800,
        "wall_time_sec": 180,
        "started_at": started_at,
        "deadline_at": started_at + timedelta(seconds=180),
    }
    values.update(overrides)
    return values


@pytest.fixture
def scope_engine():
    engine = create_engine("sqlite://")
    LLMBudgetScope.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_scope_defaults_persist_zero_reservations(scope_engine) -> None:
    values = _valid_scope(analysis_run_id=None, scope_kind="request")

    with scope_engine.begin() as connection:
        connection.execute(LLMBudgetScope.__table__.insert().values(**values))
        row = connection.execute(
            select(LLMBudgetScope.__table__).where(
                LLMBudgetScope.id == values["id"]
            )
        ).mappings().one()

    assert row["calls_reserved"] == 0
    assert row["input_tokens_reserved"] == 0
    assert row["output_tokens_reserved"] == 0
    assert row["exhausted_reason"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope_kind": "session"},
        {"analysis_run_id": None},
        {"analysis_run_id": uuid.uuid4()},
        {"scope_kind": "request"},
        {"policy_version": ""},
        {"policy_version": "v" * 33},
        {"max_calls": 0},
        {"max_input_tokens": -1},
        {"max_output_tokens": 0},
        {"wall_time_sec": 0},
        {"calls_reserved": -1},
        {"calls_reserved": 5},
        {"input_tokens_reserved": -1},
        {"input_tokens_reserved": 120_001},
        {"output_tokens_reserved": -1},
        {"output_tokens_reserved": 4_801},
        {"exhausted_reason": ""},
        {"exhausted_reason": "x" * 65},
        {
            "deadline_at": datetime(
                2026, 7, 16, 11, 59, 59, tzinfo=timezone.utc
            )
        },
    ],
)
def test_database_constraints_reject_invalid_scope_shape(
    scope_engine, overrides: dict[str, Any]
) -> None:
    with pytest.raises(IntegrityError), scope_engine.begin() as connection:
        connection.execute(
            LLMBudgetScope.__table__.insert().values(**_valid_scope(**overrides))
        )


def test_model_exposes_strict_scope_schema_without_premature_call_fk() -> None:
    table = LLMBudgetScope.__table__

    assert table.name == "llm_budget_scopes"
    assert set(table.columns.keys()) == _COLUMNS
    assert {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, sa.CheckConstraint)
    } == _CONSTRAINTS
    assert {index.name for index in table.indexes} == _SCOPE_INDEXES
    open_attempt = next(
        index
        for index in LLMCall.__table__.indexes
        if index.name == _OPEN_ATTEMPT_INDEX
    )
    assert [column.name for column in open_attempt.columns] == [
        "budget_scope_id",
        "started_at",
    ]
    assert "attempt_status = 'started'" in str(
        open_attempt.dialect_options["postgresql"]["where"]
    )
    for column_name in (
        "max_calls",
        "max_input_tokens",
        "max_output_tokens",
        "wall_time_sec",
        "calls_reserved",
        "input_tokens_reserved",
        "output_tokens_reserved",
    ):
        assert isinstance(table.c[column_name].type, sa.BigInteger)
    assert table.c.scope_kind.type.length == 24
    assert table.c.policy_version.type.length == 32
    assert table.c.exhausted_reason.type.length == 64
    assert table.c.started_at.type.timezone is True
    assert table.c.deadline_at.type.timezone is True
    assert not LLMCall.__table__.c.budget_scope_id.foreign_keys


class _RecordedOp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def __getattr__(self, name: str):
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return record


def _load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("_llm_budget_scope_0037", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0037_migration_matches_model_contract_and_chain() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder

    migration.upgrade()

    assert migration.down_revision == "0036_llm_physical_attempt"
    create_call = next(call for call in recorder.calls if call[0] == "create_table")
    assert create_call[1][0] == "llm_budget_scopes"
    elements = create_call[1][1:]
    migration_columns = {
        element.name: element
        for element in elements
        if isinstance(element, sa.Column)
    }
    assert set(migration_columns) == _COLUMNS
    migration_constraints = {
        element.name: " ".join(str(element.sqltext).split())
        for element in elements
        if isinstance(element, sa.CheckConstraint)
    }
    model_constraints = {
        constraint.name: " ".join(str(constraint.sqltext).split())
        for constraint in LLMBudgetScope.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert set(migration_constraints) == _CONSTRAINTS
    assert migration_constraints == model_constraints
    for counter in (
        "calls_reserved",
        "input_tokens_reserved",
        "output_tokens_reserved",
    ):
        assert str(migration_columns[counter].server_default.arg) == "0"
    assert {
        args[0]
        for name, args, _kwargs in recorder.calls
        if name == "create_index"
    } == _SCOPE_INDEXES | {_OPEN_ATTEMPT_INDEX}


def test_0037_downgrade_refuses_loss_before_destructive_ddl() -> None:
    migration = _load_migration()
    recorder = _RecordedOp()
    migration.op = recorder

    migration.downgrade()

    names = [name for name, _args, _kwargs in recorder.calls]
    assert names[0] == "execute"
    guard = " ".join(str(recorder.calls[0][1][0]).lower().split())
    assert "if exists (select 1 from llm_budget_scopes" in guard
    assert "from llm_calls where usage_status = 'reported_partial'" in guard
    assert "estimated_cost_usd is not null" in guard
    assert "reported_cost_usd is null" in guard
    assert "llm calls rely on estimated-cost usage shape" in guard
    assert "cannot downgrade 0037" in guard
    assert "policy snapshots and reservations" in guard
    assert "exceed its original hard limits" in guard
    assert names.index("execute") < names.index("drop_index")
    assert names.index("execute") < names.index("drop_table")
