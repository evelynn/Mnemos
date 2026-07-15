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
  ↓ status=running, capture GraphHead base generation, ProgressBus bound
  for language in project.languages:
    for verb in (symbols, contracts, calls, data_access):
      StageTracker { subprocess analyzer }   (app/analyzers/runner.py)
        ↓ JSON lines → _record_payload → graph_node_stage / graph_edge_stage
                                         (app/graph_publication.py)
  for pdb in ProjectDBs:                    ← Phase D: live DB schema stage
    maintenance_window gate                  (app/data_sampler/maintenance.py)
    decrypt secret                           (app/safety/crypto.py → kms.py)
    subprocess analyzer(live_schema) with MNEMOS_DB_CONN env
      ↓ JSON lines → staged DataEntity nodes
  seal complete producer coverage
  promote_staged_graph (one DB transaction)
    → reconcile changed/omitted Node+Edge versions
    → CAS GraphHead + immutable receipt + status=published
  reconcile runtime observations             (app/merge/runtime.py)
    → durable logical-edge overlay + overlay generation
  rebuild_findings                           (app/merge/findings.py)
    → 6 detectors run in order
  requested summarise_l1 / l2 / l3           (app/extractor/runner.py)
    → direct Anthropic or Claude Agent SDK per leaf / file / module
    → evidence-hash + source/overlay-revision Summary rows persisted
  status=completed or partial, analysis_runs_total.inc (app/obs/metrics.py)
```

Before promotion, committed stage rows are invisible to current graph readers;
failure or cancellation leaves the previous head intact. After promotion, the
source receipt remains usable even if a derived stage fails, in which case the
run closes as `partial`. HTTP, MCP, artifact, and source-file readers pin and
revalidate the source and overlay revisions so a READ COMMITTED transaction
cannot silently combine two graph states.

Each stage is wrapped in `StageTracker` (`app/orchestrator/stages.py`) which
persists progress to `analysis_stages` and emits SSE events on the
`ProgressBus` so the Analysis tab can render a live pipeline view.

### 2.3 Plan / diff / MR path

```
Dev agent → POST /api/v1/projects/{id}/plans
  ↓ lock canonical GraphHead/AnalysisRun receipt
  ↓ impact_analysis() + detached worktree at the publication's exact git_sha
  ↓ Plan {run_id, git_sha, source_generation, overlay_generation}
Human reviewer → POST /api/v1/plans/{id}/decide (approve / reject)
  ↓ exact current revision + worktree HEAD revalidated, audit record
Dev agent → MCP edit_file_in_worktree / run_in_sandbox
Dev agent → POST /api/v1/diff_submissions
  ↓ run_pipeline: 6-pass ultrareview         (app/safety/review/pipeline.py)
      rules → contracts → data_access → impact → second_opinion → validator
  ↓ head locked/rechecked after review
  ↓ DiffSubmission {status=pending_approval | blocked, review revision}
Operator → POST /api/v1/diff_submissions/{id}/approve
  ↓ submission + Plan + worktree + current review revision revalidated
  ↓ blocked verdict requires a one-use, two-person, same-revision grant
  ↓ create_mr_from_worktree                  (app/gitlab_client/mr.py)
  ↓ status=approved, gitlab_mr_url recorded
```

## 3. Data model

All tables live in one Postgres schema. The repository's **current Alembic
head** is authoritative; do not infer the supported schema from a numbered
range in an older report. Upgrades must traverse the full single-head chain.
The performance-index revision uses `CONCURRENTLY` outside the transaction,
and graph-publication revisions require the worker-drain procedure in
`operator-guide/deployment.md`.

| Domain | Current tables / contract |
|---|---|
| Identity | `users`, `organizations`, `api_keys`, `platform_settings` |
| Secrets | `secrets` |
| Projects | `projects`, `project_dbs` |
| Audit | `audit_logs` |
| Knowledge graph | `nodes`, `edges`, `node_sources`, `graph_heads`, `graph_node_stage`, `graph_edge_stage` |
| Durable graph evidence | node/edge human overlays plus edge runtime overlays/cursors |
| Samples | `data_samples` (source/overlay-revision authorised), `data_query_log` |
| Derived current products | `findings`, `summaries`, each authorized by source/overlay revision markers at the current head |
| Plans | source-bound `plans`, revision-bound `diff_submissions` and break-glass grants |
| Runs and stages | `analysis_runs`, `analysis_stages` |

### 3.1 Bitemporal graph

`nodes` and `edges` keep history via `valid_from` / `valid_to`. Only the row
with `valid_to IS NULL` is "current". Production ingest never mutates those
rows one analyzer record at a time. Each run materializes one candidate per
logical identity in staging, freezes its complete producer/deletion authority,
and promotes under the project head lock. Semantically unchanged rows retain
their current version; changed or authoritatively omitted rows close at the
same publication timestamp used by the next head generation.

Partial unique indexes enforce one current Node identity and one current Edge
`(source,target,kind)` identity while allowing historical versions. Human and
runtime facts live in durable logical-identity overlays and are re-materialized
onto a replacement version; analyzer semantic hashes explicitly exclude those
overlay-owned fields. Automatic history pruning is disabled until a retained-
from watermark can make historical comparison fail closed instead of silently
dropping evidence.

### 3.2 Publication, overlay, and derived-currentness contract

The source graph and its derived products deliberately have separate revision
boundaries:

```text
queued → running → staging → sealed → published → completed
                                                    └→ partial
