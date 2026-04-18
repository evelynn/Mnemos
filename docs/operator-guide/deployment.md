# Mnemos — Operator Deployment Guide

This guide walks an operator through bringing up a single-organisation,
self-hosted Mnemos instance on a Linux host using Docker Compose.

## 1. Prerequisites

- Linux host (x86_64) with Docker 24+ and Docker Compose v2
- 8 GB RAM minimum, 16 GB recommended for analyses over 100k-LOC repos
- 50 GB disk for Postgres + repo checkouts
- A TLS-terminating reverse proxy (nginx / caddy / traefik) — required
  because the platform serves plain HTTP on port 8080
- Outbound network access to your GitLab and the language analyzer
  registries if you are not pre-baking images

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
PLATFORM_PORT=8080
LOG_LEVEL=INFO
```

**Do not** commit the populated `.env`. Rotate `FERNET_KEY` only via the
key-rotation procedure (see §6) — rotating it naively makes every stored
secret unreadable.

## 3. Launch

```bash
docker compose up -d
docker compose exec platform alembic upgrade head
```

Wait for `docker compose ps` to show all four services healthy.

Smoke-test:

```bash
curl -fsS http://localhost:8080/api/v1/health          # liveness
curl -fsS http://localhost:8080/api/v1/health/ready    # deep check (DB, Redis, worker)
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
        proxy_pass http://127.0.0.1:8080;
    }
}
```

Caddy equivalent:

```caddy
mnemos.example.com {
    encode zstd gzip
    reverse_proxy 127.0.0.1:8080 {
        header_up X-Request-Id {http.request.uuid}
    }
}
```

## 6. Key rotation (Phase B follow-up)

A proper rotation rewraps every ciphertext column with the new Fernet
key. Until that script lands, treat `FERNET_KEY` as immutable once
secrets are stored. If the key leaks, rotate all underlying DB
credentials (which is what you would have to do anyway).

## 7. Backups

- Postgres: `pg_dump` the `mnemos` database on a cron. Store off-host.
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

## 9. Rate limits

Default caps (Phase A):

| Endpoint                        | Limit         |
|--------------------------------|---------------|
| `POST /projects/{id}/data/query`  | 30 / 60s / user |
| `POST …/data_entities/.../refresh_sample` | 20 / 60s / user |

Anonymous clients are bucketed by IP. Tuning belongs in operator
settings (Phase B).

## 10. Upgrades

1. `docker compose pull` (or rebuild: `docker compose build`)
2. `docker compose up -d`
3. `docker compose exec platform alembic upgrade head`
4. Verify `/api/v1/health/ready` stays green for one heartbeat interval.

Rollbacks: re-deploy the previous image tag; migrations support
`alembic downgrade <revision>` but review each migration's downgrade
path in advance.

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401 not_authenticated` on all requests | Missing / expired session cookie | Re-login |
| `403 insufficient_role` | Account role lacks required scope | Elevate role or ask an admin |
| `503 rate_limit_unavailable` | Redis down / unreachable | Check `docker compose logs redis` |
| `/health/ready` reports `worker: no_heartbeat` | Worker container died | `docker compose restart worker` |
| Secret test returns `unparseable` | Connection string format not recognised | Use `host:port/service` (Oracle) or `Server=host,port;` (MSSQL) |
