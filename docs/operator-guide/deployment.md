# Mnemos — Operator Deployment Guide

This guide walks an operator through bringing up a single-organisation,
self-hosted Mnemos beta instance on a Linux host using Docker Compose. It is an
operator bring-up guide, not evidence of production or large-repository
qualification.

## 1. Prerequisites

- Linux host (x86_64) with Docker 24+ and Docker Compose v2
- No validated RAM capacity formula yet. Start with at least 8 GB for a small
  evaluation and measure peak worker/analyzer RSS on the target repository.
- Disk for Postgres, source mirrors, detached worktrees, and backups; size from
  the actual repository and graph rather than a fixed unverified allowance.
- A TLS-terminating reverse proxy (nginx / caddy / traefik) — required
  because the platform is published as plain HTTP on host port 16401
- Outbound access to required Git/package registries during build, unless all
  dependencies and source mirrors are pre-staged

## 2. First-time configuration

```bash
git clone <this repo>
cd Mnemos
cp .env.example .env
```

Fill in `.env`:

```env
# Required — generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
FERNET_KEY=<48-byte base64>
# Required — any 32+ byte random string; used to sign session cookies
SECRET_KEY=<openssl rand -hex 32>
# Optional overrides
POSTGRES_USER=mnemos
POSTGRES_PASSWORD=<strong password — do not leave default>
POSTGRES_DB=mnemos
PLATFORM_PORT=16401
LOG_LEVEL=INFO
# Absolute host repository for a manual run; mounted read-only as /work
MNEMOS_SOURCE_ROOT=/absolute/path/to/source-repo
```

**Do not** commit the populated `.env`. Rotate `FERNET_KEY` only via the
key-rotation procedure (see §6) — rotating it naively makes every stored
secret unreadable.

## 3. Launch

```bash
docker compose up -d --build
docker compose exec platform alembic upgrade head
```

Wait for `docker compose ps` to show all four services healthy.

Smoke-test:

```bash
curl -fsS http://localhost:16401/api/v1/health          # liveness
curl -fsS http://localhost:16401/api/v1/health/ready    # deep check (DB, Redis, worker)
```

`/health/ready` returns 503 until the worker has written its first
heartbeat (~15 seconds after startup).

## 4. Create the first admin user

The platform does not self-register users in Phase A. Create the
bootstrap admin from the platform container:

```bash
docker compose exec platform python -m app.cli create-user \
  --username admin \
  --role admin
# you will be prompted for a password
```

Subsequent users — including `operator` and `viewer` roles — should be
created via the Settings tab in the dashboard (Phase B) or the same CLI.

## 5. Reverse proxy / TLS

Example nginx block (production should terminate TLS at the proxy):

```nginx
server {
    listen 443 ssl http2;
    server_name mnemos.example.com;

    ssl_certificate     /etc/letsencrypt/live/mnemos.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/mnemos.example.com/privkey.pem;

    # Session cookies require HTTPS in production — update app.auth.sessions
    # if you are wiring a different auth proxy.
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Request-Id      $request_id;

    client_max_body_size 50M;
    proxy_read_timeout   120s;

    location / {
        proxy_pass http://127.0.0.1:16401;
    }
}
```

Caddy equivalent:

```caddy
mnemos.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:16401 {
        header_up X-Request-Id {http.request.uuid}
    }
}
```

## 6. Key rotation

Rewrap every stored secret from the old Fernet key to a new one. Safe to
run live; prefer a maintenance window anyway so a newly-created secret
isn't missed mid-rotation.

```bash
# 1. Generate a new key.
NEW_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

# 2. Dry run first — confirm every secret decrypts with the current key.
docker compose exec platform python -m app.cli rotate-fernet-key \
  --old-key "$(grep ^FERNET_KEY= .env | cut -d= -f2-)" \
  --new-key "$NEW_KEY" \
  --dry-run

# 3. Commit the rotation.
docker compose exec platform python -m app.cli rotate-fernet-key \
  --old-key "$(grep ^FERNET_KEY= .env | cut -d= -f2-)" \
  --new-key "$NEW_KEY"

# 4. Update .env and restart so new encryptions use the new key.
sed -i "s|^FERNET_KEY=.*|FERNET_KEY=$NEW_KEY|" .env
docker compose up -d
```