running → failed | cancelled       (before publication only)
```

Promotion locks the project `GraphHead`, verifies the run's captured base
generation, reconciles staged candidates, and commits the new generation,
immutable publication receipt, and `AnalysisRun.status=published` in one
transaction. A retry with the same receipt is idempotent. Staging commits are
not current graph commits; a pre-publication failure cannot expose a mixed run.

`GraphHead.generation` identifies the published source graph.
`GraphHead.overlay_generation` identifies durable human/runtime overlay state.
Overlay writers lock the head and increment the overlay generation, and source
promotion re-materializes the latest overlay facts on replacement physical
versions. Current HTTP, MCP, artifact, and source-snapshot consumers capture
both values and revalidate them before returning a result.

Findings and summaries are post-publication products. A row is current only
when its stored graph and overlay validation markers match the ready head;
legacy or mismatched rows are hidden rather than served as current. Therefore
`published` means the source receipt is readable, not that narration and
findings are complete. `completed` means post-processing finished; `partial`
means the source remains readable but at least one post-publication stage did
not finish successfully.

Historical comparison accepts only receipt-validated terminal source
publications and fails closed when their immutable provenance is unavailable.
Finding history is not reconstructed from mutable current-product rows.
Automatic graph-history pruning remains disabled because no retained-from
watermark exists yet.

### 3.3 Multi-tenancy

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

Finding identity is stable across physical bitemporal versions. Node findings
key on their logical node subject; edge findings key on
`(source_id, target_id, edge.kind)`. `run_all()` locks one ready head, stamps
all detected rows with that source/overlay revision, and resolves previously
validated open rows that disappeared. Direct detector calls remain useful for
diagnostics but deliberately clear currentness markers.

### 5.3 LLM summarisation

`app/extractor/agent.py` supports a direct Anthropic SDK call when an API key is
configured, then the Claude Agent SDK subscription path when available. If no
backend is available, a budget is exceeded, or a provider result fails the
schema/grounding boundary, it returns an explicitly labelled deterministic
stub with a fallback reason; a stub is diagnostic output, not source truth.

The runner (`app/extractor/runner.py`) packs complete bounded JSON evidence,
hash-stamps it into Summary rows, and skips re-summarisation only for a valid
cache hit at the same source/overlay revision. L1 fires per function, L2 per
file, and L3 per module; each level feeds the next via `pack_by_budget()`.

### 5.4 MCP server

`app/mcp/server.py` exposes a bounded, count-independent tool registry via
STDIO. Categories:

- **Orientation and graph queries** — `get_project_index`,
  `get_task_context_pack`, `search_symbols`, `get_symbol`, `find_callers`,
  `find_callees`, `impact_analysis`, `get_contract`, `get_data_access`,
  `list_flows`, `find_runtime_path`, `compare_runs`.
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

Jinja + HTMX + vanilla JS expose the current route registry without freezing a
tab count in this document:

| Surface | Paths | Purpose |
|---|---|---|
| Orientation | `/`, `/projects`, `/analysis` | project inventory, run trigger, and SSE stage lifecycle |
| Source analysis | `/ask`, `/chat`, `/graph`, `/report`, `/docs` | graph-grounded inquiry, bounded navigation, and generated guidance |
| Evidence and change workflow | `/data`, `/findings`, `/plans`, `/diffs` | data impact, revision-current findings, plans, and ultrareview |
| Operations | `/health`, `/audit`, `/settings`, `/profile` | readiness, org-scoped audit, connections/settings, and user profile |
| Administration | `/users`, `/organizations`, `/sso`, `/gdpr` | admin-only mutations; API authorization remains authoritative |
| Public authentication | `/login`, `/forgot`, `/reset`, `/invite` | local/SSO entry and credential/invite flows |

## 8. Deployment posture

- Image is multi-stage Python 3.12 slim; platform + worker share it.
- Optional monitoring stack (`docker-compose.monitoring.yml`) adds
  Prometheus + Grafana with provisioned dashboard + datasource.
- The standard platform/worker image bundles the in-repo Python,
  TypeScript/JavaScript, C/C++, Java, Kotlin, Web, and configured tree-sitter
  analyzers. C#, live MSSQL/Oracle, and other standalone projects still need
  an explicitly installed contract-compatible command; merely building the
  Compose analyzer profile does not wire a sidecar into `run_ingest`. See
  `operator-guide/deployment.md` §10b.
- Backups — `scripts/backup.sh` dumps Postgres custom-format with
  retention pruning; `scripts/restore.sh` refuses to run without
  `MNEMOS_RESTORE_CONFIRM=yes`.
- Secret key rotation — `python -m app.cli rotate-fernet-key` walks every
  stored secret, decrypts with `--old-key`, re-encrypts with `--new-key`.

## 9. Testing

- `server/tests/` contains focused unit and integration suites for analyzer
  bounds, graph publication, overlay preservation, lifecycle transitions,
  source/overlay-pinned readers, finding/summary currentness, structured LLM
  boundaries, security, and operator surfaces. Test counts are intentionally
  not frozen in architecture documentation.
- Service-backed cases declare their PostgreSQL/Redis requirements; a local
  SQLite/mock pass is not evidence that those cases ran against real services.
- The CI workflow defines lint, migration round-trip, pytest, image build,
  vulnerability scan, and SBOM jobs. For the atomic-publication change set,
  the highest recorded evidence is local E2; real PostgreSQL CI, hard-kill
  injection, live-provider canaries, and the 50 K-file soak remain unrun. See
  `04-eval/atomic-graph-publication-phase-b-2026-07-15.md`.

## 10. Known limits (operator responsibility)

The platform intentionally stops at its process boundary. The following
belong to the deployment, not the code:

1. **Optional analyzer binaries** — languages outside the standard bundled
   set require a contract-compatible command on the worker `PATH`; see §10b
   of the operator guide.
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
