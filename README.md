# Mnemos — AI Source Analysis Guide

Mnemos deterministically indexes large, multi-language source trees into a
grounded, bitemporal graph that an AI can re-query through MCP. It is a source
reference and analysis guide: it supplies source-located symbols, relationships,
certainty, coverage gaps, and bounded task context. Analyzer coverage and
certainty determine which references are exact; unsupported language features
remain explicit gaps. It is not a generic SaaS, chatbot, or administrator
product, and it does not ask an LLM to form an
expensive opinion about the whole repository up front.

**Status**: beta, not production-qualified. The core source-index/MCP workflow
has unit, mock-integration, one external-repository evaluation, real PostgreSQL
ledger/concurrency validation, and a narrow 50 K-file/50 K-node publication
soak. Run-scoped staging, atomic graph-head publication, durable evidence
overlays, and source/overlay generation-pinned readers are connected to ingest.
Live-provider validation, hard process-kill fault injection, and representative
unseen-repository quality/token comparisons remain.
See the [Phase-B contract and evidence report](docs/04-eval/atomic-graph-publication-phase-b-2026-07-15.md)
and [`docs/architecture.md`](docs/architecture.md) for the delivered architecture.
The current hard-budget/PostgreSQL evidence is in the
[speed/token root report](docs/04-eval/speed-token-root-design-2026-07-16.md), and the honest
[comparison with codebase-memory-mcp](docs/04-eval/codebase-memory-comparison-2026-07-16.md)
records where Mnemos still does not lead.
The [token/refresh research](docs/04-eval/token-refresh-architecture-research-2026-07-15.md)
and [July 14 remediation assessment](docs/04-eval/source-analysis-purpose-and-remediation-2026-07-14.md)
are historical checkpoints that explain the original failure modes;
operator workflows are in
[`docs/operator-guide/deployment.md`](docs/operator-guide/deployment.md). For a
zero-external-service test drive, see
[Quick start (docker-free)](#quick-start-docker-free-local-mode) below.

## 목적 (Purpose)

> **AI가 거대한 소스를 정확히 분석하도록, 소스를 결정적으로 인덱싱하고 근거·경로·영향
> 범위·불확실성을 작게 재조회할 수 있게 제공하는 보조 도구.**

Mnemos exists to solve three failures of direct repo prompting:

1. **Knowledge decay** — docs go stale, expertise lives in people's heads,
   and one-shot analyses are obsolete the moment they finish.
2. **Context cost** — repeatedly sending a whole repository consumes tokens
   before the AI has identified the small evidence slice it needs.
3. **Unsupported conclusions** — without file/line/edge evidence and explicit
   coverage gaps, an AI confuses guesses with source facts.

### How the index is consumed

All consumers are downstream of the source-analysis graph; none broadens the
product into a generic workflow platform.

| Type | Example | Tooling |
|------|---------|---------|
| **Q&A** | "Where is the retry logic for failed payments?" | Ask tab (`POST /projects/{id}/ask`) + MCP `search_symbols`, `get_symbol`, graph traversal, then a narrow immutable-snapshot `read_file` check |
| **Data impact** | "Which code reads `Orders`?" | MCP `get_data_access`, `get_data_entity`; optional safe DB evidence remains subordinate to source analysis |
| **Development analysis** | "What is the blast radius of changing this endpoint?" | MCP task pack + callers/callees/contracts/data access + narrow source verification |

### Non-negotiable design principles (spec §2)

1. Language-neutral knowledge graph is a first-class citizen.
2. Boundaries are joined by **contracts**, not source-to-source links.
3. Information contributes only what it can prove — every node/edge carries a
   `certainty` flag (`verified` / `asserted` / `inferred`).
4. The AI performs the reasoning; Mnemos supplies bounded, grounded guidance
   and never promotes optional narration above deterministic facts.
5. **The production system is sacred** — no direct writes to `main`,
   no writes to operational DBs, no production deploys. Bypass switches
   are **not** built.
6. Deterministic index first: a normal first pass uses zero LLM tokens.
   Optional AI work has hard call/input/output/wall bounds. Every production
   paid generation also requires a positive project cap and an atomic
   worst-case dollar reservation before dispatch; no LLM call sees the whole
   codebase. Opaque/unpriced transports are fail-disabled.
7. The platform is designed as an always-on service. Analyzer output is isolated
   by run and becomes current through one atomic publication receipt; optional
   post-publication products may finish as explicitly `partial` without corrupting
   or rolling back the readable source generation.
8. Data access is least-privilege, masked, and audited.
9. Required source-analysis operator workflows should be reachable from the GUI;
   a missing surface is a product gap, not a reason to broaden scope.
10. Single-operator-friendly — Docker Compose, single Python server,
    minimal external dependencies (or fully docker-free local mode).

### Core success criteria

- A full source index consumes zero LLM tokens unless AI work was explicitly
  requested.
- MCP starts with a top-10 project index; project indexes and task packs omit
  raw source and enforce a 50 KiB serialized hard cap instead of dumping the
  repository into context.
- A same-content refresh runs no analyzer and creates no false temporal diff.
- Changed analyzer families refresh; deleted/renamed source facts close only
  after a successful authoritative-root scan. Shared facts are deleted only
  during a safe all-producer reconciliation until contribution rows exist.
- Analyzer output, memory queue, wall time, cancellation, and retry behavior
  are bounded so one bad process cannot stop the service.
- Structured AI claims are schema-valid, project-scoped, and evidence-backed;
  inferred relationships remain visibly inferred.

## What's in the box

- **Analysis pipeline** — the default worker bundles deterministic Python,
  TypeScript/JavaScript, C/C++, Java, Kotlin, Web, and tree-sitter source
  analyzers. C#, MSSQL/Oracle, and .NET-binary analyzers are present in the
  repository but are not wired into the standard Compose worker. Analyzer
  facts feed a bitemporal knowledge graph
  (nodes + edges with `valid_from`/`valid_to`), reconciled into six Finding
  types (duplicate endpoints, unverified claims, dynamic calls, dead paths,
  schema mismatches, opaque components failing). The Python analyzer
  (`ggoss-py`) is pure-stdlib and also runs in-repo without Docker.
- **Optional LLM narration** — explicit L1 (function) → L2 (file) → L3
  (module) pass over bounded graph evidence. Semantic evidence hashes skip
  unchanged targets; this layer is not required for source lookup or MCP.
  The price-attested direct Anthropic route is crash-accounted. The opaque
  Claude Agent SDK route is intentionally disabled until it has an immutable
  route/price/output contract.
- **Ask (Q&A)** — `POST /projects/{id}/ask` answers from graph evidence.
  Arbitrary host-path deepening is disabled; agents can verify a selected range
  through the bounded MCP reader tied to the latest completed Git snapshot.
- **Search** — `search_symbols` is deterministic lexical/BM25. Legacy
  Voyage/OpenAI vector scaffolding is deliberately non-executable even when
  `MNEMOS_EMBEDDING_PROVIDER` is set because it lacks project-scoped durable
  accounting and an immutable worst-price contract.
- **Data path safety** — per-project DB bindings with `sensitive_tables`,
  regex-based `masking_rules`, Korean PII validators (RRN / foreigner ID /
  Luhn / driver's licence), Oracle `allow_awr` consent, and cron-expression
  `maintenance_windows`. Every query is masked, audited, and rate-limited.
- **MCP server** — tools exposing the graph (symbols, callers/callees,
  impact analysis, contracts, flows, runtime paths), data samples, column
  stats, file reads, plan submission, sandboxed worktree editing, and diff
  submission to IDE agents.
- **Plan / diff / MR flow** — AI-driven changes land as plans, run through a
  multi-pass ultrareview (Gate A + Gate B), and open GitLab MRs when
  approved. Worktrees are real `git worktree`s with read-only bind mounts;
  the current build has no OS containment backend, so `run_in_sandbox` is
  fail-disabled by default and always disabled in production. Its allowlisted
  local argv fallback is available only for explicitly opted-in development
  against repositories the developer trusts; it is never treated as a
  filesystem or network sandbox.
- **Runtime observation** — OTLP trace receiver assembles trace trees into
  `runtime_observations`, reconciles `EXPOSES`/`CALLS` evidence into durable
  logical-edge overlays, and materializes it onto the current graph version
  (14-day observation-retention sweep built in).
- **Voice on the Ask tab** — a full **local** voice loop: *speak* a
  question (mic → STT) and *hear* the answer (🔊 → TTS). STT defaults to
  **Moonshine tiny-ko** (optional `[voice]` extra — ~26M params, ONNX,
  no torch, beats Whisper-tiny on Korean), with multilingual faster-whisper
  as a one-env-var alternative (`[voice-whisper]`). TTS is **Kokoro-82M**
  (optional `[tts]` extra — Apache-2.0, multilingual incl. Korean). No audio
  or text leaves the deployment; buttons auto-hide when an extra isn't
  installed. See [`docs/voice-commands.md`](docs/voice-commands.md).
- **RBAC & team workflow** — local login with `admin` / `operator` /
  `viewer` roles, organisation-scoped multi-tenancy, optional OIDC SSO with
  JWKS signature verification, user CRUD + invite tokens + password reset,
  comment threads + assignees on plans and diffs, notification centre,
  brute-force lockout, CSRF middleware, sliding session TTL, per-session
  rate limiting.
- **Dashboard** — Jinja + HTMX UI with light/dark/auto theme, responsive
  mobile drawer, Korean/English i18n, command palette (`cmd/ctrl+K`),
  CSV/Excel export. Operator tabs: Dashboard, Projects, Analysis, Ask,
  Graph, Data, Plans, Diffs, Findings, Report, Docs, Health, Audit,
  Settings, Profile. Admin-only tabs: Users, Organizations, SSO/OIDC,
  GDPR tools.
- **Operability** — JSON logs with `x-request-id` correlation, Prometheus
  `/metrics` (optional bearer-token auth), deep `/health/ready` covering
  DB + Redis + worker heartbeat + analyzer availability, Fernet key
  rotation CLI, `pg_dump` backup / restore scripts, pluggable KMS (local
  env or self-hosted HashiCorp Vault), startup self-verification.

## Quick start (docker-free local mode)

The fastest way to try the platform — a single Python 3.12+ process with
**zero external services** (SQLite + in-process fakeredis, jobs inline,
available in-repo analyzers):

```bash
cd server
pip install -e ".[local]"               # adds aiosqlite + fakeredis
python -m app.serve_local --seed-demo   # boots on :8080 with a demo dataset
```

## Quick start (Docker Compose)

The service topology is Postgres + Redis + platform + one ARQ worker. The
platform and worker images contain the runnable in-repo source analyzers listed
above; standalone analyzer-profile images are optional contract-test artifacts.

```bash
cp .env.example .env

# Host repository mounted read-only as /work for a manual analysis.
# Use an absolute path. If omitted, the Mnemos checkout itself is mounted.
echo "MNEMOS_SOURCE_ROOT=/absolute/path/to/source-repo" >> .env

# Generate FERNET_KEY (encrypts the secrets table).
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  | xargs -I{} sh -c 'echo "FERNET_KEY={}" >> .env'

# Generate SECRET_KEY (signs the session cookie). The default placeholder
# in .env.example is forgeable — the platform refuses to start in
# production until you replace it with a random string.
python -c "import secrets; print(f'SECRET_KEY={secrets.token_urlsafe(48)}')" \
  >> .env

docker compose up -d --build
docker compose exec platform alembic upgrade head

# create the first admin
docker compose exec platform python -m app.cli create-user --username admin --role admin

curl -sf http://localhost:8080/api/v1/health        # liveness
curl -sf http://localhost:8080/api/v1/health/ready  # deep check
```

Visit `http://localhost:8080/login`. For TLS, reverse-proxy configuration,
backups, and upgrades, see
[`docs/operator-guide/deployment.md`](docs/operator-guide/deployment.md);
for a guided first session, see
[`docs/operator-guide/getting-started.md`](docs/operator-guide/getting-started.md).
On the Analysis tab use source path `/work`, keep `summarize` unchecked for the
zero-token index, and use a revision that exists in that Git checkout.
The standard Compose worker enforces `SOURCE_ALLOWED_ROOT=/work`, so a manual
request cannot select arbitrary container files. Completed Git runs keep only
a root-relative repository locator and MCP reads reopen the exact commit.

## Optional: monitoring stack

```bash
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
  --profile monitoring up -d
```

Grafana lands at `:3000` with the **Mnemos Overview** dashboard pre-provisioned.

## Repository layout

```
Mnemos/
├── Mnemos_spec.md                  # the platform specification (§ references)
├── docker-compose.yml              # core: postgres, redis, platform, worker
│                                   # + optional standalone analyzer profiles
├── docker-compose.monitoring.yml   # optional: prometheus + grafana
├── server/                         # FastAPI platform (Python 3.12)
│   ├── app/
│   │   ├── api/                    # REST endpoints (auth, projects, analysis,
│   │   │                           #   ask, data, plans, diffs, findings,
│   │   │                           #   users, comments, organizations, gdpr,
│   │   │                           #   webhooks, voice, health, …)
│   │   ├── auth/                   # RBAC, sessions, OIDC, org-scope ACL,
│   │   │                           #   passwords, brute-force lockout
│   │   ├── security/               # CSRF, security headers, rate-limit
│   │   │                           #   middlewares
│   │   ├── safety/                 # crypto, KMS, ratelimit, DB probe,
│   │   │                           #   SQL row-cap, Korean PII validators,
│   │   │                           #   ultrareview pipeline (review/)
│   │   ├── analyzers/              # subprocess runner + language registry
│   │   ├── orchestrator/           # ARQ jobs, stages, cron, worker
│   │   │                           #   heartbeat, progress bus
│   │   ├── merge/                  # runtime reconciliation + finding detectors
│   │   ├── extractor/              # L1-L3 LLM summarisation
│   │   ├── data_sampler/           # masking, project_db policy, maintenance
│   │   ├── mcp/                    # bounded MCP tool registry + embeddings
│   │   │                           #   adapter (vector/BM25 hybrid scaffold)
│   │   ├── sandbox/                # git worktree, command allowlist, runner
│   │   ├── runtime_receiver/       # OTLP trace ingest + scrubbing
│   │   ├── gitlab_client/          # MR creation
│   │   ├── voice/                  # local STT (Moonshine/faster-whisper)
│   │   │                           #   + TTS (Kokoro-82M) engines
│   │   ├── notify/                 # outbound notifications
│   │   ├── artifacts/              # AGENTS.md / mcp.json generators
│   │   ├── audit/                  # audit logger + middleware
│   │   ├── obs/                    # request-id, JSON logs, metrics, errors
│   │   ├── models/                 # SQLAlchemy models
│   │   ├── dashboard/              # Jinja templates + HTMX UI
│   │   │                           #   15 operator tabs (Dashboard, Projects,
│   │   │                           #   Analysis, Ask, Graph, Data, Plans,
│   │   │                           #   Diffs, Findings, Report, Docs, Health,
│   │   │                           #   Audit, Settings, Profile)
│   │   │                           #   + 4 admin-only (Users, Organizations,
│   │   │                           #   SSO/OIDC, GDPR tools)
│   │   ├── testing/                # SQLite polyglot shims for the test env
│   │   ├── local_mode.py           # docker-free mode (SQLite + fakeredis)
│   │   ├── serve_local.py          # `python -m app.serve_local` launcher
│   │   ├── seed_demo.py            # demo dataset seeder (--seed-demo)
│   │   ├── startup_verify.py       # boot-time self-verification
│   │   ├── cli.py                  # create-user, key rotation, verify, …
│   │   ├── main.py                 # FastAPI app factory
│   │   └── worker.py               # ARQ worker entrypoint
│   ├── alembic/versions/           # single-head chain; use current Alembic head
│   └── tests/                      # pytest unit/integration suites;
│                                   # service requirements are declared per case
├── analyzers/                      # language/DB analyzer source (11)
│   ├── ggoss-binary-dotnet/
│   ├── ggoss-cpp/
│   ├── ggoss-csharp/
│   ├── ggoss-java/
│   ├── ggoss-kotlin/
│   ├── ggoss-py/                   # pure-stdlib; also runs in-repo
│   │                               #   without Docker (local mode)
│   ├── ggoss-sql-mssql/
│   ├── ggoss-sql-oracle/
│   ├── ggoss-treesitter/
│   ├── ggoss-ts/
│   └── ggoss-web/
├── monitoring/                     # Prometheus config + Grafana dashboards
├── scripts/                        # backup.sh, restore.sh, loadtest/,
│   │                               # accuracy/ (extraction + Korean STT WER),
│   │                               # sequential_smoke_test.py
├── .github/workflows/              # CI: lint + test + build + SBOM + trivy
└── docs/
    ├── architecture.md             # delivered architecture & subsystems
    ├── analysis-strategy.md
    ├── analyzer-contract.md
    ├── ultrareview.md
    ├── voice-commands.md           # local STT/TTS voice loop
    ├── 04-eval/                    # dogfooding & self-audit round notes
    │                               #   (per-PR evaluation records)
    └── operator-guide/
        ├── getting-started.md      # guided first session
        ├── deployment.md           # end-to-end operator runbook
        ├── performance-review.md   # load-test gates + rate-limit tuning
        ├── large_system_readiness.md
        ├── score-evidence.md
        ├── phase1_checklist.md
        └── phase2_backlog.md       # deferred items
```

## Delivery history (summary)

The platform was built through self-review cycles — each round pairs a
design pass with an adversarial audit pass, and a phase only closes after
consecutive zero-defect rounds. Condensed phase log:

| Phase | PRs | Outcome |
|-------|-----|---------|
| Phase 1 — core platform + audit cycle | PR-1 → PR-19 | 7 rounds; closed at zero Critical. Break-glass TTL grants, read-only DB probes, SQLGlot row-cap, real git worktrees, Korean PII validators. |
| Phase 2 — UX backlog sprint + convergence | PR-20 → PR-31 | 9/10 P2 items shipped (i18n, SSE cross-tab, pagination, drill-down, OTLP Tier 2, runtime observations + retention). |
| Large-system readiness | PR-32 → PR-37 | Analyzer subprocess contract pins, 5 operator scenarios (E1–E5), crashed-worker recovery cron, scale stress tests, D1–D5 invariants. |
| Team product (RBAC/UX/design) | PR-38 → PR-48 | Users CRUD + invites + resets, CSRF/lockout/rate-limit/sliding TTL, design tokens + dark mode + responsive drawer, comments + assignees, notification centre, command palette. |
| Productisation & dogfooding | PR-49 → PR-154+ | Docker-free local mode (`serve_local`), in-repo analyzers, graph-backed Ask, agent context artifacts, graph/report/docs/health GUI tabs, and safety/contract hardening. Historical round notes live in [`docs/04-eval/`](docs/04-eval/) and do not supersede the current limitations. |

For per-PR detail, see the round notes in [`docs/04-eval/`](docs/04-eval/),
[`docs/operator-guide/phase2_backlog.md`](docs/operator-guide/phase2_backlog.md),
[`docs/operator-guide/score-evidence.md`](docs/operator-guide/score-evidence.md),
and `git log`.

**Current state**: the repository includes unit/integration suites, migrations,
MCP tools, analyzers, and the dashboard. Do not infer production readiness from
their counts; the current evidence and unverified workflows are recorded in the
Phase-B evidence report linked above.

## Tests

```bash
cd server
pytest -m "not integration"    # no external services
pytest                         # integration cases require their declared services
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
  Redis, sliding idle-timeout TTL, admin force-logout.
- Login — brute-force lockout (Redis-backed, 15-min window), 12-char minimum
  password policy with weak-list check.
- CSRF — double-submit cookie + `X-CSRF-Token` header on every mutation.
- Headers — HSTS, CSP (`script-src 'self'`), X-Frame-Options,
  Referrer-Policy.
- OIDC — id_token signature verified against the IdP JWKS (RS256/ES256)
  before any claim is trusted. Audience, issuer, and clock skew are all
  validated.
- Org boundary — cross-tenant access returns `404 not_found` (not 403) so
  project existence does not leak.
- `/metrics` — optional `METRICS_BEARER_TOKEN` for scrape auth.
- Rate limits — Redis sliding-window per-user on `/data/query` (30/min) and
  `/refresh_sample` (20/min), plus a global per-session mutation limiter
  (60/min). Rejections audit-logged.
- Write path — analyzers never execute DDL/DML directly; the platform is the
  only commit point. Sandbox commands run under an allowlist against
  read-only bind mounts.

## License

See [`LICENSE`](LICENSE).

### Third-party (bundled)

- **[ExcelJS](https://github.com/exceljs/exceljs)** (MIT) — self-hosted
  at `server/app/dashboard/static/exceljs.min.js`, lazy-loaded on first
  use to back the dashboard's **Export Excel** buttons (findings + audit
  tabs). Bundled rather than CDN-loaded so the platform works air-gapped
  and stays within its `script-src 'self'` CSP. See PR-134.
