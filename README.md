# Mnemos — Knowledge Production Platform

Self-hosted platform that continuously analyses multi-language, multi-database
production systems (C#, TypeScript, MSSQL, Oracle, .NET binaries) and turns
the extracted knowledge into an accessible asset for development, Q&A, and
safe data lookup.

**Status**: beta. Single-organisation self-hosted deployments are production-
capable. See [`docs/architecture.md`](docs/architecture.md) for the delivered
architecture and [`docs/operator-guide/deployment.md`](docs/operator-guide/deployment.md)
for operator workflows.

## 목적 (Purpose)

> **운영 중인 복합 언어·복합 DB 시스템을 지속적으로 분석·축적하여, 그 축적된 지식 자산으로
> 개발·질의응답·데이터 조회 요청을 상시 처리하는 자체 호스팅 플랫폼.**
> *(Mnemos_spec.md §1.1)*

Mnemos exists to solve three problems that plague enterprise polyglot
systems (C# + TypeScript + Oracle/MSSQL + opaque .NET DLLs):

1. **Knowledge decay** — docs go stale, expertise lives in people's heads,
   and one-shot analyses are obsolete the moment they finish.
2. **Operational risk** — generic AI coding tools have no concrete knowledge
   of a specific production system and cannot be trusted with read-write
   access to live data or `main` branches.
3. **Data opacity** — schemas alone don't reveal what actually lives in a
   column; safe sampling with PII masking is required.

### Three first-class request types

The platform's purpose is **request handling on top of accumulated knowledge**,
not one-shot analysis. The three request types are co-equal:

| Type | Example | Tooling |
|------|---------|---------|
| **Q&A** | "Where is the retry logic for failed payments?" | MCP `search_symbols`, `get_symbol`, graph traversal |
| **Data lookup** | "Show me 10 sample rows from `Orders`, masked." | MCP `sample_data`, `query_data` — PII masked, audited, rate-limited |
| **Development** | "Add caching to this endpoint." | MCP `submit_plan` → Gate A → `submit_diff` → Gate B → GitLab MR |

### Non-negotiable design principles (spec §2)

1. Language-neutral knowledge graph is a first-class citizen.
2. Boundaries are joined by **contracts**, not source-to-source links.
3. Information contributes only what it can prove — every node/edge carries a
   `certainty` flag (`verified` / `asserted` / `inferred`).
4. Conversation & coding loops are delegated to Claude Code; we wrap them
   with knowledge production, safety gates, and tools.
5. **The production system is sacred** — no direct writes to `main`,
   no writes to operational DBs, no production deploys. Bypass switches
   are **not** built.
6. Bottom-up incremental analysis — no LLM call ever sees the whole
   codebase.
7. The platform is an always-on service, not a batch job; state is
   restart-safe.
8. Data access is least-privilege, masked, and audited.
9. Every operator function is reachable from the GUI.
10. Single-operator-friendly — Docker Compose, single Python server,
    minimal external dependencies.

### Phase 1 success criteria (spec §1.5)

- Register a real C# + TS + MSSQL/Oracle system via GUI → first full analysis
  in ≤ 8 hours.
- After registration, run in **always-on mode** — react to git push, schema
  changes, and runtime traces.
- Q&A / data / dev requests all natural from Claude Code over MCP.
- Data lookups always return PII-masked samples.
- Dev requests pass Gate A + Gate B and land as a GitLab MR.
- All LLM / MCP / file-write / DB / data-query operations are audit-logged.
- The three safety isolations (source, DB, runtime) are enforced
  automatically.

## What's in the box

- **Analysis pipeline** — per-language analyzers feed a bitemporal knowledge
  graph (nodes + edges with `valid_from`/`valid_to`), reconciled into six
  Finding types (duplicate endpoints, unverified claims, dynamic calls,
  dead paths, schema mismatches, opaque components failing).
- **LLM summarisation** — L1 (function) → L2 (file) → L3 (module) hierarchy
  with evidence hashing so the LLM only re-summarises when underlying facts
  change.
- **Data path safety** — per-project DB bindings with `sensitive_tables`,
  regex-based `masking_rules`, Oracle `allow_awr` consent, and
  cron-expression `maintenance_windows`. Every query is masked, audited, and
  rate-limited.
- **MCP server** — 18 tools exposing the graph, data samples, file reads,
  plan submission, and worktree editing to IDE agents.
- **Plan / diff / MR flow** — AI-driven changes land as plans, run through a
  multi-pass ultrareview, and open GitLab MRs when approved.
- **RBAC** — local login with `admin` / `operator` / `viewer` roles,
  organisation-scoped multi-tenancy, optional OIDC SSO with JWKS signature
  verification.
- **Operability** — JSON logs with `x-request-id` correlation, Prometheus
  `/metrics` (optional bearer-token auth), deep `/health/ready` covering
  DB + Redis + worker heartbeat, Fernet key rotation CLI,
  `pg_dump` backup / restore scripts, pluggable KMS (local env or
  self-hosted HashiCorp Vault).

## Quick start (local)

```bash
cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  | xargs -I{} sh -c 'echo "FERNET_KEY={}" >> .env'

docker compose up -d
docker compose exec platform alembic upgrade head

# create the first admin
docker compose exec platform python -m app.cli create-user --username admin --role admin

curl -sf http://localhost:8080/api/v1/health        # liveness
curl -sf http://localhost:8080/api/v1/health/ready  # deep check
```

Visit `http://localhost:8080/login`. For TLS, reverse-proxy configuration,
backups, and upgrades, see
[`docs/operator-guide/deployment.md`](docs/operator-guide/deployment.md).

## Optional: monitoring stack

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  --profile monitoring up -d
```

Grafana lands at `:3000` with the **Mnemos Overview** dashboard pre-provisioned.

## Repository layout

```
Mnemos/
├── docker-compose.yml              # core: postgres, redis, platform, worker
├── docker-compose.monitoring.yml   # optional: prometheus + grafana
├── server/                         # FastAPI platform (Python 3.12)
│   ├── app/
│   │   ├── api/                    # REST endpoints (auth, projects, diffs, …)
│   │   ├── auth/                   # RBAC, sessions, OIDC, org-scope ACL
│   │   ├── safety/                 # crypto, kms, ratelimit, probe, ultrareview
│   │   ├── analyzers/              # subprocess runner + language registry
│   │   ├── orchestrator/           # ARQ jobs, worker heartbeat, progress bus
│   │   ├── merge/                  # node/edge upsert, finding detectors
│   │   ├── extractor/              # L1-L3 LLM summarisation
│   │   ├── data_sampler/           # masking, project_db policy, maintenance
│   │   ├── mcp/                    # MCP server (18 tools)
│   │   ├── obs/                    # request-id, JSON logs, metrics, errors
│   │   └── dashboard/              # Jinja templates + HTMX UI
│   │                               #   12 tabs total: 9 operator (Dashboard,
│   │                               #   Projects, Analysis, Data, Plans,
│   │                               #   Diffs, Findings, Audit, Settings)
│   │                               #   + 3 admin-only (Organizations,
│   │                               #   SSO/OIDC, GDPR tools)
│   ├── alembic/versions/           # migrations 0001 → 0015 (see /server/alembic)
│   └── tests/                      # pytest suite — ~65 unit + ~9 integration
│                                   # (markers split via `pytest -m integration`)
├── analyzers/                      # language/DB analyzer source
│   ├── ggoss-csharp/
│   ├── ggoss-ts/
│   ├── ggoss-sql-mssql/
│   ├── ggoss-sql-oracle/
│   └── ggoss-binary-dotnet/
├── monitoring/                     # Prometheus config + Grafana JSON
├── scripts/                        # backup.sh, restore.sh, loadtest/
├── .github/workflows/              # CI: lint + test + build + SBOM + trivy
└── docs/
    ├── architecture.md             # delivered architecture & subsystems
    ├── analysis-strategy.md
    ├── analyzer-contract.md
    ├── ultrareview.md
    └── operator-guide/
        ├── deployment.md           # end-to-end operator runbook
        ├── performance-review.md   # load-test gates + rate-limit tuning
        └── phase1_checklist.md
```

## Delivery history (branch `claude/review-project-compliance-X8OEU`)

| Commit    | Summary |
|-----------|---------|
| `2f6ca84` | project_dbs table + per-DB policy (spec §12.2 gap) |
| `03843c9` | Phase A — RBAC, rate limit, structured logs, uniform error handler, deep health, graceful shutdown, CI, operator guide |
| `e53f3d2` | Phase B — Prometheus metrics, Grafana dashboard, backup/restore scripts, Fernet key rotation CLI, k6 baseline, trivy SBOM in CI |
| `074b055` | Phase C — organisations model, OIDC SSO skeleton, pluggable KMS, GDPR export/erase |
| `c6fbce9` | Phase C follow-up — Vault KMS (replacing AWS KMS), require_project_org retrofit, admin dashboard tabs, perf indexes, load-test checklist |
| `24989d0` | Phase D — re-audit fixes (secure cookies, audit/diffs tenancy, JWKS verification) + spec analysis gaps (live_schema wiring, 6/6 findings, analyzer binary docs) |
| `f69e51d` | UI completion — dashboard home widgets + settings CRUD real implementation |

## Tests

```bash
cd server
pytest -m "not integration"    # 60 pure unit tests, no external services
pytest                          # full suite — requires Postgres + Redis
ruff check .
```

CI runs lint + migrations up/down/up + full pytest + docker build + trivy
vulnerability scan + CycloneDX SBOM upload.

## Security posture

- Cookies — `SESSION_COOKIE_SECURE=true` by default (flip to `false` only for
  local HTTP dev).
- Secrets — Fernet DEK sourced from env or Vault KV-v2; `MNEMOS_ENV=production`
  refuses to boot without an explicit `FERNET_KEY`.
- Session cookies — HttpOnly, SameSite=Lax, signed opaque tokens stored in
  Redis.
- OIDC — id_token signature verified against the IdP JWKS (RS256/ES256)
  before any claim is trusted. Audience, issuer, and clock skew are all
  validated.
- Org boundary — cross-tenant access returns `404 not_found` (not 403) so
  project existence does not leak.
- `/metrics` — optional `METRICS_BEARER_TOKEN` for scrape auth.
- Rate limits — Redis sliding-window per-user on `/data/query` (30/min) and
  `/refresh_sample` (20/min). Rejections audit-logged.
- Write path — analyzers never execute DDL/DML directly; the platform is the
  only commit point.

## License

See [`LICENSE`](LICENSE).
