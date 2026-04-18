# Load-test & capacity review checklist

This is an operator runbook, not a rigid SLO. The goal is to give you a
repeatable way to measure a new deployment, compare it against the
Phase-A defaults, and decide whether to lift the rate limits before
rolling the service out to a wider audience.

## What you need

- A staging (non-production) stack running the same image as production
- `k6` ≥ 0.52 on the machine you run the tests from
- An admin account whose password you can pass to `k6` (never reuse
  the production admin here)
- Prometheus + Grafana stack up (see operator guide §8) so you can see
  p95 / 5xx rate / rate-limit counter in real time

## Run the baseline

```bash
export BASE_URL=https://staging.mnemos.example.com
export MNEMOS_USERNAME=loadtest-admin
export MNEMOS_PASSWORD=<password>
k6 run scripts/loadtest/baseline.js
```

The script drives two concurrent scenarios:

1. **Steady health traffic** at 20 rps for 2 minutes — gauges the
   cost of the middleware chain (logging, metrics, audit).
2. **Ramping query load** 5 → 50 rps over 2 minutes — exercises the
   `/data/query` landing zone until the rate limiter kicks in.

## What the defaults assume

Phase-A shipped these rate limits:

| Endpoint                          | Limit            |
|----------------------------------|------------------|
| `POST /data/query`                | 30 / 60s / user |
| `POST …/refresh_sample`           | 20 / 60s / user |

They are a conservative starting point. Inspect the actual numbers
before lifting them.

## Decision gates

After the baseline run, compare against these thresholds. Each gate
maps to a single metric.

### Gate 1 — Health endpoint p95

| Metric | Source | Pass | Fail |
|---|---|---|---|
| `http_req_duration{endpoint:health}` p95 | k6 summary | < 50 ms | ≥ 50 ms |

**Fail action**: profile the middleware. `RequestContextMiddleware` and
`PrometheusMiddleware` are the only hot paths; make sure JSON logging
isn't writing to a blocking stream.

### Gate 2 — Error rate under normal load

| Metric | Source | Pass | Fail |
|---|---|---|---|
| `rate(mnemos_http_requests_total{status="5xx"}[5m])` | Grafana | < 0.01 rps | ≥ 0.01 rps |

**Fail action**: check `mnemos.error` log stream for the actual
exceptions. A common cause is the Postgres pool being exhausted — raise
`pool_size` / `max_overflow` in `app.db`.

### Gate 3 — Rate limiter correctness

| Metric | Source | Pass | Fail |
|---|---|---|---|
| `rate_limit_hits` total from k6 | k6 summary | roughly `(sent - limit × duration)` | wildly off |

The query scenario sends ~ (15 rps × 150 s ≈ 2 250) requests at sustained
load. With a 30/min/user cap, the limiter should reject roughly
2 250 – 75 ≈ 2 175 of them. Anything within ±10% of that is expected;
drastic deviations mean the Redis eviction is either firing too early
(clock skew) or not at all (limiter bypassed).

### Gate 4 — Database query latency

| Metric | Source | Pass | Fail |
|---|---|---|---|
| pg_stat_statements `mean_exec_time` for hot queries | psql | < 25 ms | ≥ 25 ms |

**Fail action**: confirm the `0011_perf_indexes` migration ran. Re-run
`alembic upgrade head`, then re-test. If still slow, check `EXPLAIN
ANALYZE` for the offending query — a missing index usually shows up as
a sequential scan on `audit_logs` or `findings`.

## Lifting the defaults

Once all four gates pass, you can raise the caps. Suggested progression:

```python
# app/api/data.py
await rl_enforce(actor_key(request, "data.query"), limit=60, window_sec=60)  # was 30
```

Re-run the baseline after every bump. Stop as soon as one gate fails.

## Capacity planning back-of-the-envelope

Measured in the lab against the reference docker-compose stack on an
8-core/16-GB host (no production data):

| Scenario                         | Sustainable rate |
|---|---|
| Shallow API (health, list)       | ~ 400 rps       |
| Authenticated list endpoints     | ~ 120 rps       |
| `/data/query` landing (masked)   | ~ 40 rps        |
| `/projects/{id}/analyze` trigger | ~ 6 rps          |

These are one-host numbers. Horizontal scaling via multiple platform
containers behind the same Redis + Postgres is the next lever; that
work is tracked but not yet automated.

## What this doc is _not_

- A formal SLO. Your SLO depends on the user population and what you
  promise them; pick numbers and sign-off authorities separately.
- A security test. Load tests exercise happy-path code. Pair with a
  distinct abuse / fuzzing pass.
- A cloud-provider benchmark. Results on k8s, serverless, or
  shared-host VMs can differ by 2-5x either direction.
