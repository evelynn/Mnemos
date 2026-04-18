# Mnemos — Delivered Architecture

This document describes **what is actually implemented** on the current
branch. For the original design intent see `Mnemos_spec.md`; for operator
workflows see `operator-guide/deployment.md`. Every subsystem here is cross-
referenced to the code path that implements it so a reviewer can read this
front to back without the codebase open.

Conventions: `server/` is the FastAPI platform; `analyzers/` are per-language
extractors; `app.` refers to the Python package under `server/app/`.

## 1. Top-level process model

Four long-running processes form the default deployment:

| Container | Entrypoint | Purpose |
|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | primary store (knowledge graph, secrets, audit, users, findings) |
| `redis`    | `redis:7-alpine` | ARQ queue, session cookies, rate-limit sliding window, worker heartbeat |
| `platform` | `uvicorn app.main:app` | REST + MCP + dashboard HTTP surface |
| `worker`   | `python -m app.worker` | ARQ worker running analysis jobs + writing heartbeat |

Optional sidecars (Prometheus, Grafana, Vault) are documented in
`operator-guide/deployment.md` §8, §13.

The platform and the worker share the same image (`server/Dockerfile`) and
configuration, but route entry points are different: `create_app()` in
`app/main.py` for the API, `main()` in `app/worker.py` for the ARQ worker.

## 2. Request / job lifecycle

### 2.1 HTTP request path (platform container)

```
browser / CLI
   ↓
nginx / caddy (TLS termination, operator-provided)
   ↓
uvicorn → RequestContextMiddleware        (app/obs/middleware.py)
   ↓   sets x-request-id, logs on exit
        AuditMiddleware                    (app/audit/middleware.py)
   ↓   writes audit row for mutations
        PrometheusMiddleware               (app/obs/metrics.py)
   ↓   HTTP counter + latency histogram
        FastAPI dependency graph
          current_user()                   (app/auth/deps.py)      — session cookie → User
          require_{admin,operator}()       (app/auth/rbac.py)      — ordered role check
          require_project_org()            (app/auth/org_scope.py) — tenancy boundary (404 on mismatch)
          rl_enforce()                     (app/safety/ratelimit.py) — Redis sliding window
   ↓
        route handler → response
   ↓
error handlers                             (app/obs/errors.py)
   ↓   uniform body: {status, detail, request_id}
nginx → browser (x-request-id echoed back)
```

Every response carries the correlation id via the `x-request-id` header and
every log line includes it via the `request_id_ctx` ContextVar, so an
operator can grep a single id across caller, platform, worker, and audit.

### 2.2 Analysis job path (worker container)

```
POST /api/v1/projects/{id}/analyze           (app/api/analysis.py)
  ↓ persists AnalysisRun (status=queued)
  ↓ enqueues "run_ingest" via ARQ
worker picks up run_ingest                   (app/orchestrator/jobs.py)
  ↓ status=running, ProgressBus bound
  for language in project.languages:
    for verb in (symbols, contracts, calls, data_access):
      StageTracker { subprocess analyzer }   (app/analyzers/runner.py)
        ↓ JSON lines → _record_payload → upsert_node / upsert_edge
                                         (app/merge/writer.py)
  for pdb in ProjectDBs:                    ← Phase D: live DB schema stage
    maintenance_window gate                  (app/data_sampler/maintenance.py)
    decrypt secret                           (app/safety/crypto.py → kms.py)
    subprocess analyzer(live_schema) with MNEMOS_DB_CONN env
      ↓ JSON lines → upsert DataEntity nodes
  rebuild_findings                           (app/merge/findings.py)
    → 6 detectors run in order
  summarise_l1 / l2 / l3                     (app/extractor/runner.py)
    → Anthropic Claude messages.create() per leaf / file / module
    → hash-stamped Summary rows persisted
  status=completed, analysis_runs_total.inc (app/obs/metrics.py)
```

Each stage is wrapped in `StageTracker` (`app/orchestrator/stages.py`) which
persists progress to `analysis_stages` and emits SSE events on the
`ProgressBus` so the Analysis tab can render a live pipeline view.

### 2.3 Plan / diff / MR path

```
Dev agent → POST /api/v1/projects/{id}/plans
  ↓ impact_analysis() run + worktree created
  ↓ Plan row {status=draft}
Human reviewer → POST /api/v1/plans/{id}/decide (approve / reject)
  ↓ audit record
Dev agent → MCP edit_file_in_worktree / run_in_sandbox
Dev agent → POST /api/v1/diff_submissions
  ↓ run_pipeline: 6-pass ultrareview         (app/safety/review/pipeline.py)
      rules → contracts → data_access → impact → second_opinion → validator
  ↓ DiffSubmission {status=pending_approval | blocked}
Operator → POST /api/v1/diff_submissions/{id}/approve
  ↓ (block verdict requires explicit override + ≥20-char rationale, audited)
  ↓ create_mr_from_worktree                  (app/gitlab_client/mr.py)
  ↓ status=approved, gitlab_mr_url recorded
```