If the CLI reports `skipped > 0`, at least one row failed to decrypt
with the supplied old key — **do not** flip `.env` until you investigate
(the row is likely corrupted or was encrypted with a prior key).

## 7. Backups

- Postgres: `scripts/backup.sh` dumps the DB in custom (`-Fc`) format and
  prunes anything older than `MNEMOS_BACKUP_RETENTION_DAYS` (default 14).
  Wire via cron or a systemd timer:

  ```cron
  15 3 * * * cd /opt/mnemos && ./scripts/backup.sh /var/backups/mnemos >> /var/log/mnemos-backup.log 2>&1
  ```

- Restore: `MNEMOS_RESTORE_CONFIRM=yes ./scripts/restore.sh <file>`.
  The script stops platform+worker, drops the DB, runs `pg_restore`,
  restarts, and prints the resulting alembic revision so you can confirm
  it matches the image on disk.

- Redis: not authoritative; loss just empties queues and rate-limit
  counters. RDB snapshots in the bind mount are sufficient.
- `platform_data` volume: contains repo checkouts — rebuildable from
  GitLab. Low priority.

## 8. Observability

- Structured JSON logs on stdout. Ship with your log aggregator.
- `x-request-id` header is emitted on every response and in each log
  line; forward it from the proxy so user-visible ids match the logs.
- `/api/v1/health/ready` returns per-dependency status; wire into your
  load balancer's drain logic.
- Prometheus metrics at `/metrics`. Spin up the optional monitoring
  stack with:

  ```bash
  docker compose -f docker-compose.yml -f docker-compose.monitoring.yml \
    --profile monitoring up -d
  ```

  Grafana at `http://<host>:16402` (admin/admin by default — change via
  `GRAFANA_ADMIN_PASSWORD`), dashboard *Mnemos Overview* pre-provisioned.
- Baseline metrics to alert on:

  | Metric | Alert when |
  |---|---|
  | `rate(mnemos_http_requests_total{status="5xx"}[5m])` | > 0.5 rps for 10m |
  | `histogram_quantile(0.95, …_duration_seconds_bucket[5m])` | > 2s for 10m |
  | `increase(mnemos_rate_limited_total[1h])` | > 100 / h |
  | `absent(mnemos_http_requests_total)` | for 5m (scrape failure) |

## 9. Rate limits

Default caps (Phase A):

| Endpoint                        | Limit         |
|--------------------------------|---------------|
| `POST /projects/{id}/data/query`  | 30 / 60s / user |
| `POST …/data_entities/.../refresh_sample` | 20 / 60s / user |

Anonymous clients are bucketed by IP. Tuning belongs in operator
settings (Phase B).

## 10. Upgrades

The graph-publication migrations require a maintenance-window upgrade. An old
worker writes directly to current Node/Edge rows, while the new worker writes
run-scoped staging and advances `GraphHead`; they must never overlap.

1. Stop webhook/manual analysis ingress and record all `queued`, `running`, and
   `published` run IDs. Let the old workers drain; do not hard-stop a worker
   while it still reports a running mutation.
2. Stop **both** old `platform` and `worker` containers. Verify no old worker
   process remains, then take and test a PostgreSQL backup.
3. Pull/build one immutable image tag and use that same tag for platform,
   worker, and the one-off `alembic upgrade head`. Keep application workers
   down while migrations 0027+ create/backfill graph heads, staging, durable
   overlays, publication lifecycle, derived-product markers, and the
   Plan/review/sample source-revision provenance fences.
4. Start the new platform and worker. Do not re-enable ingress yet. Confirm
   `alembic heads` has one head and `/api/v1/health/ready` is green for at least
   one heartbeat interval.
