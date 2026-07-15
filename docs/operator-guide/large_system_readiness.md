# Large-system analysis readiness

A snapshot of evidence and gaps for the source-analysis workflow, updated
2026-07-15. The old Phase-1 target (C# + TS + MSSQL/Oracle in ≤8 hours) is not
a measured capacity result. This document separates fixture/mock evidence from
real-repository and production-environment evidence; it no longer assigns a
readiness percentage.

## Analyzer accuracy benchmark (PR-66)

"Analyzer accuracy can't be verified without a staging environment"
conflated a production *deployment* with *verification*. The
static-analysis literature (NIST SAMATE / CAS tool evaluations)
settles the question: you measure an analyzer against a *labelled
corpus* with a known answer key and score precision / recall.

``server/tests/fixtures/refsys/`` is that corpus — a small but
realistically-shaped polyglot system: a TypeScript BFF that calls a
C# API over HTTP and also reads/writes the database directly.
``ground_truth.json`` is the answer key. ``test_pr66_refsys_
benchmark.py`` runs the **real** TypeScript analyzer over it, joins
the result against the C# analyzer's recorded output, and scores it:

| Metric | Measured | Threshold |
|---|---|---|
| Contract extraction precision | 1.00 | ≥ 0.95 |
| Contract extraction recall | 0.75 | ≥ 0.70 |
| Data-entity extraction precision / recall | 1.00 / 1.00 | ≥ 0.95 / 1.00 |
| Cross-language Contract joins (spec §2.2) | 3 | ≥ 3 |

Recall is deliberately below 1.0: the corpus includes a dynamic
template-literal URL that static analysis genuinely cannot resolve,
so the figure reflects the real limitation rather than a curated
best case. The benchmark is a CI regression guard — an analyzer
change that drops a contract or invents a spurious one fails it.

**OTLP runtime replay (PR-68).** ``runtime-trace.otlp.json`` is a
recorded OTLP/HTTP trace of the same reference system;
``test_pr68_otlp_replay.py`` replays it through ``assemble_trace_
tree`` and scores the ``(service, operation, kind)`` extraction
against ``runtime_ground_truth.json`` (precision/recall 1.00). It
also proves the trace's exposed API operation resolves to the very
``http.GET./api/orders`` contract the static benchmark finds — so a
§7.6 reconcile would mark that contract ``exercised``. This is the
"live OTLP collector" gap closed with a recorded fixture, the same
way OpenTelemetry's own conformance suite works.

## Headline

* **End-to-end analysis pipeline**: JSONL subprocess → validation → run-scoped
  staging → sealed producer coverage → atomic graph-head publication has local
  fixture/mock integration evidence. The standard Compose worker
  now carries the in-repo Python/TS/JS/C/C++/Java/Kotlin/Web/tree-sitter path.
  C#, live DB, and .NET-binary analyzers are not part of that worker image, so
  historical image-build tests do not prove those languages in the normal run.
* **L0 extract → graph/MCP index** is the default complete product and uses
  zero LLM tokens. L1-L3 narration and uncovered-language AI extraction are
  independent explicit options; a missing AI backend does not degrade L0.
* **GitLab webhook → ARQ enqueue** is fail-closed: HMAC + dedup, fixed worker
  queue, and an operator-managed mirror containing the exact pushed SHA.
  Without `SOURCE_MIRROR_ROOT`, the response says
  `source_mirror_not_configured` and creates no queued ghost.
* **PII masking**: 11 patterns × 11 column-name keywords + Korean
  validators (RRN / foreigner ID / driver's licence / Luhn).
  Verified on the 10 000-row payload that hits the
  ``MNEMOS_MAX_ROWS`` clamp.
* **Multi-operator workflow**: viewer / operator / admin RBAC,
  ``submit_diff`` requires operator (no viewer can burn ultrareview
  cycles), break-glass grant 15-min TTL, initiator ≠ approver.

## What's *not* yet verified end-to-end

* **GitLab MR creation flow**. ``create_mr_from_worktree`` is fully
  implemented and **PR-33 added a mock-based integration test**
  covering the happy path (python-gitlab SDK returns a fresh MR),
  the not-configured short-circuit, a git-step failure (preserves
  the command output in ``MRResult.message``) and a python-gitlab
  exception (auth / network / project-not-found). Live-server tests
  still belong in Phase 3 — that needs a real GitLab dev instance
  in CI.
* **Scale**. The ``perf_indexes`` migrations added the hot-path
  indexes for million-node graphs. **PR-34 added synthetic scale
  tests** that bound the hot paths: ``AnalyzerRunner`` drains 10 000
  JSON records in <30 s, ``mask_rows`` handles 50 000 rows × 5
  columns in <10 s and scales linearly with row count (a quadratic
  regex regression would blow the budget), the masker redacts a
  dense PII document with 55 patterns in one pass, and 20
  parallel subprocess spawns complete in <15 s. An external real-repo run
  has also produced 11,391 symbols and 57,491 CALLS with 25/25 MCP checks;
  a controlled unseen-repo break-even/soak study is still future work.
* **Crashed-worker recovery.** Before source publication, a stale ``running``
  run is terminalised and its staging cannot affect the current graph. After
  publication, the immutable receipt/head pair keeps the source generation
  readable while recovery may resume only the derived post-processing work.
  This is covered locally; a real process kill against PostgreSQL remains an
  explicit evidence gap.

## Five operator scenarios — current state

### Scenario 1 — register → first MR

```
GUI: register a project
  → /api/v1/projects (operator role)            ✓
  → ProjectDB binding (admin role + db_probe)   ✓
GitLab push event
  → webhook HMAC verified                       ✓
  → exact SHA present in configured mirror      required
  → fixed-queue ARQ job enqueued                 ✓
  → analysis_run row created                    ✓
  → AnalyzerRunner spawns the binary            ✓
  → JSONL records → run-scoped stage rows       ✓ (local contract tests)
  → coverage seal + atomic GraphHead receipt    ✓ (local contract tests)
  → findings rebuild                            ✓
  → diff submission                             ✓
  → ultrareview pipeline                        ✓
  → MR creation (python-gitlab)                 ⚠ untested
```

### Scenario 2 — large mono-repo (50 K files, 3 languages)

```
Analyzer stdout/stderr queue = 256 records       ✓
JSONL record cap = 1 MiB                         ✓
Analyzer wall timeout + terminate/kill           ✓
Seen identities: 100K RAM then temp-disk spill   ✓
Deletion sweep reads current rows in pages       ✓
Per-stage time budget: 1800 s (jobs.py)         ✓
Unavailable producer recorded; empty success denied ✓ (focused contract tests)
Row-cap clamp at 10 000 on data queries         ✓ (test_e2_*)
Masker stays correct at 10K rows                ✓ (PR-32)
```

The runner now has hard memory/output/time boundaries, but a 50 K-file
multi-language soak is not yet evidence-backed. Treat the first run as a
calibration exercise and do not claim a capacity number before measuring it.

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
  - partial JSONL records isolated in staging    ✓
  - stage finishes with error_log set            ✓
  - old published graph remains readable         ✓ (local concurrency tests)
Worker crashes mid-run
  - heartbeat key goes stale                     ✓
  - /health/ready surfaces 503                   ✓
  - pre-publish stale run cannot move GraphHead  ✓
  - post-publish receipt remains readable        ✓ (local lifecycle tests)
ARQ Redis connection drop
  - asyncpg reconnect                            ✓ (pool)
  - source staging is not exposed on retry       ✓
Postgres connection drop
  - request-level 503                            ✓ (uniform handler)
  - transaction rollback preserves old head      ✓ by contract; real fault injection pending
```

### Scenario 5 — multi-operator concurrency

```
RBAC tiers (viewer/operator/admin)               ✓
viewer cannot submit_diff                        ✓ (PR-18)
operator cannot grant break-glass                ✓
admin cannot self-approve own grant              ✓
break-glass TTL = 15 min                         ✓ (PR-32 pin)
single graph mutation per worker                 ✓
database head CAS + run/project provenance       ✓ (local; real PostgreSQL CI pending)
cross-tab onboarding state sync                  ✓ (PR-28)
cross-tab SSE strip                              ✓ (PR-23)
```

## What new operators should expect on day 1

1. Register the project through the GUI (Projects tab).
2. Optionally add ProjectDB bindings only when source-impact analysis needs
   schema evidence; source indexing does not require a database connection.
3. Trigger the default **scope: incremental**. With no completed baseline it
   selects every relevant producer and builds the deterministic index with zero
   LLM tokens; later runs skip unchanged families. Reserve **full** for explicit
   repair/reconciliation, and enable narration only for an evaluation/use case.
4. Watch the Pipeline monitor tab. SSE retries up to 6 times
   with exponential backoff + jitter before giving up.
5. When findings render, review them; click "Submit diff" to take
   a fix through ultrareview; an admin approves with a break-glass
   token if the diff is blocked.

Do not estimate runtime from KLOC alone. Record analyzer-family file counts,
bytes, stage time, and graph rows. A same-content incremental run hashes the
source then spawns no analyzer; a changed family re-walks that family today.
