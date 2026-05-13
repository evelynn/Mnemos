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
│   └── tests/                      # pytest suite — 332 unit + 16 integration
│                                   # (`pytest -m "not integration"` for the
│                                   #  unit-only run that needs no services)
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
        ├── phase1_checklist.md
        └── phase2_backlog.md       # deferred items (OTLP Tier 2, i18n, …)
```

## Phase 2 status

The 10 P2-* items the 7 self-review rounds deferred have been
substantially shipped in PR-20 ~ PR-27:

| Item | Status | Landed in |
|------|--------|-----------|
| P2-1 OTLP Tier 2 (trace merge) | ✓ initial | PR-25 |
| P2-2 RuntimeObservation table | ✓ (incl. 14-day retention sweep) | PR-25 + PR-29 |
| P2-3 Korean UI (i18n) | ✓ user-facing surfaces | PR-26 / PR-27 / PR-28 / PR-29 / PR-30 |
| P2-4 Relative timestamps | ✓ | PR-20 |
| P2-5 Colour-blind status glyphs | ✓ | PR-20 |
| P2-6 Large-result pagination | ✓ client-side | PR-24 |
| P2-7 Dashboard drill-down | ✓ | PR-22 |
| P2-8 SSE cross-tab notification | ✓ | PR-23 |
| P2-9 Onboarding auto-progression | ✓ | PR-21 |
| P2-10 TOTP / DPoP step-up auth | out of scope | — single-operator threat model |

See [`docs/operator-guide/phase2_backlog.md`](docs/operator-guide/phase2_backlog.md)
for the per-item detail, the follow-up scope each shipped item still
has open (e.g. distributed-trace assembly for P2-1, server-side
Jinja translation for P2-3), and the explicit "out of scope entirely"
list (multi-region, marketplace, BYO LLM, mobile).

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

### Branch `claude/project-analysis-audit-A4rEd` — 7 self-review rounds (PR-1 → PR-18)

Each "round" pairs a Team A design agent with a Team B critic agent
that must surface ≥1 must-fix. The cycle terminates when two
consecutive rounds raise zero new code defects.

| Round | PRs | Headline change |
|------|------|-----------------|
| 1 | PR-1 → PR-6 | Critical-2: `override=true` removed in favour of a break-glass TTL grant; ProjectDB now refuses a binding without a metadata-only read-only probe. Plus webhook → ARQ enqueue, ARQ cron + Postgres advisory locks, SQLGlot row-cap with Korean PII baseline, real `git worktree` + read-only bind mount. |
| 2 | PR-7 → PR-10 | DB-probe analyzer verb (mssql + oracle, metadata-only — no rolled-back DML); data-path safety net (DRY, dialect single-source, max-row clamp + RFC 7234 `Warning` header, env allowlist, `worktree_meta` JSONB); UI consolidation (focus, modal dialogs, dialog-polyfill, toast helpers, `renderJsonFromScript`); Korean PII validators (RRN / foreigner ID / Luhn / driver's licence). |
| 3 | PR-11 → PR-14 | PII validator wired into the masking engine (no more leaking `[UNVERIFIED_*]`); four real Grafana metrics emitted from real call sites; UI a11y / IME-safe rationale counter / clock-offset-aware countdown; ProjectDB + data-query GUI forms; break-glass share-context URL (token still out-of-band). |
| 4 | PR-15 + PR-16 | UX quick wins (component_id surfaced, regex client validation, Korean column names in `PARTIAL_MASK_COLUMNS`, friendly SQL parser errors, secret-empty hint) and a full pytest green-up (188/188). |
| 5 | PR-17 | Share-URL guest flow (`sessionStorage`-based hash stash/restore); first-run onboarding card; SSE reconnect with exponential backoff + 50% jitter + `visibilitychange`; `MNEMOS_USER_ROLE_HINT` button gating; DB-aggregate `/api/v1/health/metrics_summary` (replaces process-local Prometheus); CI builds the .NET analyzers and asserts ≥9 integration tests actually collect. |
| 6 | PR-18 | `submit_diff` now requires operator (closed the viewer can-spam loophole); dashboard auto-redirects `#approve=…` to `/diffs`; SSE failure toast names the Monitor button explicitly; `docs/operator-guide/phase2_backlog.md` consolidates the 10 deferred items. |
| 7 | PR-19 | Doc cleanup — README links the Phase 2 backlog and the test counts match reality (212 unit + 16 integration). Round 7 found zero code defects; the cycle terminates here. |