5. Existing pre-publication graphs are deliberately marked `needs_rebuild`.
   For every project, run an explicit authoritative `scope=full` analysis with
   `summarize=false` and `agent_extract_limit=0` first. All required producer
   binaries must be available; an incomplete producer cannot authorize the
   rebuild. Verify a ready head whose `current_run_id`, generation, timestamp,
   and atomic receipt agree. A `partial` run still has a usable source receipt,
   but its recorded post-processing error must be reviewed before treating
   findings/summaries as complete.
6. Re-enable webhook/manual analysis ingress only after those checks. Keep
   automatic bitemporal graph-history pruning disabled; safe pruning needs a
   retained-from watermark/history revision that is not implemented yet.

Rollback across this boundary means stopping every new process and restoring
the tested pre-upgrade database backup together with the previous image. Do
not run an old worker against the migrated schema, and do not treat a blind
Alembic downgrade after new publications as a data-safe rollback.

### 10a. Exceptional schema-only downgrade

The supported rollback is still the backup restore above. A schema-only
downgrade through 0034-0028 is a **data-retirement procedure**, not an
equivalent rollback. Run it only with ingress, `platform`, and every worker
stopped, after taking a second tested backup. Use a normal libpq PostgreSQL URL
as `PGURL`; the application's `postgresql+asyncpg://` URL is not accepted by
`psql`/`pg_dump`.

Inspect all seven fail-closed boundaries before attempting the downgrade:

```sql
-- 0028: any non-zero count blocks the downgrade.
SELECT 'node_human' AS state, count(*) FROM graph_node_human_overlays
UNION ALL SELECT 'edge_human', count(*) FROM graph_edge_human_overlays
UNION ALL SELECT 'edge_runtime', count(*) FROM graph_edge_runtime_overlays
UNION ALL SELECT 'runtime_cursor', count(*) FROM graph_edge_runtime_cursors;

-- 0029: either status is unknown to the older worker.
SELECT id, project_id, status, stats->'graph_publication' AS receipt,
       error_log
FROM analysis_runs
WHERE status IN ('published', 'partial');

-- 0030: the older scalar reader permits one unsuperseded row per key.
SELECT project_id, target_id, level, array_agg(id ORDER BY generated_at, id) AS ids
FROM summaries
WHERE superseded_by IS NULL
GROUP BY project_id, target_id, level
HAVING count(*) > 1;

-- 0031: the older upsert permits one row per legacy identity bucket.
SELECT project_id, kind, subject_node_id,
       array_agg(id ORDER BY first_seen_at, id) AS ids
FROM findings
GROUP BY project_id, kind, subject_node_id
HAVING count(*) > 1;

-- 0032: the older Plan workflow cannot preserve source/worktree provenance.
SELECT id, project_id, status, source_run_id, source_git_sha,
       source_graph_generation, source_overlay_generation, worktree_path
FROM plans
WHERE source_run_id IS NOT NULL OR source_git_sha IS NOT NULL
   OR source_graph_generation IS NOT NULL
   OR source_overlay_generation IS NOT NULL;

-- 0033: the older Gate-B workflow can approve an unbound verdict/grant.
SELECT 'submission' AS kind, id, review_run_id, review_git_sha,
       review_source_generation, review_overlay_generation
FROM diff_submissions
WHERE review_run_id IS NOT NULL OR review_git_sha IS NOT NULL
   OR review_source_generation IS NOT NULL
   OR review_overlay_generation IS NOT NULL
UNION ALL
SELECT 'grant', id, review_run_id, review_git_sha,
       review_source_generation, review_overlay_generation
FROM diff_break_glass_grants
WHERE review_run_id IS NOT NULL OR review_git_sha IS NOT NULL
   OR review_source_generation IS NOT NULL
   OR review_overlay_generation IS NOT NULL;

-- 0034: the older sample reader exposes samples across graph/policy changes.
SELECT id, project_id, data_entity_id, source_run_id, source_git_sha,
       source_graph_generation, source_overlay_generation,
       source_project_db_present, source_project_db_id, source_policy_hash
FROM data_samples
WHERE source_run_id IS NOT NULL OR source_git_sha IS NOT NULL
   OR source_graph_generation IS NOT NULL
   OR source_overlay_generation IS NOT NULL
   OR source_project_db_present IS NOT NULL
   OR source_project_db_id IS NOT NULL
   OR source_policy_hash IS NOT NULL;
```

