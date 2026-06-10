# Mnemos — Knowledge Production Platform

Self-hosted platform that continuously analyses multi-language, multi-database
production systems (C#, TypeScript, Python, MSSQL, Oracle, .NET binaries) and
turns the extracted knowledge into an accessible asset for development, Q&A,
and safe data lookup.

**Status**: beta. Single-organisation self-hosted deployments are production-
capable. See [`docs/architecture.md`](docs/architecture.md) for the delivered
architecture and [`docs/operator-guide/deployment.md`](docs/operator-guide/deployment.md)
for operator workflows. For a zero-dependency test drive, see
[Quick start (docker-free)](#quick-start-docker-free-local-mode) below.

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
| **Q&A** | "Where is the retry logic for failed payments?" | Ask tab (`POST /projects/{id}/ask`, with automatic on-demand deepening) + MCP `search_symbols`, `get_symbol`, graph traversal |
| **Data lookup** | "Show me 10 sample rows from `Orders`, masked." | MCP `get_sample_data`, `search_data`, `get_column_stats` — PII masked, audited, rate-limited |
| **Development** | "Add caching to this endpoint." | MCP `submit_plan` → Gate A → `edit_file_in_worktree` / `run_in_sandbox` → `submit_diff` → Gate B → GitLab MR |

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
    minimal external dependencies (or fully docker-free local mode).

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

- **Analysis pipeline** — six language/DB analyzers (C#, TypeScript,
  Python, MSSQL, Oracle, .NET binary) feed a bitemporal knowledge graph
  (nodes + edges with `valid_from`/`valid_to`), reconciled into six Finding
  types (duplicate endpoints, unverified claims, dynamic calls, dead paths,
  schema mismatches, opaque components failing). The Python analyzer
  (`ggoss-py`) is pure-stdlib and also runs in-repo without Docker.
- **LLM summarisation** — L1 (function) → L2 (file) → L3 (module) hierarchy
  with evidence hashing so the LLM only re-summarises when underlying facts
  change.
- **Ask (Q&A) with on-demand deepening** — `POST /projects/{id}/ask` answers
  from the graph when a confident symbol match exists; otherwise it ranks
  candidate files, extracts them on demand, and re-answers, so a bounded
  first-pass analysis still converges on the right answer.
- **Hybrid search scaffold** — `search_symbols` is BM25/lexical by default;
  set `MNEMOS_EMBEDDING_PROVIDER` (`voyage` or `openai`) to enable the vector
  half of the spec's vector + BM25 ensemble.
- **Data path safety** — per-project DB bindings with `sensitive_tables`,
  regex-based `masking_rules`, Korean PII validators (RRN / foreigner ID /
  Luhn / driver's licence), Oracle `allow_awr` consent, and cron-expression
  `maintenance_windows`. Every query is masked, audited, and rate-limited.
- **MCP server** — 20 tools exposing the graph (symbols, callers/callees,
  impact analysis, contracts, flows, runtime paths), data samples, column
  stats, file reads, plan submission, sandboxed worktree editing, and diff
  submission to IDE agents.
- **Plan / diff / MR flow** — AI-driven changes land as plans, run through a
  multi-pass ultrareview (Gate A + Gate B), and open GitLab MRs when
  approved. Worktrees are real `git worktree`s with read-only bind mounts;
  `run_in_sandbox` executes commands under an allowlist.
- **Runtime observation** — OTLP trace receiver assembles trace trees into
  `runtime_observations` and upserts `EXPOSES`/`CALLS` edges into the graph
  (14-day retention sweep built in).
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
  DB + Redis + worker heartbeat + analyzer image presence, Fernet key
  rotation CLI, `pg_dump` backup / restore scripts, pluggable KMS (local
  env or self-hosted HashiCorp Vault), startup self-verification.

## Quick start (docker-free local mode)

The fastest way to try the platform — a single Python 3.12+ process with
**zero external services** (SQLite + in-process fakeredis, jobs inline,
in-repo Python analyzer):

```bash
cd server
pip install -e ".[local]"               # adds aiosqlite + fakeredis
python -m app.serve_local --seed-demo   # boots on :8080 with a demo dataset
```

## Quick start (Docker Compose)

The production topology: Postgres + Redis + platform + ARQ worker + six
analyzer images.

```bash
cp .env.example .env

# Generate FERNET_KEY (encrypts the secrets table).
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  | xargs -I{} sh -c 'echo "FERNET_KEY={}" >> .env'

# Generate SECRET_KEY (signs the session cookie). The default placeholder
# in .env.example is forgeable — the platform refuses to start in
# production until you replace it with a random string.
python -c "import secrets; print(f'SECRET_KEY={secrets.token_urlsafe(48)}')" \
  >> .env

docker compose up -d
docker compose exec platform alembic upgrade head

# Build the six language-analyzer images (one-time; the platform
# invokes them via `docker run`). Omitting this step leaves their
# stages reporting "analyzer_binary_not_found" instead of crashing —
# /api/v1/health/ready lists which are missing.
docker compose --profile analyzers build

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
│                                   # + analyzer build profiles (6 images)
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
│   │   ├── merge/                  # node/edge upsert, finding detectors
│   │   ├── extractor/              # L1-L3 LLM summarisation
│   │   ├── data_sampler/           # masking, project_db policy, maintenance
│   │   ├── mcp/                    # MCP server (20 tools) + embeddings
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
│   ├── alembic/versions/           # migrations 0001 → 0024
│   └── tests/                      # pytest suite — 1,598 unit + 16
│                                   # integration tests
│                                   # (`pytest -m "not integration"` for the
│                                   #  unit-only run that needs no services)
├── analyzers/                      # language/DB analyzer source (6)
│   ├── ggoss-csharp/
│   ├── ggoss-ts/
│   ├── ggoss-py/                   # pure-stdlib; also runs in-repo
│   │                               #   without Docker (local mode)
│   ├── ggoss-sql-mssql/
│   ├── ggoss-sql-oracle/
│   └── ggoss-binary-dotnet/
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
| Productisation & dogfooding | PR-49 → PR-154+ | Docker-free local mode (`serve_local`), in-repo Python analyzer, Ask tab with on-demand deepening, real-LLM E2E dogfooding, graph/report/docs/health GUI tabs, Excel export, voice (STT/TTS), security deep-audit fixes, OpenAPI/env-example integrity gates. Round notes live in [`docs/04-eval/`](docs/04-eval/). |

For per-PR detail, see the round notes in [`docs/04-eval/`](docs/04-eval/),
[`docs/operator-guide/phase2_backlog.md`](docs/operator-guide/phase2_backlog.md),
[`docs/operator-guide/score-evidence.md`](docs/operator-guide/score-evidence.md),
and `git log`.

**Current state**: 1,598 unit + 16 integration tests, `ruff check` clean,
24 alembic migrations (`0001` → `0024`), 20 MCP tools, 6 analyzer images,
19 dashboard pages, full Korean i18n surface, spec §2 (10 of 10 principles)
preserved end-to-end.

## Tests

```bash
cd server
pytest -m "not integration"    # 1,598 unit tests, no external services
pytest                         # full suite — adds 16 integration tests
                               # (requires Postgres + Redis)
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
