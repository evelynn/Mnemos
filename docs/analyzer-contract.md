# Analyzer plugin contract (Phase 1)

This document is the binding interface between the platform and any
language/database analyzer. It mirrors `Mnemos_spec.md` §6.

## 1. CLI surface

Every analyzer MUST expose a single command-line entry point named
`ggoss-<name>` (binary or shell script) that supports the following verbs:

| Verb | Purpose | Notes |
|---|---|---|
| `probe <path>` | Detect whether the analyzer applies to the given path. | Output: a single JSON object on stdout: `{"applicable": bool, "reason": string, "files_found": int}`. Exit code 0 in both cases (applicability is data, not failure). |
| `inventory <path>` | Enumerate analyzable inputs without producing graph records. | Output: one JSON object on stdout summarising files/modules/errors. |
| `symbols <path> [--output <file>]` | Stream `record_type=symbol` records. | JSON Lines on stdout (or `--output` file). |
| `calls <path> [--output <file>]` | Stream `record_type=edge,kind=CALLS` records. | JSON Lines. |
| `contracts <path> [--output <file>]` | Stream `record_type=contract` and the EXPOSES edges that connect them. | JSON Lines. |
| `data_access <path> [--output <file>]` | Stream READS/WRITES edges and the `record_type=data_entity` nodes they reference. Source-only analyzers emit name-keyed logical entities (`data.<table>`); the merge layer reconciles these against the schema-qualified entities the DB analyzers emit. | JSON Lines. |
| `schema` | Print the JSON Schema for this analyzer's `data` payloads. | One JSON document on stdout. |

DB analyzers (`ggoss-sql-mssql`, `ggoss-sql-oracle`) extend the surface with
`live_schema`, `live_stats`, `sample`, `query` (see §6.3 of the spec) and
the `db_probe` verb described below.

### 1.0 `query` verb hard row cap

The `query` verb MUST honour the `MNEMOS_MAX_ROWS` environment variable
(default 10 000) by setting the driver-level fetch limit before
materialising results:

* MSSQL: `cursor.arraysize = max_rows` and stop after `max_rows`
  `fetchmany` rounds. Do not rely on `TOP n` rewriting — the platform
  already applied LIMIT/TOP at the AST level (`safety/sql_limit.py`),
  the driver cap is a second line of defence.
* Oracle: `cursor.arraysize = max_rows`, `cursor.prefetchrows =
  max_rows`. Same fetchmany discipline.
* Excess rows are dropped silently; the analyzer reports the
  `rows_truncated: bool` flag in its JSON envelope so the platform can
  surface a "results truncated" indicator in the UI.

### 1.1 `db_probe` verb (DB analyzers only)

Confirms a credential is read-only before the platform persists a
ProjectDB binding (spec §2.5, §2.8). Reads `MNEMOS_DB_CONN` from the
environment, queries metadata only (`HAS_TABLE_PRIVILEGE`,
`user_tab_privs`, `fn_my_permissions`, equivalent), and prints **one**
JSON object on stdout:

```json
{
  "status": "pass" | "fail_rw" | "fail_connect",
  "connect_ok": true,
  "read_ok": true,
  "write_blocked": true,
  "grants": {"public": ["SELECT"]},
  "facts": [
    {"value": "INSERT not granted on any schema",
     "source": "pg_has_table_privilege",
     "confidence": "verified"}
  ],
  "latency_ms": 84
}
```

* Exit code 0 — payload is the verdict.
* Exit code 2 — analyzer does not implement the verb (older builds);
  platform records the binding with `status: deferred`.
* The analyzer MUST NOT execute `INSERT` / `UPDATE` / `DELETE` /
  `MERGE` / `CREATE` / DDL against the live DB during the probe. Even
  rolled-back attempts leave traces in redo / transaction logs.
* `confidence` values are exactly the §2.3 set:
  `verified` (the DB itself told us) /
  `asserted` (one trustworthy source) /
  `inferred` (LLM or heuristic).

## 2. Output record envelope

Every record emitted to stdout (or `--output`) is one JSON object per line:

```json
{
  "record_type": "symbol" | "contract" | "data_entity" | "edge" | "sample",
  "source_name": "ggoss-csharp",
  "source_version": "1.0.0",
  "analyzed_at": "2026-04-17T10:00:00Z",
  "data": { /* §5.1 / §5.2 schema */ }
}
```

`source_name` MUST be unique per analyzer + variant. The platform uses it to
populate `created_by` arrays on nodes/edges.

## 3. Error reporting

- Recoverable errors are written to **stderr** as JSON Lines, one per problem:
  ```json
  {"level":"error","file":"Order.cs","message":"...","recoverable":true}
  ```
- The analyzer MUST continue past recoverable failures.
- Only `recoverable: false` (or a crash) yields a non-zero exit code.

## 4. Performance budget

- 100k LOC: `symbols` ≤ 10 minutes
- 300k LOC: `symbols` ≤ 30 minutes
- Resident memory ≤ 4 GB

## 5. Container conventions

- Image name: `mnemos/<analyzer-name>:<version>` (locally built; no registry
  required for Phase 1).
- Working directory inside the container: `/work` — the platform mounts the
  target source tree at this path read-only.
- Output files (when `--output` is used) go to `/work/.mnemos/out/`, which the
  platform creates writable.
- Long-lived state files MUST live under `/work/.mnemos/` so cleanup is one
  `rm -rf`.
- `STDIN` is unused.

## 6. Versioning

- The CLI surface itself is versioned through the `schema` command's output
  (`$schema` field). Breaking changes require a major bump.
- Minor versions add optional fields, never remove them.