Export the state before making an irreversible disposition. The first dump is
needed for 0028 because human judgements and runtime replay cursors are not a
cache. The CSV files preserve the exact rows that require operator judgement
for 0029-0034.

```bash
pg_dump "$PGURL" --data-only \
  --table=graph_node_human_overlays \
  --table=graph_edge_human_overlays \
  --table=graph_edge_runtime_overlays \
  --table=graph_edge_runtime_cursors \
  --file=mnemos-0028-overlays.sql

psql "$PGURL" -c "\copy (SELECT * FROM analysis_runs WHERE status IN ('published','partial')) TO 'mnemos-0029-runs.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM summaries WHERE superseded_by IS NULL) TO 'mnemos-0030-current-summaries.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM findings) TO 'mnemos-0031-findings.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM plans WHERE source_run_id IS NOT NULL OR source_git_sha IS NOT NULL OR source_graph_generation IS NOT NULL OR source_overlay_generation IS NOT NULL) TO 'mnemos-0032-plans.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM diff_submissions WHERE review_run_id IS NOT NULL OR review_git_sha IS NOT NULL OR review_source_generation IS NOT NULL OR review_overlay_generation IS NOT NULL) TO 'mnemos-0033-submissions.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM diff_break_glass_grants WHERE review_run_id IS NOT NULL OR review_git_sha IS NOT NULL OR review_source_generation IS NOT NULL OR review_overlay_generation IS NOT NULL) TO 'mnemos-0033-grants.csv' CSV HEADER"
psql "$PGURL" -c "\copy (SELECT * FROM data_samples WHERE source_run_id IS NOT NULL OR source_git_sha IS NOT NULL OR source_graph_generation IS NOT NULL OR source_overlay_generation IS NOT NULL OR source_project_db_present IS NOT NULL OR source_project_db_id IS NOT NULL OR source_policy_hash IS NOT NULL) TO 'mnemos-0034-samples.csv' CSV HEADER"
```

Resolve each blocker explicitly:

- **0034:** after verifying the sample export, delete revision-bound samples;
  they are a recapturable masked cache, but removing only their provenance
  would let the older reader expose them under a different sensitivity or
  masking policy. Recapture only after a later supported upgrade/rebuild.
- **0033:** export the submission and grant audit rows, then remove grants
  before their submissions. Do not null only the four review fields: that
  converts a deliberately unapprovable legacy row into an apparently valid
  older Gate-B verdict. Restore the backup if those decisions must remain
  actionable.
- **0032:** close any active Plan, preserve its diff/audit export, remove its
  detached worktree, then delete the bound Plan (dependent submissions/grants
  cascade). A worktree cannot be safely rebound to mutable mirror `HEAD`.
- **0031:** for every reported `(project_id, kind, subject_node_id)` group,
  retain one operator-selected row. Preserve a user `false_positive`, manual
  `resolved`, or `acknowledged` disposition in preference to a system-open
  duplicate. Delete other rows only by their reviewed IDs after the full
  finding export; the migration never deletes audit findings for you.
- **0030:** choose one current Summary ID per reported
  `(project_id, target_id, level)` and set each other row's `superseded_by` to
  that ID. Do not delete the narrative merely to satisfy the guard.
- **0029:** inspect each receipt against `graph_heads.current_run_id`,
  `generation`, and `published_at`, plus the post-processing error. There is
  no lossless automatic mapping. Record the original row, then deliberately
  map it to one older terminal status (`completed`, `failed`, or `cancelled`)
  according to the reviewed source-publication and derived-stage outcome.
- **0028:** after the SQL dump is verified, retire rows in dependency order:
  `graph_edge_runtime_cursors`, `graph_edge_runtime_overlays`,
  `graph_edge_human_overlays`, then `graph_node_human_overlays`. Re-upgrading
  later without reconciling the exported cursors with retained
  `runtime_observations` can double-apply runtime counts.

