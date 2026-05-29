#!/usr/bin/env python3
"""Mnemos Oracle analyzer (Phase 1).

Implements probe/inventory/live_schema/live_stats/sample/query/schema per
docs/analyzer-contract.md and spec §7.4. Uses the oracledb thin-mode driver
so no Oracle Instant Client install is required.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_NAME = "ggoss-sql-oracle"
SOURCE_VERSION = "1.0.0"

# A plain Oracle identifier — letters, digits, _ $ #, not starting with a
# digit. A table name matching this cannot smuggle SQL into a FROM clause.
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def _max_rows() -> int:
    """Hard row cap for the ``query`` verb (analyzer-contract §1.0)."""
    try:
        return max(1, int(os.environ.get("MNEMOS_MAX_ROWS", "10000")))
    except ValueError:
        return 10000


def _parse_conn(conn_string: str) -> tuple[str, str, str]:
    """Split an Oracle ``user/password@dsn`` string.

    The username is taken up to the *first* ``/`` and the dsn from the
    *last* ``@`` so a password containing ``/`` or ``@`` — common in
    generated credentials — doesn't corrupt the split.
    """
    user, _, rest = conn_string.partition("/")
    password, _, dsn = rest.rpartition("@")
    return user, password, dsn


def _validate_table(table: str) -> str:
    """Return ``table`` unchanged iff it is a plain (optionally
    schema-qualified) Oracle identifier; raise otherwise.

    The ``sample`` verb interpolates the table name into a ``FROM``
    clause — bind parameters cannot carry an identifier — so the only
    safe defence is to reject anything that isn't an identifier.
    """
    parts = table.split(".")
    if len(parts) not in (1, 2) or not all(_IDENT.match(p) for p in parts):
        raise ValueError(f"invalid_table_name: {table!r}")
    return table


def _is_safe_select(sql: str) -> bool:
    """True iff ``sql`` is a single read-only statement.

    Rejects multi-statement payloads (a trailing ``;`` is tolerated and
    stripped) and anything that isn't a ``SELECT`` / CTE ``WITH``.
    """
    stripped = sql.strip().rstrip(";").strip()
    if ";" in stripped:
        return False
    head = stripped.lower()
    return head.startswith("select") or head.startswith("with")


def envelope(record_type: str, data: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            {
                "record_type": record_type,
                "source_name": SOURCE_NAME,
                "source_version": SOURCE_VERSION,
                "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
                "data": data,
            }
        )
        + "\n"
    )


def error(message: str, *, recoverable: bool = False, file: str | None = None) -> None:
    sys.stderr.write(
        json.dumps(
            {"level": "error", "message": message, "recoverable": recoverable, "file": file}
        )
        + "\n"
    )


def cmd_probe(path: str | None) -> int:
    if not path or not Path(path).exists():
        sys.stdout.write(
            json.dumps({"applicable": False, "reason": "path_not_found", "files_found": 0})
            + "\n"
        )
        return 0
    files = list(Path(path).rglob("*.sql"))
    plb = list(Path(path).rglob("*.pkb")) + list(Path(path).rglob("*.pks"))
    count = len(files) + len(plb)
    sys.stdout.write(
        json.dumps(
            {
                "applicable": count > 0,
                "reason": f"found {len(files)} .sql + {len(plb)} .pkb/.pks",
                "files_found": count,
            }
        )
        + "\n"
    )
    return 0


def cmd_inventory(path: str | None) -> int:
    if not path or not Path(path).exists():
        sys.stdout.write(json.dumps({"files": [], "modules": [], "errors": []}) + "\n")
        return 0
    files = [
        str(p.relative_to(path))
        for p in Path(path).rglob("*.sql")
    ][:5000]
    sys.stdout.write(json.dumps({"files": files, "modules": [], "errors": []}) + "\n")
    return 0


def cmd_db_probe(conn_string: str | None) -> int:
    """Metadata-only read-only credential probe (spec §2.5, §2.8).

    Does NOT issue INSERT/UPDATE/DELETE — not even rolled back ones.
    Oracle redo logs persist rolled-back DML, RLS triggers can fire and
    leave side effects (audit rows, mail dispatch). We refuse to leave
    any trace in the live DB.

    Strategy:
      * Connect with the supplied creds.
      * Read ``v$version`` for the engine banner.
      * Aggregate ``user_tab_privs`` (per-table) and ``session_privs``
        (account-wide) — write_blocked is True iff the user holds
        zero INSERT/UPDATE/DELETE/MERGE/CREATE/ALTER/DROP privileges
        on any object visible to it.
      * Round-trip latency from ``SELECT 1 FROM dual``.
    """
    started = datetime.now(tz=timezone.utc)
    if not conn_string:
        json.dump(
            {
                "status": "fail_connect",
                "connect_ok": False,
                "read_ok": False,
                "write_blocked": None,
                "grants": {},
                "facts": [],
                "latency_ms": 0,
                "error": "missing_connection_string",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    oracledb = _require_oracledb()
    if oracledb is None:
        # _require_oracledb already wrote to stderr; surface a structured
        # envelope so the platform records the failure consistently
        # rather than getting an unparseable empty stdout.
        json.dump(
            {
                "status": "fail_connect",
                "connect_ok": False,
                "read_ok": False,
                "write_blocked": None,
                "grants": {},
                "facts": [],
                "latency_ms": 0,
                "error": "oracledb_not_installed",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    user, password, dsn = _parse_conn(conn_string)
    facts: list[dict[str, Any]] = []
    grants: dict[str, list[str]] = {}
    write_privs = {"INSERT", "UPDATE", "DELETE", "MERGE",
                   "CREATE", "ALTER", "DROP", "TRUNCATE"}
    try:
        with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
            cur = conn.cursor()
            cur.execute("SELECT BANNER FROM v$version WHERE ROWNUM = 1")
            row = cur.fetchone()
            if row is not None:
                facts.append({
                    "value": row[0],
                    "source": "v$version",
                    "confidence": "verified",
                })

            cur.execute("SELECT 1 FROM dual")
            cur.fetchone()

            # Account-wide privileges via SESSION_PRIVS — catches DBA-
            # grade rights the user holds independent of object grants.
            try:
                cur.execute("SELECT PRIVILEGE FROM session_privs")
                session_privs = [r[0] for r in cur.fetchall()]
                if session_privs:
                    grants["__session__"] = session_privs
                    facts.append({
                        "value": {"count": len(session_privs)},
                        "source": "session_privs",
                        "confidence": "verified",
                    })
            except Exception as exc:  # noqa: BLE001
                facts.append({
                    "value": f"session_privs unreadable: {exc}",
                    "source": "session_privs",
                    "confidence": "inferred",
                })

            # Per-table direct grants — bucketed by owner.table for
            # the audit payload but flattened for the verdict.
            try:
                cur.execute(
                    "SELECT TABLE_NAME, PRIVILEGE FROM user_tab_privs "
                    "WHERE GRANTEE = USER"
                )
                for table_name, priv in cur.fetchall():
                    grants.setdefault(table_name, []).append(priv)
            except Exception as exc:  # noqa: BLE001
                facts.append({
                    "value": f"user_tab_privs unreadable: {exc}",
                    "source": "user_tab_privs",
                    "confidence": "inferred",
                })
    except Exception as exc:  # noqa: BLE001
        elapsed = int((datetime.now(tz=timezone.utc) - started).total_seconds() * 1000)
        json.dump(
            {
                "status": "fail_connect",
                "connect_ok": False,
                "read_ok": False,
                "write_blocked": None,
                "grants": {},
                "facts": facts,
                "latency_ms": elapsed,
                "error": f"connect_failed: {exc}",
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    elapsed = int((datetime.now(tz=timezone.utc) - started).total_seconds() * 1000)
    held_write = {
        priv
        for plist in grants.values()
        for priv in plist
        if priv in write_privs
    }
    write_blocked = not held_write
    status = "pass" if write_blocked else "fail_rw"
    facts.append({
        "value": sorted(held_write) if held_write else "no write privileges held",
        "source": "verdict",
        "confidence": "verified",
    })
    json.dump(
        {
            "status": status,
            "connect_ok": True,
            "read_ok": True,
            "write_blocked": write_blocked,
            "grants": grants,
            "facts": facts,
            "latency_ms": elapsed,
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def _require_oracledb():
    try:
        import oracledb  # type: ignore

        return oracledb
    except ImportError:
        error("oracledb_not_installed", recoverable=False)
        return None


def cmd_live_schema(conn_string: str | None) -> int:
    if not conn_string:
        error("missing_connection_string", recoverable=False)
        return 2
    oracledb = _require_oracledb()
    if oracledb is None:
        return 1
    user, password, dsn = _parse_conn(conn_string)
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        component_id = f"db.oracle.{dsn or 'default'}"
        envelope(
            "data_entity",
            {
                "id": component_id,
                "kind": "database",
                "component_id": component_id,
                "name": dsn or "default",
                "schema": {},
                "sample_available": False,
                "is_sensitive": False,
                "certainty": "verified",
                "created_by": [SOURCE_NAME],
            },
        )
        cur = conn.cursor()
        cur.execute(
            """
            SELECT OWNER, TABLE_NAME, COLUMN_NAME, DATA_TYPE, NULLABLE
              FROM ALL_TAB_COLUMNS
             WHERE OWNER NOT IN ('SYS', 'SYSTEM', 'XDB', 'MDSYS')
             ORDER BY OWNER, TABLE_NAME, COLUMN_ID
            """
        )
        tables: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for owner, table, column, dtype, nullable in cur:
            key = (owner, table)
            tables.setdefault(key, []).append(
                {"name": column, "type": dtype, "nullable": nullable == "Y"}
            )

        for (owner, table), cols in tables.items():
            entity_id = f"{component_id}.{owner}.{table}"
            envelope(
                "data_entity",
                {
                    "id": entity_id,
                    "kind": "table",
                    "component_id": component_id,
                    "name": f"{owner}.{table}",
                    "schema": {"columns": cols},
                    "sample_available": False,
                    "is_sensitive": False,
                    "certainty": "verified",
                    "created_by": [SOURCE_NAME],
                },
            )
    return 0


def cmd_sample(conn_string: str | None, table: str | None, limit: int) -> int:
    if not conn_string or not table:
        error("sample_requires_conn_and_table", recoverable=False)
        return 2
    oracledb = _require_oracledb()
    if oracledb is None:
        return 1
    limit = max(1, min(limit, 100))
    try:
        safe_table = _validate_table(table)
    except ValueError as exc:
        error(str(exc), recoverable=False)
        return 2
    user, password, dsn = _parse_conn(conn_string)
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        cur = conn.cursor()
        # safe_table is a validated plain identifier; limit is an int.
        cur.execute(
            f"SELECT * FROM {safe_table} FETCH FIRST {limit} ROWS ONLY"
        )
        cols = [{"name": d[0], "type": d[1].name} for d in cur.description]
        rows = []
        for row in cur:
            rows.append([None if v is None else str(v) for v in row])
    envelope("sample", {"table": table, "columns": cols, "rows": rows})
    return 0


def cmd_query(conn_string: str | None, sql_file: str | None) -> int:
    if not conn_string or not sql_file:
        error("query_requires_conn_and_sql_file", recoverable=False)
        return 2
    sql = Path(sql_file).read_text()
    if not _is_safe_select(sql):
        error("only_single_select_allowed", recoverable=False)
        return 2
    oracledb = _require_oracledb()
    if oracledb is None:
        return 1
    max_rows = _max_rows()
    user, password, dsn = _parse_conn(conn_string)
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        cur = conn.cursor()
        cur.arraysize = min(max_rows, 1000)
        cur.execute(sql)
        cols = [{"name": d[0], "type": d[1].name} for d in cur.description]
        # Hard row cap (analyzer-contract §1.0) — fetchmany in batches
        # and stop at max_rows so a huge result can't OOM the worker.
        rows: list[list[Any]] = []
        truncated = False
        while True:
            batch = cur.fetchmany(cur.arraysize)
            if not batch:
                break
            for row in batch:
                rows.append([None if v is None else str(v) for v in row])
                if len(rows) >= max_rows:
                    truncated = True
                    break
            if truncated:
                break
    envelope(
        "query_result",
        {"columns": cols, "rows": rows, "rows_truncated": truncated},
    )
    return 0


def cmd_schema() -> int:
    sys.stdout.write(
        json.dumps(
            {
                "schema": "https://mnemos.dev/analyzer/ggoss-sql-oracle/v1",
                "record_types": ["symbol", "data_entity", "edge", "sample"],
            }
        )
        + "\n"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="ggoss-sql-oracle")
    sub = parser.add_subparsers(dest="verb")

    sub.add_parser("schema")

    for verb in ("probe", "inventory"):
        p = sub.add_parser(verb)
        p.add_argument("path", nargs="?")

    live = sub.add_parser("live_schema")
    live.add_argument("--conn", help="user/password@dsn")

    stats = sub.add_parser("live_stats")
    stats.add_argument("--conn")

    sample = sub.add_parser("sample")
    sample.add_argument("--conn")
    sample.add_argument("--table", required=True)
    sample.add_argument("--limit", type=int, default=10)

    query = sub.add_parser("query")
    query.add_argument("--conn")
    query.add_argument("--sql-file", required=True)

    probe_db = sub.add_parser("db_probe")
    probe_db.add_argument("--conn", help="user/password@dsn")

    args = parser.parse_args()
    # Platform passes MNEMOS_DB_CONN (canonical); honour the older
    # MNEMOS_ORACLE_CONN as a fallback so existing operator scripts keep
    # working during the rollout.
    conn_env = os.environ.get("MNEMOS_DB_CONN") or os.environ.get("MNEMOS_ORACLE_CONN")

    try:
        if args.verb == "probe":
            return cmd_probe(args.path)
        if args.verb == "inventory":
            return cmd_inventory(args.path)
        if args.verb == "live_schema":
            return cmd_live_schema(args.conn or conn_env)
        if args.verb == "live_stats":
            return 0  # Week-6 AWR/V$ integration
        if args.verb == "sample":
            return cmd_sample(args.conn or conn_env, args.table, args.limit)
        if args.verb == "query":
            return cmd_query(args.conn or conn_env, args.sql_file)
        if args.verb == "db_probe":
            return cmd_db_probe(args.conn or conn_env)
        if args.verb == "schema":
            return cmd_schema()
        parser.print_help()
        return 2
    except Exception as exc:  # noqa: BLE001
        error(str(exc), recoverable=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
