"""SQLGlot-backed LIMIT enforcement + write-block (spec §2.5, §2.8)."""

from __future__ import annotations

import pytest

from app.safety.sql_limit import (
    DEFAULT_MAX_ROWS,
    WriteStatementBlocked,
    enforce_limit,
)


# ---------------------------------------------------------------------------
# Row-cap behaviour
# ---------------------------------------------------------------------------


def test_adds_limit_when_absent_postgres():
    r = enforce_limit("SELECT id FROM orders", dialect="postgres", max_rows=100)
    assert r.clamp == "added"
    assert r.effective_limit == 100
    assert "limit" in r.sql.lower()


def test_keeps_small_limit():
    r = enforce_limit("SELECT id FROM orders LIMIT 50", dialect="postgres", max_rows=100)
    assert r.clamp == "none"
    assert r.effective_limit == 50
    assert r.original_limit == 50


def test_clamps_oversized_limit():
    r = enforce_limit(
        "SELECT id FROM orders LIMIT 999999", dialect="postgres", max_rows=100
    )
    assert r.clamp == "clamped"
    assert r.effective_limit == 100
    assert r.original_limit == 999999


def test_adds_limit_for_mssql_top_syntax():
    """MSSQL operators commonly use TOP. The platform translates to LIMIT
    internally via the AST so the analyzer's driver layer sees a uniform
    representation; the rewritten SQL is whatever sqlglot emits for the
    target dialect."""
    r = enforce_limit("SELECT id FROM orders", dialect="mssql", max_rows=50)
    assert r.clamp == "added"
    assert r.effective_limit == 50
    # sqlglot's TSQL output uses TOP or FETCH; either is acceptable.
    lower = r.sql.lower()
    assert "top" in lower or "fetch" in lower


def test_adds_limit_for_oracle_fetch_first():
    r = enforce_limit("SELECT * FROM dual", dialect="oracle", max_rows=10)
    assert r.clamp == "added"
    assert r.effective_limit == 10


def test_default_cap_when_caller_omits_max_rows():
    r = enforce_limit("SELECT 1", dialect="postgres")
    assert r.effective_limit == DEFAULT_MAX_ROWS


def test_invalid_max_rows_rejected():
    with pytest.raises(ValueError):
        enforce_limit("SELECT 1", dialect="postgres", max_rows=0)


# ---------------------------------------------------------------------------
# Write-statement block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders (id) VALUES (1)",
        "UPDATE orders SET id = 1",
        "DELETE FROM orders",
        "MERGE INTO orders USING staging ON 1=1 WHEN MATCHED THEN DELETE",
        "DROP TABLE orders",
        "ALTER TABLE orders ADD COLUMN x int",
        "CREATE TABLE foo (id int)",
    ],
)
def test_write_statements_are_blocked(sql):
    with pytest.raises(WriteStatementBlocked):
        enforce_limit(sql, dialect="postgres", max_rows=100)


def test_select_shell_over_update_is_blocked():
    """The historical regex check let `WITH cte AS (UPDATE ...) SELECT * FROM cte`
    through because the top token was SELECT. The AST walker must catch the
    nested write statement."""
    sql = (
        "WITH bad AS (UPDATE orders SET id = id + 1 RETURNING id) "
        "SELECT * FROM bad"
    )
    with pytest.raises(WriteStatementBlocked):
        enforce_limit(sql, dialect="postgres", max_rows=100)


# ---------------------------------------------------------------------------
# Parse-error surface
# ---------------------------------------------------------------------------


def test_parse_error_surfaces_to_caller():
    """A malformed statement must raise — caller maps to HTTP 400."""
    import sqlglot.errors

    with pytest.raises((sqlglot.errors.ParseError, ValueError)):
        enforce_limit("SELEKT *", dialect="postgres", max_rows=100)