Re-run all seven inspection queries and require zero blocking rows/groups
before `alembic downgrade`. A successful command only proves schema
compatibility with the dispositions above; it does not recreate the exact
pre-upgrade application state. To cross 0027 or to preserve all evidence
without retirement, restore the tested pre-upgrade backup instead.

## 10b. Analyzer binaries

The standard Compose build uses the repository root as its build context.
``server/Dockerfile`` copies ``analyzers/`` into both platform and worker
images, installs the TypeScript dependency and tree-sitter extra, and enables
the bounded in-repo subprocess path. It currently covers:

- Python, TypeScript/JavaScript, C/C++, Java, Kotlin, HTML/CSS/SCSS;
- configured tree-sitter languages (currently Go/Rust/Ruby/PHP/Scala/Swift).

The repository also contains C#, live MSSQL/Oracle, and .NET-binary analyzer
projects. They are **not** installed in the standard worker image. The Compose
``analyzers`` profile builds standalone contract-test images only; building
that profile does not connect those images to ``run_ingest``.

To add one of the unavailable analyzers, install a contract-compatible command
on the worker's ``PATH`` (or extend ``server/Dockerfile`` with the required
runtime and binary), then run its ``probe``, ``inventory``, ``symbols``, and
``calls`` verbs against a labelled fixture. Do not mount the Docker socket and
assume sidecar execution: no supported sidecar adapter exists in the current
runner.

For manual source analysis, set an absolute host path before starting Compose:

```env
MNEMOS_SOURCE_ROOT=/absolute/path/to/source-repo
SOURCE_PROJECT_ROOTS='{"<project-uuid>":"."}'
```

Compose mounts it read-only at ``/work``. Use ``/work`` as the Analysis-tab
source path. ``SOURCE_ALLOWED_ROOT=/work`` is only the filesystem boundary;
``SOURCE_PROJECT_ROOTS`` is the authorization boundary that binds each project
UUID to one relative directory. Missing, empty, malformed, absolute, ``..``,
symlink, and junction mappings reject manual analysis before a run is created.
The API enqueues the canonical path and the worker independently revalidates
the same binding before any file or Git read. A single-repository deployment
uses ``{"<project-uuid>": "."}``; a shared mount uses distinct subdirectories,
for example ``{"<uuid-a>":"team-a", "<uuid-b>":"team-b"}``. Completed Git
runs can therefore reopen the exact analysed commit without persisting a
host-absolute path. For webhook analysis,
separately maintain an exact-SHA mirror at
``SOURCE_MIRROR_ROOT/<project UUID>[.git]``; a URL alone is not source data.
In the standard Compose file this root is ``/var/lib/mnemos/repos`` backed by
the shared ``repos_data`` volume. For example, bootstrap a bare mirror with:

```bash
docker compose exec platform git clone --mirror <git-url> \
  /var/lib/mnemos/repos/<project-uuid>.git
```

An external credentialed sync must run ``git fetch --prune`` before delivering
each webhook (or otherwise guarantee the pushed SHA is present). Mnemos does
not silently fetch a mutable branch during webhook handling.

OTLP ingestion also binds authority to the credential rather than a caller
header. Prefer one unique token per organization:

```env
MNEMOS_OTLP_ORG_TOKENS='{"<organization-uuid>":"<random-32+-character-token>"}'
```

The legacy single-tenant form requires both ``MNEMOS_OTLP_TOKEN`` and
``MNEMOS_OTLP_ORGANIZATION_ID`` and cannot be combined with the map. An optional
``X-Mnemos-Organization-Id`` must equal the token-bound organization. Duplicate
tokens, malformed JSON/UUIDs, partial or ambiguous configuration, and deleted
organization rows reject the trace before payload parsing or buffering.

Gate-A Plan creation is stricter than a read-only analysis. The current
publication must carry a canonical full lowercase Git object ID, that exact
commit must still exist in the project mirror, and the detached Plan worktree
must resolve to it. A manual/non-Git publication, missing mirror, abbreviated
SHA, or pruned commit returns a revision-unavailable 409; Mnemos never falls
back to mutable mirror `HEAD`. Recreate the mirror/commit and publish a fresh
authoritative analysis before recreating the Plan. Legacy unbound Plans remain
visible for audit but cannot be approved or edited.

