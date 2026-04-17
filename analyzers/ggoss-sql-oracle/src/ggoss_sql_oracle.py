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
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SOURCE_NAME = "ggoss-sql-oracle"
SOURCE_VERSION = "1.0.0"


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
    user, _, rest = conn_string.partition("/")
    password, _, dsn = rest.partition("@")
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
    user, _, rest = conn_string.partition("/")
    password, _, dsn = rest.partition("@")
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM {table} FETCH FIRST {limit} ROWS ONLY")
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
    if not sql.lstrip().lower().startswith("select"):
        error("only_select_allowed", recoverable=False)
        return 2
    oracledb = _require_oracledb()
    if oracledb is None:
        return 1
    user, _, rest = conn_string.partition("/")
    password, _, dsn = rest.partition("@")
    with oracledb.connect(user=user, password=password, dsn=dsn) as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [{"name": d[0], "type": d[1].name} for d in cur.description]
        rows = [[None if v is None else str(v) for v in row] for row in cur]
    envelope("query_result", {"columns": cols, "rows": rows})
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

    args = parser.parse_args()
    conn_env = os.environ.get("MNEMOS_ORACLE_CONN")

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
        if args.verb == "schema":
            return cmd_schema()
        parser.print_help()
        return 2
    except Exception as exc:  # noqa: BLE001
        error(str(exc), recoverable=False)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