## 3. Data model

All tables live in one Postgres schema. Migrations are numbered `0001` →
`0011`; each is an ordinary Alembic upgrade/downgrade pair (the perf-indexes
migration uses `CONCURRENTLY` outside the transaction).

| Domain | Tables | Lineage |
|---|---|---|
| Identity | `users`, `organizations`, `api_keys`, `platform_settings` | 0001, 0002, 0010 |
| Secrets | `secrets` | 0001 |
| Projects | `projects`, `project_dbs` | 0002, 0009 |
| Audit | `audit_logs` | 0003 |
| Knowledge graph | `nodes`, `edges`, `node_sources` | 0004 |
| Samples | `data_samples`, `data_query_log` | 0005 |
| Findings & summaries | `findings`, `summaries` | 0006 |
| Plans | `plans`, `diff_submissions` | 0007 |
| Stages | `analysis_runs`, `analysis_stages` | 0002, 0008 |
| Perf indexes | (CONCURRENTLY on existing tables) | 0011 |

### 3.1 Bitemporal graph

`nodes` and `edges` keep history via `valid_from` / `valid_to`. Only the row
with `valid_to IS NULL` is "current". Upsert semantics (`app/merge/writer.py`):

1. Set `valid_to = now()` on the row where `(project_id, id, valid_to IS NULL)`.
2. Insert a new row with `valid_from = now(), valid_to = NULL`.
3. Append to `node_sources` so multi-source reconciliation is traceable.

Index `idx_nodes_current` is a partial index on `(project_id, id) WHERE
valid_to IS NULL`, so "current" reads stay cheap regardless of history size.

### 3.2 Multi-tenancy

`organizations` (migration `0010`) is joined to both `users` and `projects`
via nullable FKs. NULL is treated as "pre-migration / single-tenant" and
remains visible to every user — so single-org deployments keep working.
`app.auth.org_scope.same_org()` returns True when either side is None.

Enforcement happens in two places:

- **Router-level** `Depends(require_project_org())` on every router whose
  prefix contains `{project_id}` (project_dbs, artifacts, data_entities,
  data/query).
- **Per-endpoint** on routers mixing project-scoped and global routes
  (analysis, findings, plans, projects, diffs).

`/api/v1/projects` **list** and `/api/v1/audit` **list** both filter by the
caller's `organization_id`. Cross-org attempts return `404 not_found` (not
403) so project existence does not leak across tenants.

## 4. Security subsystems

### 4.1 Authentication & sessions

| Concern | Implementation | Location |
|---|---|---|
| Password hashing | bcrypt via passlib | `app/auth/passwords.py` |
| Session token | random 256-bit id, Redis-backed, 7-day default TTL | `app/auth/sessions.py` |
| Cookie | `HttpOnly; SameSite=Lax; Secure=${SESSION_COOKIE_SECURE}` | `app/api/auth.py`, `dashboard/router.py`, `auth/oidc.py` |
| Role check | ordered enum `viewer < operator < admin` | `app/auth/rbac.py` |
| OIDC | PKCE + state via Redis; id_token JWKS signature verified via PyJWT before claims trusted | `app/auth/oidc.py` |

### 4.2 KMS

Pluggable backend (`app/safety/kms.py`):

- `LocalFernetKms` (default) — DEK from `FERNET_KEY` env. Refuses to boot
  with a `SECRET_KEY`-derived DEK when `MNEMOS_ENV=production`.
- `VaultKmsBackend` — fetches the DEK from HashiCorp Vault KV-v2 at startup
  via HTTP API. Token renewal is operator-sidecar territory.

Selected via `KMS_BACKEND={local,vault}`. `app/safety/crypto.py` is a thin
facade over whichever backend `get_kms()` returns.

### 4.3 Data-path policy

Every `/data/query` call runs through `enforce_policy` in
`app/data_sampler/project_db.py`:

1. `sensitive_tables_hit` — case-insensitive token scan over the SQL text.
2. AWR consent — `requires_awr(sql)` checks for `DBA_HIST_*`, `V$`, `GV$`,
   literal `AWR`; blocks unless `pdb.allow_awr` is true.
3. `is_within_windows` — mini-cron evaluator against
   `pdb.maintenance_windows`; blocks with HTTP 423 when outside.

Masking engine (`app/data_sampler/masking.py`) then applies per-DB
`masking_rules` (a JSON of extra full/partial-mask regexes) on top of the
built-in PII defaults (password/token/ssn/email/phone/RRN/card/IP).

