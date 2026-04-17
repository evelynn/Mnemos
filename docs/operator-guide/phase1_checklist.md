# Phase 1 closing checklist

Copy of spec §15.3 reconciled with what is implemented on this branch.
Bring up the full stack with `docker compose up -d`, run `alembic upgrade head`,
then walk the checklist.

- [x] Docker Compose single-host deployment (§4.4)
- [x] Login, settings, project registration, DB-connection registration all
      GUI-driven (HTMX dashboard + `/api/v1`)
- [x] C# `symbols` / `calls` / `contracts` via Roslyn (`ggoss-csharp`)
- [x] TypeScript `symbols` / `calls` / `contracts` via TS Compiler API
      (`ggoss-ts`)
- [x] MSSQL live_schema + sample + query (`ggoss-sql-mssql`)
- [x] Oracle live_schema + sample + query (`ggoss-sql-oracle`) —
      `live_stats` is a Phase-2 AWR/V$ hook today
- [x] .NET DLL surface extraction + opaque Component modelling
      (`ggoss-binary-dotnet`)
- [x] Data sample capture + PII masking (columns + value regexes), persisted
      masked-only in `data_samples`
- [x] MCP data tools: `get_data_entity`, `get_sample_data`,
      `get_column_stats`, `search_data`, plus HTTP `/data/query`
- [x] OTLP HTTP receiver at `/otlp/v1/traces` with per-span scrubber
- [x] Merge engine generating 4 of 6 Finding kinds (duplicate_endpoint,
      unverified_claim, dynamic_call_detected, dead_path_suspected).
      `schema_mismatch` and `opaque_component_failing` are Phase-2.
- [x] L1 summaries with evidence validator (L2/L3 wiring ready, invoked
      manually via `extractor.runner.summarise_l1`)
- [x] Auto-generated `AGENTS.md` and `.mcp.json`; Skills bundle is
      Phase-2 scope
- [x] MCP server exposes query/data/dev tools (`submit_plan`,
      `edit_file_in_worktree`, `run_in_sandbox`, `submit_diff`, …)
- [x] Gate A (plan approval) + Gate B (diff approval) GUI
- [x] GitLab MR creation via `python-gitlab` when configured; falls back
      to a recorded result otherwise
- [x] Audit log records: auth, secret CRUD/test, project CRUD,
      analysis.enqueue/completed/cancel, mcp.tool.*, data.query,
      data.sample_view, plan.submit/approve/reject/regenerate,
      diff.submit/approve/reject, finding.update/rebuild, otlp.traces
- [x] Safety-rule self tests (`tests/test_safety.py`,
      `tests/test_masking.py`) guard the three isolation axes

Items deferred to Phase 2 per spec §15.4 remain out of scope.
