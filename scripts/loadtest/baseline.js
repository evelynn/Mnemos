// k6 baseline load scenario.
//
// Run against a test-only stack (never production):
//   BASE_URL=http://localhost:16401 \
//   MNEMOS_SESSION=<cookie value from /api/v1/auth/login> \
//   k6 run scripts/loadtest/baseline.js
//
// Validates that the Phase A rate limits hold:
//   /data/query  30/min/user    -> half of VUs should hit 429s at 50 rps
//   /health      unlimited      -> stays at p95 < 200ms under load
//
// Thresholds are deliberately loose — this is a saturation check, not
// an SLO. Tighten once you have a real baseline from your environment.

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Counter } from "k6/metrics";
import { loginOnce, authCookie } from "./login.js";

const BASE = __ENV.BASE_URL || "http://localhost:16401";
// Legacy fallback: a raw session cookie can still be supplied via
// MNEMOS_SESSION. Newer runs should set MNEMOS_USERNAME/MNEMOS_PASSWORD
// so setup() handles the login flow itself.
const SESSION = __ENV.MNEMOS_SESSION || "";

export function setup() {
  if (SESSION) return { cookie: SESSION };
  return loginOnce();
}

export const options = {
  scenarios: {
    health_background: {
      executor: "constant-arrival-rate",
      rate: 20,
      timeUnit: "1s",
      duration: "2m",
      preAllocatedVUs: 10,
      exec: "healthTraffic",
    },
    query_saturation: {
      executor: "ramping-arrival-rate",
      startRate: 5,
      timeUnit: "1s",
      stages: [
        { target: 20, duration: "30s" },
        { target: 50, duration: "30s" },
        { target: 50, duration: "1m" },
        { target: 0,  duration: "10s" },
      ],
      preAllocatedVUs: 30,
      exec: "queryTraffic",
      startTime: "10s",
    },
  },
  thresholds: {
    "http_req_duration{endpoint:health}": ["p(95)<200"],
    "http_req_failed{endpoint:health}":   ["rate<0.01"],
    // Expect rate limiting to kick in; <95% of /data/query should NOT fail.
    "http_req_failed{endpoint:query}":    ["rate<0.95"],
  },
};

const rateLimitHits = new Counter("rate_limit_hits");
const queryDuration = new Trend("query_duration_ms");

export function healthTraffic() {
  const r = http.get(`${BASE}/api/v1/health`, {
    tags: { endpoint: "health" },
  });
  check(r, { "health 200": (res) => res.status === 200 });
  sleep(0.1);
}

export function queryTraffic(data) {
  const headers = {
    "Content-Type": "application/json",
    ...authCookie(data),
  };

  const body = JSON.stringify({
    db_component_id: "db.mssql.LoadTest",
    columns: ["id", "name"],
    rows: [[1, "alpha"], [2, "beta"]],
    sql: "SELECT id, name FROM Dummy",
    purpose: "loadtest",
  });

  const r = http.post(
    `${BASE}/api/v1/projects/00000000-0000-0000-0000-000000000000/data/query`,
    body,
    { headers, tags: { endpoint: "query" } }
  );
  queryDuration.add(r.timings.duration);
  if (r.status === 429) rateLimitHits.add(1);
}

export function handleSummary(data) {
  const hits = data.metrics.rate_limit_hits
    ? data.metrics.rate_limit_hits.values.count
    : 0;
  return {
    stdout: `\nrate_limit_hits=${hits}\n\n` + textSummary(data, { indent: " ", enableColors: true }),
  };
}

// Minimal textSummary copied from k6/docs — avoids network pull of the helper.
function textSummary(data, options) {
  const indent = options && options.indent ? options.indent : "";
  const lines = [];
  for (const [name, m] of Object.entries(data.metrics)) {
    const v = m.values;
    lines.push(
      `${indent}${name.padEnd(48)} ` +
        Object.entries(v).map(([k, x]) => `${k}=${typeof x === "number" ? x.toFixed(2) : x}`).join(" ")
    );
  }
  return lines.join("\n");
}