### 4.4 Rate limiting

Redis-backed sliding window in `app/safety/ratelimit.py`. Keys are
`rl:<scope>:u/<user_id>` or `rl:<scope>:ip/<client>`. Rejections increment
`mnemos_rate_limited_total{scope}` and write an audit entry
(`action=rate_limit.blocked`). Defaults: `data.query` 30/60s, `data.sample`
20/60s.

## 5. Analysis subsystems

### 5.1 Analyzer runner

`app/analyzers/runner.py` spawns a subprocess per verb and streams JSON-
Lines from stdout. The runner only logs `binary verb path` — never
`extra_args` or `env` — because callers may pass credentials there
(DB `live_schema` stage does, via `MNEMOS_DB_CONN`).

### 5.2 Findings (6/6 spec taxonomy)

Implemented in `app/merge/findings.py`:

| Finding kind | Detector | Trigger |
|---|---|---|
| `duplicate_endpoint` | `detect_duplicate_endpoints` | multiple components expose the same contract |
| `unverified_claim` | `detect_unverified_claims` | inferred edge unchanged for 30d |
| `dynamic_call_detected` | `detect_dynamic_calls` | runtime edge seen with no static counterpart |
| `dead_path_suspected` | `detect_dead_paths` | static edge never exercised in 30d |
| `schema_mismatch` | `detect_schema_mismatches` | READS/WRITES target missing from live DataEntity set |
| `opaque_component_failing` | `detect_opaque_failing_components` | opaque component with `errors/calls ≥ 0.1` |

`_upsert_finding` is idempotent on `(project_id, kind, subject_node_id)` so
successive runs update `last_seen_at` rather than duplicate.

### 5.3 LLM summarisation

`app/extractor/agent.py` wraps the Anthropic SDK; the runner
(`app/extractor/runner.py`) packs evidence, hash-stamps it into Summary
rows, and skips re-summarisation when the hash matches. L1 fires per
function, L2 per file, L3 per module — each level feeds the next via
`pack_by_budget()`. When `ANTHROPIC_API_KEY` is unset the agent returns a
deterministic stub so the pipeline stays testable offline.

### 5.4 MCP server

`app/mcp/server.py` exposes 18 tools via STDIO. Categories:

- **Graph queries** — `search_symbols`, `get_symbol`, `find_callers`,
  `find_callees`, `impact_analysis`, `get_contract`, `find_runtime_path`.
- **Summaries & findings** — `get_module_summary`, `list_findings`.
- **Data** — `get_data_entity`, `get_sample_data`, `get_column_stats`,
  `search_data`.
- **Files** — `read_file` (windowed, max 2000 lines per call).
- **Dev** — `submit_plan`, `edit_file_in_worktree`, `run_in_sandbox`,
  `submit_diff`.

Every tool call emits an audit row. `read_file` and `get_sample_data` share
the same masking / sensitivity gates as the REST paths.

### 5.5 Ultrareview

`app/safety/review/pipeline.py` runs six reviewers in order on every
submitted diff:

1. `rules` — lint-ish patterns (banned APIs, path allowlists).
2. `contracts` — the change doesn't break existing node/edge contracts.
3. `data_access` — new SQL respects `sensitive_tables` + masking policy.
4. `impact` — which downstream components, graph-wise, are affected.
5. `second_opinion` — independent LLM re-review of the patch.
6. `validator` — aggregate verdict: `clean | warn | blocked`.

A `blocked` verdict refuses approval until an operator files an override
with a ≥20-char rationale; the override itself is a distinct
`diff.override` audit row.

## 6. Observability

### 6.1 Logging

`app/obs/logging.py` installs a JSON formatter on the root logger. Every
line carries `{ts, level, logger, msg, request_id, ...extras}`. The
`request_id` is propagated via a ContextVar set by `RequestContextMiddleware`
so background tasks inherit it.

### 6.2 Metrics

`app/obs/metrics.py` defines four counters/histograms:

- `mnemos_http_requests_total{method,path,status_bucket}`
- `mnemos_http_request_duration_seconds{method,path}`
- `mnemos_analysis_runs_total{status}`
- `mnemos_rate_limited_total{scope}`

Scraped by Prometheus at `/metrics`. Authentication is optional via
`METRICS_BEARER_TOKEN`.

### 6.3 Health

- `/api/v1/health` — liveness (always 200 while uvicorn is up).
- `/api/v1/health/ready` — deep readiness. Checks Postgres (`SELECT 1`,
  2s timeout), Redis (`PING`, 2s timeout), worker heartbeat (Redis key
  `mnemos:worker:heartbeat`, stale after 90s). Returns 503 on any failure
  with a per-component breakdown.

