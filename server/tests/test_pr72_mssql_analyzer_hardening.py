"""PR-72 — MSSQL analyzer hardening (security: §2.5 / §2.8).

The non-TypeScript-analyzer review found the MSSQL analyzer
(analyzers/ggoss-sql-mssql/src/Program.cs) carried the same two
critical defects PR-70 fixed in the Oracle analyzer:

* the ``query`` verb read the whole result into memory with no
  ``MNEMOS_MAX_ROWS`` cap (analyzer-contract §1.0);
* ``sample`` interpolated the raw ``--table`` argument into a
  ``FROM`` clause and ``query`` only checked the SQL *started with*
  ``select`` — both SQL-injectable.

These are C# source assertions: the analyzer compiles only with the
.NET SDK (CI), so this module is a regression guard that the guard
clauses stay present — it does not exercise the compiled binary.
"""

from __future__ import annotations

from pathlib import Path

_MSSQL = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-sql-mssql" / "src" / "Program.cs"
)


def _read() -> str:
    return _MSSQL.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Injection defences
# ---------------------------------------------------------------------------


def test_table_name_validator_present():
    body = _read()
    assert "static bool IsValidTableName(" in body
    # An identifier-only allowlist regex.
    assert "[A-Za-z_][A-Za-z0-9_]*" in body


def test_sample_validates_table_before_use():
    body = _read()
    idx = body.find("static async Task<int> SampleAsync(")
    slab = body[idx:idx + 900]
    assert "IsValidTableName(table)" in slab
    assert '"invalid_table_name"' in slab


def test_query_rejects_non_select_and_multistatement():
    body = _read()
    idx = body.find("static async Task<int> QueryAsync(")
    slab = body[idx:idx + 2100]
    # SELECT / CTE WITH only.
    assert '"select"' in slab and '"with"' in slab
    # Embedded ; (a second statement) is rejected.
    assert "Contains(';')" in slab
    assert '"multi_statement_not_allowed"' in slab


# ---------------------------------------------------------------------------
# Row cap
# ---------------------------------------------------------------------------


def test_query_verb_caps_rows():
    body = _read()
    assert "static int MaxRows()" in body
    assert "MNEMOS_MAX_ROWS" in body
    idx = body.find("static async Task<int> QueryAsync(")
    slab = body[idx:idx + 2100]
    assert "MaxRows()" in slab
    assert "rows.Count >= maxRows" in slab
    assert "rows_truncated = truncated" in slab