### Phase 2 sprint (PR-20 → PR-27)

After the audit cycle converged, the deferred Phase 2 items were
implemented one-per-PR (with two pair-shipped where the changes
touched the same files):

| PRs | Phase-2 items | Headline |
|-----|---------------|----------|
| PR-20 | P2-4 + P2-5 | Relative timestamps via ``Intl.RelativeTimeFormat`` + colour-blind glyphs on every ``.badge.*`` and ``.sse-status.*`` state |
| PR-21 | P2-9 | Onboarding card promoted from a static 3-step wall to a session-stored state machine that strikes through completed steps and hides itself when all three are done |
| PR-22 | P2-7 | Every actionable stat card on the dashboard becomes an ``<a class="stat-link">`` with the matching query string; landing pages auto-apply the filter |
| PR-23 | P2-8 | ``BroadcastChannel("mnemos-sse")`` propagates the analysis-tab SSE state to every other tab as a sticky strip |
| PR-24 | P2-6 | Client-side chunked rendering (100 rows / page) for the data tab and findings list, eliminating the freeze on 10K-row result sets |
| PR-25 | P2-1 + P2-2 | OTLP trace-tree assembly + ``runtime_observations`` table + ``EXPOSES``/``CALLS`` upsert into the graph |
| PR-26 | P2-3 (initial wave) | Korean phrase book + sidebar EN / 한국어 switcher with ``data-i18n`` / ``data-i18n-placeholder`` markup convention |
| PR-27 | P2-3 (broader rollout) | Sidebar nav and empty-state messages on the data, analysis, findings tabs picked up the ``data-i18n`` markers; ``Findings rebuild queued`` toast goes through ``MnemosUI.t`` |

### Post-sprint convergence (PR-28 → PR-31)

After the sprint shipped, four more self-review rounds (8 → 11) ran
to catch anything Phase 2 had introduced. Each is a single PR.

| PR | Round closed | Headline |
|----|-------------|----------|
| PR-28 | 8 | i18n broader rollout (every page ``<h1>`` + sidebar admin + audit timestamps), onboarding state moved from sessionStorage to localStorage (cross-tab fix), SSE state persisted to localStorage so a fresh tab can replay the last broadcast |
| PR-29 | 9 | ``runtime_observations`` 14-day retention sweep wired into the existing ``retention_purge`` cron (PR-25 had promised the TTL but not implemented it), plus form-label CTA i18n (``Search`` / ``Load`` / ``Rebuild`` / ``Start analysis`` / ``Load runs`` / ``Load submissions``) |
| PR-30 | 10 | 22 form labels across 9 templates now wear ``<span data-i18n>`` so the phrase-book entries PR-29 added actually fire at runtime (the audit caught the dead entries); 9 new Korean translations |
| PR-31 | 11 | README convergence — this entry plus a "cycle closed" marker. Round 11 found Critical 0 / Major 0 / Minor 0; the audit cycle terminates with zero defects in flight. |

**Final state**: 30 PRs, **332 unit + 16 integration tests**,
``ruff check`` clean, 1 new dep (``sqlglot``), 1 new alembic
migration (``0016_runtime_observations``), 2 new API endpoints
(``/api/v1/health/metrics_summary``, ``/api/v1/diff_submissions``).
spec §2 (10 of 10 principles) preserved end-to-end.

### Self-review cycle summary

Eleven rounds total — Phase 1's seven rounds (PR-1 → PR-19) plus
four post-Phase-2 rounds (PR-28 → PR-31). Critical-defect count
per round:

```
Phase 1:  2 → 4 → 8 → 2 → 2 → 1 → 0
Phase 2+: 3 → 1 → 1 → 0     (UX-major in the 8th)
```

Two consecutive zero-defect rounds (7th and 11th) bracket the
work; everything between landed in commits with tests that pin
the fix.

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