### Verifying

After the bring-up steps above, run one small labelled repository with
``summarize=false`` and ``agent_extract_limit=0``. Check the stage records and
``producer_coverage`` in run stats, not just process exit. An unavailable or
incomplete producer must be reported as a coverage gap; if no requested
changed producer can run, the run must fail instead of publishing an empty
"completed" graph.

## 11. Multi-tenancy (Phase C — opt-in)

Single-host deployments stay in the ``default`` organisation and nothing
changes. To partition the platform across multiple tenants:

```bash
# 1. Create an additional org.
curl -X POST https://.../api/v1/organizations \
  -H 'content-type: application/json' \
  -d '{"slug":"team-red","display_name":"Team Red"}'

# 2. Provision users with organization_id scoped to that org (via the
#    /api/v1/users CRUD — see api/users.py).
```

**Known scope**: the Phase-C foundation ships the ``organizations``
table, ``same_org()``/``require_project_org()`` helpers, and the org
column on ``projects`` and ``users``. The retrofit of every existing
endpoint to enforce the check is tracked in
``app/auth/org_scope.py`` docstring — mechanical work, one ``Depends``
per route. Until that retrofit lands, treat the org boundary as
best-effort: audits show cross-org access but the API does not yet
block it on every route.

## 12. OIDC / SSO (Phase C)

Set these env vars (leave empty to disable and fall back to local
password auth):

```env
OIDC_ISSUER=https://login.example.com/auth/realms/corp
OIDC_CLIENT_ID=mnemos
OIDC_CLIENT_SECRET=…
OIDC_REDIRECT_URI=https://mnemos.example.com/api/v1/auth/oidc/callback
OIDC_SCOPES="openid email profile"
```

Login flow: ``GET /api/v1/auth/oidc/login`` → IdP → callback lands at
``/api/v1/auth/oidc/callback``. First-time users are provisioned as
``role=viewer`` in the ``default`` org; an admin elevates them.

## 13. KMS backend

``KMS_BACKEND=local`` (default) reads ``FERNET_KEY`` from env — fine for
single-host deployments behind a hardened host.

``KMS_BACKEND=vault`` fetches the DEK from a self-hosted HashiCorp Vault
KV-v2 store at startup. Vault (not a cloud KMS) is the chosen external
option so the platform stays air-gappable. Env vars:

```env
KMS_BACKEND=vault
VAULT_ADDR=http://vault:8200
VAULT_TOKEN=<periodic token or AppRole-issued token>
VAULT_KV_PATH=secret/data/mnemos/kms
# optional; defaults to "fernet_key"
VAULT_KV_KEY=fernet_key
```

Populate the key once:

```bash
vault kv put secret/mnemos/kms \
  fernet_key="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
```

Token renewal and AppRole login are deliberately left to an operator
sidecar — Mnemos only reads the DEK at startup, so a short-lived token
is acceptable as long as the sidecar refreshes it before restarts.

## 14. GDPR endpoints

Admin-only. Available at ``/api/v1/gdpr/users/{user_id}``:

- ``GET …/export`` → JSON dump of user record, API keys, audit entries.
- ``DELETE …`` → deletes the user + rewrites audit entries so the
  actor field becomes ``redacted:<uuid>`` (forensic chain preserved,
  identifiable form removed).

## 15. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 not_authenticated` on all requests | Missing / expired session cookie | Re-login |
| `403 insufficient_role` | Account role lacks required scope | Elevate role or ask an admin |
| `503 rate_limit_unavailable` | Redis down / unreachable | Check `docker compose logs redis` |
| `/health/ready` reports `worker: no_heartbeat` | Worker container died | `docker compose restart worker` |
| Secret test returns `unparseable` | Connection string format not recognised | Use `host:port/service` (Oracle) or `Server=host,port;` (MSSQL) |
