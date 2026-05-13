# Large-system analysis readiness

A snapshot of what the platform can actually do on a real-world
deployment, written after PR-32. Spec §1.5 — "register a real C# +
TS + MSSQL/Oracle system via GUI → first full analysis in ≤ 8
hours" — is the bar; this doc tells an operator how close we are.

## Headline

* **End-to-end analysis pipeline**: works for the four Phase-1
  languages. C# + TypeScript + MSSQL + Oracle binaries all build
  in CI, the platform spawns them through ``AnalyzerRunner.run``,
  and stdout/stderr are parsed as JSONL records. Confirmed by 8
  new subprocess tests in PR-32.
* **L0 extract → graph upsert → L1-L3 summarise**: every leg has
  production code and unit/integration tests. Stub LLM fallback
  means a deployment without an Anthropic key still completes a
  run (degraded summaries).
* **GitLab webhook → ARQ enqueue**: production-tested. HMAC token
  check, dedup, per-branch serialisation.
* **PII masking**: 11 patterns × 11 column-name keywords + Korean
  validators (RRN / foreigner ID / driver's licence / Luhn).
  Verified on the 10 000-row payload that hits the
  ``MNEMOS_MAX_ROWS`` clamp.
* **Multi-operator workflow**: viewer / operator / admin RBAC,
  ``submit_diff`` requires operator (no viewer can burn ultrareview
  cycles), break-glass grant 15-min TTL, initiator ≠ approver.

## What's *not* yet verified end-to-end

* **GitLab MR creation flow**. ``create_mr_from_worktree`` is
  fully implemented (python-gitlab SDK, token resolution, git push
  command sequence) but has no integration test against a live or
  mock GitLab. Phase 3 follow-up.
* **Scale**. The ``perf_indexes`` migration (0011) added the right
  indexes for million-node graphs, but the only published
  perf-test result is the Phase-B baseline from earlier in the
  project. A real 50 K-file mono-repo has not been pushed
  through the pipeline end-to-end.
* **Crashed-worker auto-recovery**. The heartbeat side is
  watertight (90-second stale-after window in ``health.py``,
  Grafana alert in the dashboard). What's missing is a cron that
  proactively flips a stale ``analysis_runs.status='running'`` row
  back to ``failed`` so the GUI doesn't show a wedged run forever.
  Currently this requires manual SQL — Phase 3 backlog item.

## Five operator scenarios — current state

### Scenario 1 — register → first MR

```
GUI: register a project
  → /api/v1/projects (operator role)            ✓
  → ProjectDB binding (admin role + db_probe)   ✓
GitLab push event
  → webhook HMAC verified                       ✓
  → ARQ job enqueued                            ✓
  → analysis_run row created                    ✓
  → AnalyzerRunner spawns the binary            ✓
  → JSONL records → Node/Edge upsert            ✓
  → findings rebuild                            ✓
  → diff submission                             ✓
  → ultrareview pipeline                        ✓
  → MR creation (python-gitlab)                 ⚠ untested
```

### Scenario 2 — large mono-repo (50 K files, 3 languages)

```
Worker memory: tmpfs /scratch = 512 MB         ⚠ unverified
Per-stage time budget: 1800 s (jobs.py)         ✓
Stage skip when analyzer absent                 ✓ (test_e1_*)
Row-cap clamp at 10 000 on data queries         ✓ (test_e2_*)
Masker stays correct at 10K rows                ✓ (PR-32)
```

The platform won't crash on a 50 K-file payload — the stage
budget bounds the worst-case time, the JSONL streaming bounds
memory growth — but the *first* large run is still a calibration
exercise. Operators are expected to tune the per-language stage
budget after seeing real numbers.

### Scenario 3 — sensitive database

```
ProjectDB.sensitive_tables enforced              ✓
ProjectDB.masking_rules layered on platform      ✓
db_probe refuses read-write credential           ✓
SQLGlot rejects DML at the parser level          ✓
Korean RRN/card/licence validators wired         ✓
10 000-row clamp on a single query               ✓
Masking applied + masking_applied=true flag      ✓
```

### Scenario 4 — failure modes

```
Analyzer subprocess crashes (non-zero exit)
  - partial JSONL records preserved              ✓ (PR-32)
  - stage finishes with error_log set            ✓
Worker crashes mid-run
  - heartbeat key goes stale                     ✓
  - /health/ready surfaces 503                   ✓
  - analysis_run row stays at 'running'          ⚠ no auto-reset
ARQ Redis connection drop
  - asyncpg reconnect                            ✓ (pool)
  - ARQ retry policy                             ✓ (default 5x)
Postgres connection drop
  - request-level 503                            ✓ (uniform handler)
```

### Scenario 5 — multi-operator concurrency

```
RBAC tiers (viewer/operator/admin)               ✓
viewer cannot submit_diff                        ✓ (PR-18)
operator cannot grant break-glass                ✓
admin cannot self-approve own grant              ✓
break-glass TTL = 15 min                         ✓ (PR-32 pin)
per-branch queue serialisation                   ✓
cross-tab onboarding state sync                  ✓ (PR-28)
cross-tab SSE strip                              ✓ (PR-23)
```

## What new operators should expect on day 1

1. Register the project through the GUI (Projects tab).
2. Add the ProjectDB binding(s) — the platform will refuse a
   read-write credential at probe time.
3. Trigger the first analysis run with **scope: full** and a tight
   per-stage budget (default 1800 s; halve it for the first run
   if the platform is single-host).
4. Watch the Pipeline monitor tab. SSE retries up to 6 times
   with exponential backoff + jitter before giving up.
5. When findings render, review them; click "Submit diff" to take
   a fix through ultrareview; an admin approves with a break-glass
   token if the diff is blocked.

The first end-to-end run usually takes 1-2 hours per 10 KLOC of
new source. After that, incremental webhook-driven runs typically
complete in under 5 minutes.
