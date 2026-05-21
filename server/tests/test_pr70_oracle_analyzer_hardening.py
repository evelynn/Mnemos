"""PR-70 — Oracle analyzer hardening (security: §2.5 / §2.8).

A critical review of the four non-TypeScript analyzers found the
Oracle analyzer (analyzers/ggoss-sql-oracle/src/ggoss_sql_oracle.py)
had three real defects:

* the ``query`` verb materialised the whole result set — no
  ``MNEMOS_MAX_ROWS`` cap, so a large table OOMs the worker
  (analyzer-contract §1.0);
* ``sample`` interpolated the raw ``--table`` argument into a FROM
  clause and ``query`` only checked the SQL *starts with* ``select``
  — both SQL-injectable;
* the connection-string parser broke on a ``/`` or ``@`` in the
  password, and ``db_probe`` ignored ``--conn``.

This module unit-tests the pure helpers that fix them. The verbs
themselves need a live Oracle DB, so the CLI-level behaviour is left
to the containerised CI suite — but the validation logic, which is
where the security depends, is verified here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_ANALYZER = (
    Path(__file__).resolve().parents[2]
    / "analyzers" / "ggoss-sql-oracle" / "src" / "ggoss_sql_oracle.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("ggoss_ora", _ANALYZER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_ORA = _load()


# ---------------------------------------------------------------------------
# _parse_conn — robust against / and @ in the password
# ---------------------------------------------------------------------------


def test_parse_conn_plain():
    assert _ORA._parse_conn("scott/tiger@db1") == ("scott", "tiger", "db1")


def test_parse_conn_password_with_at_and_slash():
    # The username ends at the first '/', the dsn starts at the last '@'.
    assert _ORA._parse_conn("u/pa/ss@dsn") == ("u", "pa/ss", "dsn")
    assert _ORA._parse_conn("u/p@ss@host:1521/svc") == (
        "u", "p@ss", "host:1521/svc",
    )


# ---------------------------------------------------------------------------
# _validate_table — the sample-verb injection defence
# ---------------------------------------------------------------------------


def test_validate_table_accepts_identifiers():
    assert _ORA._validate_table("ORDERS") == "ORDERS"
    assert _ORA._validate_table("HR.EMPLOYEES") == "HR.EMPLOYEES"


@pytest.mark.parametrize(
    "bad",
    [
        "orders; DROP TABLE secrets--",
        "orders WHERE 1=1",
        "a b",
        "1orders",
        "orders--",
        "a.b.c",
        "",
        "orders) UNION SELECT * FROM dba_users--",
    ],
)
def test_validate_table_rejects_injection(bad):
    with pytest.raises(ValueError):
        _ORA._validate_table(bad)


# ---------------------------------------------------------------------------
# _is_safe_select — single read-only statement only
# ---------------------------------------------------------------------------


def test_is_safe_select_accepts_select_and_cte():
    assert _ORA._is_safe_select("SELECT * FROM t")
    assert _ORA._is_safe_select("  with x as (select 1) select * from x ")
    assert _ORA._is_safe_select("select 1;")  # trailing ; tolerated


@pytest.mark.parametrize(
    "bad",
    [
        "SELECT 1; DELETE FROM orders",
        "DELETE FROM orders",
        "update orders set x = 1",
        "INSERT INTO orders VALUES (1)",
        "select 1; select 2",
    ],
)
def test_is_safe_select_rejects_writes_and_multistatement(bad):
    assert not _ORA._is_safe_select(bad)


# ---------------------------------------------------------------------------
# _max_rows — the query-verb hard cap
# ---------------------------------------------------------------------------


def test_max_rows_default(monkeypatch):
    monkeypatch.delenv("MNEMOS_MAX_ROWS", raising=False)
    assert _ORA._max_rows() == 10000


def test_max_rows_from_env(monkeypatch):
    monkeypatch.setenv("MNEMOS_MAX_ROWS", "500")
    assert _ORA._max_rows() == 500
    monkeypatch.setenv("MNEMOS_MAX_ROWS", "garbage")
    assert _ORA._max_rows() == 10000


# ---------------------------------------------------------------------------
# Source-level — the verbs actually use the helpers
# ---------------------------------------------------------------------------


def test_query_verb_caps_rows():
    body = _ANALYZER.read_text(encoding="utf-8")
    idx = body.find("def cmd_query(")
    slab = body[idx:idx + 1700]
    assert "_is_safe_select(sql)" in slab
    assert "fetchmany(" in slab
    assert "rows_truncated" in slab
    assert "_max_rows()" in slab


def test_sample_verb_validates_table():
    body = _ANALYZER.read_text(encoding="utf-8")
    idx = body.find("def cmd_sample(")
    slab = body[idx:idx + 900]
    assert "_validate_table(table)" in slab


def test_db_probe_accepts_conn_flag():
    body = _ANALYZER.read_text(encoding="utf-8")
    assert 'probe_db = sub.add_parser("db_probe")' in body
    assert 'probe_db.add_argument("--conn"' in body
    assert "cmd_db_probe(args.conn or conn_env)" in body
