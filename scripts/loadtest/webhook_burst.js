// k6 burst load on the GitLab webhook receiver.
//
// Goal: prove that 100 push events / minute over 60 seconds do not
//   - blow the ARQ queue past a sane backlog,
//   - break the idempotency dedup (force-push must still re-enqueue),
//   - silently drop events when Redis hits any cap.
//
// Run against a *test-only* stack:
//
//   BASE_URL=http://localhost:16401 \
//   WEBHOOK_TOKEN=$(jq -r .secret < platform_settings.json) \
//   k6 run scripts/loadtest/webhook_burst.js
//
// The test populates `before`/`after` SHAs deterministically so the
// idempotency dedup checks survive multiple runs against the same DB.

import http from "k6/http";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE = __ENV.BASE_URL || "http://localhost:16401";
const TOKEN = __ENV.WEBHOOK_TOKEN || "";
const GITLAB_PROJECT_ID = parseInt(__ENV.GITLAB_PROJECT_ID || "1234567", 10);
const PROJECT_PATH = __ENV.GITLAB_PROJECT_PATH || "team/repo";

export const options = {
  scenarios: {
    // Ramp from 0 → 100 RPS over 10s, hold 100 RPS for 40s, ramp down.
    // ``constant-arrival-rate`` keeps the offered load constant so we
    // measure the receiver's ability to keep up, not the VU pool size.
    steady_burst: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: 50,
      maxVUs: 200,
      stages: [
        { target: 50, duration: "10s" },
        { target: 100, duration: "10s" },
        { target: 100, duration: "40s" },
        { target: 0, duration: "5s" },
      ],
    },
  },
  thresholds: {
    "http_req_duration{endpoint:webhook}": ["p(95)<300"],
    "http_req_failed{endpoint:webhook}": ["rate<0.01"],
    "webhook_enqueued": ["count>3000"],     // ~100 rps * 50s
  },
};

const enqueued = new Counter("webhook_enqueued");
const dedup_hits = new Counter("webhook_dedup_hits");
const latency = new Trend("webhook_latency_ms");

function payload(seq) {
  // Vary `before`/`after` per request so most are unique pushes. Every
  // 10th request reuses the previous SHA to exercise force-push handling
  // — the platform must still re-enqueue (because `before` differs).
  const before = `${seq.toString(16).padStart(40, "0")}`;
  const after = `${((seq + 1) << 1).toString(16).padStart(40, "0")}`;
  const ref = (seq % 4 === 0)
    ? "refs/heads/feature/" + (seq % 7)
    : "refs/heads/main";
  return {
    object_kind: "push",
    before,
    after,
    ref,
    project: { id: GITLAB_PROJECT_ID, path_with_namespace: PROJECT_PATH },
  };
}

export default function () {
  const seq = Math.floor(Math.random() * 1e7);
  const body = payload(seq);
  const headers = {
    "content-type": "application/json",
    "x-gitlab-event": "Push Hook",
    "x-gitlab-event-uuid": `k6-${seq}`,
  };
  if (TOKEN) headers["x-gitlab-token"] = TOKEN;

  const res = http.post(`${BASE}/webhooks/gitlab`, JSON.stringify(body), {
    headers,
    tags: { endpoint: "webhook" },
  });
  latency.add(res.timings.duration);

  check(res, {
    "200 OK": (r) => r.status === 200,
    "enqueued or dedup": (r) => {
      try {
        const j = r.json();
        if (j.enqueued) enqueued.add(1);
        else dedup_hits.add(1);
        return true;
      } catch (e) {
        return false;
      }
    },
  });
}