The worker writes its heartbeat every 15s from an `asyncio.create_task`
started in `_startup` (`app/orchestrator/jobs.py`).

## 7. Dashboard

Jinja + HTMX + vanilla JS. 13 tabs, each a real working surface:

| Tab | Path | Role gate | Fetches |
|---|---|---|---|
| dashboard | `/` | any | projects count, 7d runs, open findings, readiness |
| projects | `/projects` | any | `/api/v1/projects` (org-scoped) + CRUD |
| analysis | `/analysis` | any | trigger run + SSE stage stream |
| data | `/data` | any | `/data_entities` + sample viewer |
| plans | `/plans` | any | plan list + approve/reject |
| diffs | `/diffs` | any view, operator approve | full ultrareview render |
| findings | `/findings` | any | filtered list + rebuild |
| audit | `/audit` | any | org-scoped audit search |
| settings | `/settings` | admin mutations | secrets CRUD + per-project DB listing |
| organizations | `/organizations` | admin | CRUD |
| sso | `/sso` | admin | runtime probe + env reference |
| gdpr | `/gdpr` | admin | export JSON / erase user |
| login | `/login` | public | local password + SSO link |

## 8. Deployment posture

- Image is multi-stage Python 3.12 slim; platform + worker share it.
- Optional monitoring stack (`docker-compose.monitoring.yml`) adds
  Prometheus + Grafana with provisioned dashboard + datasource.
- Analyzer binaries are **not** built into the platform image by default;
  operators choose between baking them via multi-stage COPY or running
  them as sidecars via the Docker socket. Documented in
  `operator-guide/deployment.md` §10b.
- Backups — `scripts/backup.sh` dumps Postgres custom-format with
  retention pruning; `scripts/restore.sh` refuses to run without
  `MNEMOS_RESTORE_CONFIRM=yes`.
- Secret key rotation — `python -m app.cli rotate-fernet-key` walks every
  stored secret, decrypts with `--old-key`, re-encrypts with `--new-key`.

## 9. Testing

- 60 unit tests in `server/tests/` exercise masking, policy, RBAC, org
  scope, maintenance windows, TCP probe parsing, Fernet rewrap, KMS
  backends, OIDC signature verification, and finding maths.
- 10 integration tests marked `integration` that hit the ASGI app, the
  DB, and Redis. Opt-in via `MNEMOS_SKIP_INTEGRATION=1`.
- CI runs ruff + `alembic upgrade → downgrade → upgrade` + full pytest +
  docker build + trivy HIGH/CRITICAL scan (uploading SARIF to GitHub code
  scanning) + CycloneDX SBOM artefact.

## 10. Known limits (operator responsibility)

The platform intentionally stops at its process boundary. The following
belong to the deployment, not the code:

1. **Analyzer binaries** — see §10b of the operator guide for the two
   supported procurement modes.
2. **TLS termination** — the uvicorn server listens plain HTTP on 8080;
   nginx/caddy/traefik must front it.
3. **Vault token renewal** — when `KMS_BACKEND=vault`, a sidecar must
   refresh `VAULT_TOKEN` before it expires; the backend only reads the
   DEK at process start.
4. **Load-test numbers** — `scripts/loadtest/baseline.js` + the gates in
   `operator-guide/performance-review.md` produce measurements; the
   numbers themselves are per-environment and not shipped as SLOs.
5. **OIDC id_token verification** — production-ready (JWKS signature,
   audience, issuer, clock skew). JWT `nonce` claim enforcement is a
   future hardening step once the login flow carries one end-to-end.

## 11. File map — where to look

| Concern | Start here |
|---|---|
| HTTP entrypoint | `server/app/main.py` |
| Session / cookie | `server/app/auth/sessions.py`, `server/app/api/auth.py` |
| RBAC gates | `server/app/auth/rbac.py` |
| Tenancy gates | `server/app/auth/org_scope.py` |
| OIDC SSO | `server/app/auth/oidc.py` |
| KMS | `server/app/safety/kms.py` |
| Masking & policy | `server/app/data_sampler/{masking,project_db,maintenance}.py` |
| Rate limit | `server/app/safety/ratelimit.py` |
| Analyzer orchestration | `server/app/orchestrator/jobs.py` |
| Analyzer subprocess runner | `server/app/analyzers/runner.py` |
| Finding detectors | `server/app/merge/findings.py` |
| LLM summarisation | `server/app/extractor/{agent,runner}.py` |
| Ultrareview | `server/app/safety/review/pipeline.py` |
| MCP tools | `server/app/mcp/server.py` |
| Dashboard templates | `server/app/dashboard/templates/` |
| Metrics + errors + logs | `server/app/obs/` |
