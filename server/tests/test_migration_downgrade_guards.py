"""Fail-closed compatibility guards for graph-publication downgrades.

The ordinary empty-schema Alembic round trip proves that downgrade DDL is
syntactically usable, but it cannot prove that populated, non-representable
state is rejected *before* columns or tables are dropped.  These tests pin
both the semantic blocker for revisions 0028-0034 and its execution order.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool


_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
_DESTRUCTIVE_OPS = {
    "drop_column",
    "drop_constraint",
    "drop_index",
    "drop_table",
}


@dataclass
class _RecordedOp:
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = field(
        default_factory=list
    )

    def __getattr__(self, name: str):
        def _record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args, kwargs))

        return _record


def _load_migration(filename: str) -> ModuleType:
    path = _VERSIONS / filename
    module_name = f"_downgrade_guard_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_downgrade(filename: str) -> list[tuple[str, tuple[Any, ...], dict[str, Any]]]:
    module = _load_migration(filename)
    recorder = _RecordedOp()
    module.op = recorder
    module.downgrade()
    return recorder.calls


def _guard_sql(filename: str) -> str:
    calls = _record_downgrade(filename)
    execute_calls = [args for name, args, _kwargs in calls if name == "execute"]
    assert len(execute_calls) == 1, (
        f"{filename}: expected one compatibility guard, got "
        f"{len(execute_calls)} op.execute calls"
    )
    return str(execute_calls[0][0])


_GUARDS = {
    "0028_graph_fact_overlays.py": (
        "cannot downgrade 0028",
        (
            "graph_node_human_overlays",
            "graph_edge_human_overlays",
            "graph_edge_runtime_overlays",
            "graph_edge_runtime_cursors",
            "export the overlay/cursor tables",
            "explicitly retire",
            "runtime counts may be replayed",
        ),
    ),
    "0029_analysis_run_publication_lifecycle.py": (
        "cannot downgrade 0029",
        (
            "status in ('published', 'partial')",
            "drain all workers",
            "explicitly map each affected run",
            "publication receipt",
            "postprocess outcome",
        ),
    ),
    "0030_summary_graph_generation.py": (
        "cannot downgrade 0030",
        (
            "where superseded_by is null",
            "group by project_id, target_id, level",
            "having count(*) > 1",
            "choose one current row",
            "supersede the others",
        ),
    ),
    "0031_finding_graph_currentness.py": (
        "cannot downgrade 0031",
        (
            "group by project_id, kind, subject_node_id",
            "having count(*) > 1",
            "export or consolidate rows",
            "at most one",
        ),
    ),
    "0032_plan_revision_provenance.py": (
        "cannot downgrade 0032",
        (
            "from plans",
            "source_run_id is not null",
            "revision-bound plans exist",
            "pre-0032 backup",
        ),
    ),
    "0033_review_revision_provenance.py": (
        "cannot downgrade 0033",
        (
            "from diff_submissions",
            "from diff_break_glass_grants",
            "review_run_id is not null",
            "gate-b review provenance exists",
        ),
    ),
    "0034_data_sample_revision_provenance.py": (
        "cannot downgrade 0034",
        (
            "from data_samples",
            "source_run_id is not null",
            "source_policy_hash is not null",
            "revision-bound data samples exist",
            "restore a pre-0034 backup",
        ),
    ),
    "0039_second_opinion_products.py": (
        "cannot downgrade 0039",
        (
            "from second_opinion_products",
            "canonical second-opinion products exist",
            "safe replay impossible",
        ),
    ),
    "0040_llm_semantic_products.py": (
        "cannot downgrade 0040",
        (
            "from llm_semantic_products",
            "encrypted llm candidates exist",
            "terminal-attempt-without-product gap",
        ),
    ),
}


@pytest.mark.parametrize("filename", _GUARDS)
def test_nonrepresentable_state_guard_runs_before_destructive_ddl(filename: str):
    calls = _record_downgrade(filename)
    names = [name for name, _args, _kwargs in calls]

    assert names[0] == "execute", (
        f"{filename}: compatibility guard must be the first downgrade operation"
    )
    destructive_indexes = [
        index for index, name in enumerate(names) if name in _DESTRUCTIVE_OPS
    ]
    assert destructive_indexes, f"{filename}: expected destructive downgrade DDL"
    assert 0 < min(destructive_indexes)

    sql = " ".join(_guard_sql(filename).lower().split())
    exception_text, required_fragments = _GUARDS[filename]
    assert "do $mnemos$" in sql
    assert "if exists" in sql
    assert "raise exception" in sql
    assert "using hint" in sql
    assert exception_text in sql
    for fragment in required_fragments:
        assert fragment in sql, f"{filename}: guard lost {fragment!r}"


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
async def test_postgres_guards_reject_blockers_in_connection_local_temp_tables():
    """Execute every DO guard without changing the shared application schema."""

    url = _async_postgres_url()
    if url is None:
        pytest.skip("DATABASE_URL is not PostgreSQL")
    pytest.importorskip("asyncpg")

    cases = (
        (
            "0028_graph_fact_overlays.py",
            (
                "CREATE TEMP TABLE graph_node_human_overlays (id integer)",
                "CREATE TEMP TABLE graph_edge_human_overlays (id integer)",
                "CREATE TEMP TABLE graph_edge_runtime_overlays (id integer)",
                "CREATE TEMP TABLE graph_edge_runtime_cursors (id integer)",
            ),
            (
                "INSERT INTO graph_node_human_overlays VALUES (1)",
                "INSERT INTO graph_edge_human_overlays VALUES (1)",
                "INSERT INTO graph_edge_runtime_overlays VALUES (1)",
                "INSERT INTO graph_edge_runtime_cursors VALUES (1)",
            ),
            (
                "graph_edge_runtime_cursors",
                "graph_edge_runtime_overlays",
                "graph_edge_human_overlays",
                "graph_node_human_overlays",
            ),
        ),
        (
            "0029_analysis_run_publication_lifecycle.py",
            ("CREATE TEMP TABLE analysis_runs (status text)",),
            (
                "INSERT INTO analysis_runs VALUES ('published')",
                "INSERT INTO analysis_runs VALUES ('partial')",
            ),
            ("analysis_runs",),
        ),
        (
            "0030_summary_graph_generation.py",
            (
                "CREATE TEMP TABLE summaries ("
                "project_id uuid, target_id text, level integer, "
                "superseded_by uuid)",
            ),
            (
                (
                    "INSERT INTO summaries VALUES "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'sym:x', 1, NULL), "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'sym:x', 1, NULL)"
                ),
            ),
            ("summaries",),
        ),
        (
            "0031_finding_graph_currentness.py",
            (
                "CREATE TEMP TABLE findings ("
                "project_id uuid, kind text, subject_node_id text)",
            ),
            (
                (
                    "INSERT INTO findings VALUES "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'dead_code', NULL), "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'dead_code', NULL)"
                ),
                (
                    "INSERT INTO findings VALUES "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'duplicate_endpoint', 'contract:x'), "
                    "('00000000-0000-0000-0000-000000000001', "
                    "'duplicate_endpoint', 'contract:x')"
                ),
            ),
            ("findings",),
        ),
        (
            "0032_plan_revision_provenance.py",
            (
                "CREATE TEMP TABLE plans ("
                "source_run_id uuid, source_git_sha text, "
                "source_graph_generation bigint, "
                "source_overlay_generation bigint)",
            ),
            (
                (
                    "INSERT INTO plans VALUES ("
                    "'00000000-0000-0000-0000-000000000001', "
                    f"'{'a' * 40}', 1, 0)"
                ),
            ),
            ("plans",),
        ),
        (
            "0033_review_revision_provenance.py",
            (
                "CREATE TEMP TABLE diff_submissions ("
                "review_run_id uuid, review_source_generation bigint, "
                "review_overlay_generation bigint, review_git_sha text)",
                "CREATE TEMP TABLE diff_break_glass_grants ("
                "review_run_id uuid, review_source_generation bigint, "
                "review_overlay_generation bigint, review_git_sha text)",
            ),
            (
                (
                    "INSERT INTO diff_submissions VALUES ("
                    "'00000000-0000-0000-0000-000000000001', "
                    f"1, 0, '{'b' * 40}')"
                ),
                (
                    "INSERT INTO diff_break_glass_grants VALUES ("
                    "'00000000-0000-0000-0000-000000000001', "
                    f"1, 0, '{'c' * 40}')"
                ),
            ),
            ("diff_break_glass_grants", "diff_submissions"),
        ),
        (
            "0034_data_sample_revision_provenance.py",
            (
                "CREATE TEMP TABLE data_samples ("
                "source_run_id uuid, source_git_sha text, "
                "source_graph_generation bigint, "
                "source_overlay_generation bigint, "
                "source_project_db_present boolean, "
                "source_project_db_id uuid, source_policy_hash text)",
            ),
            (
                (
                    "INSERT INTO data_samples VALUES ("
                    "'00000000-0000-0000-0000-000000000001', "
                    f"'{'d' * 40}', 1, 0, true, "
                    "'00000000-0000-0000-0000-000000000002', "
                    f"'{'e' * 64}')"
                ),
            ),
            ("data_samples",),
        ),
        (
            "0039_second_opinion_products.py",
            ("CREATE TEMP TABLE second_opinion_products (id integer)",),
            ("INSERT INTO second_opinion_products VALUES (1)",),
            ("second_opinion_products",),
        ),
        (
            "0040_llm_semantic_products.py",
            ("CREATE TEMP TABLE llm_semantic_products (id integer)",),
            ("INSERT INTO llm_semantic_products VALUES (1)",),
            ("llm_semantic_products",),
        ),
    )

    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                for filename, create_sql, blocker_sqls, drop_tables in cases:
                    for statement in create_sql:
                        await connection.execute(text(statement))

                    guard = text(_guard_sql(filename))
                    # An empty compatibility boundary remains downgradeable.
                    await connection.execute(guard)

                    for blocker_sql in blocker_sqls:
                        savepoint = await connection.begin_nested()
                        try:
                            await connection.execute(text(blocker_sql))
                            with pytest.raises(DBAPIError) as exc_info:
                                await connection.execute(guard)
                            assert _GUARDS[filename][0] in str(
                                exc_info.value
                            ).lower()
                        finally:
                            await savepoint.rollback()

                    for table_name in drop_tables:
                        await connection.execute(text(f"DROP TABLE {table_name}"))
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()
