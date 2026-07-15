# Atomic graph publication — Phase B contract and evidence (2026-07-15)

## Status

Phase B is connected to `run_ingest`: analyzer output is isolated by run,
sealed, and promoted through one source-publication transaction. Durable
human/runtime overlays survive physical bitemporal version replacement, and
current readers and derived products are pinned to source and overlay
revisions.

This is an implementation/evidence checkpoint, **not a production-qualification
claim**. The highest recorded evidence level for the combined change is E2.
Real PostgreSQL CI for this branch, hard-kill fault injection, a 50 K-file
representative soak, and a live-provider canary have not been run.

The earlier [Phase-A report](atomic-graph-publication-phase-a-2026-07-15.md) is
a historical design checkpoint. The current delivered architecture is in
[`docs/architecture.md`](../architecture.md).

## 1. Contract map

The publication boundary is a machine-consumed state contract, not just a job
ordering convention.

| Layer | Current contract |
|---|---|
| Outer producer envelope | Bounded analyzer JSONL records plus producer/run context; malformed, oversized, incomplete, or failed producer output cannot gain deletion authority |
| Boundary normalization | `_record_payload` validates and canonicalizes node/edge identities, certainty, provenance, and source-owned data before staging; conflicting aliases/shapes fail closed |
| Canonical persistence | `AnalysisRun` base generation and write-once coverage seal, run-scoped node/edge stage rows, bitemporal Node/Edge rows, `GraphHead`, atomic receipt, durable overlays, derived-product markers, and source-bound Plan/review/sample provenance |
| Consumers | HTTP, MCP, artifacts, source-snapshot reads, findings, summaries, samples, Gate-A/Gate-B mutations, and historical run comparison; current consumers must capture and revalidate source plus overlay generation |

Provider-specific optional narration has its own schema/grounding boundary. It
does not participate in the source publication transaction and cannot promote
LLM prose into a verified graph fact.

## 2. Lifecycle and visibility

`staging` and `sealed` below are internal publication phases, not additional
`AnalysisRun.status` values:

```text
queued → running → run-scoped staging → coverage seal
       → atomic graph promotion + receipt → published
       → runtime overlay → findings → requested summaries
       → completed | partial

queued | running → cancelled | failed       (before publication)
```

- `running`: stage rows may commit, but current graph readers cannot see them.
- `published`: the source generation, `GraphHead`, receipt, and run marker have
  committed together. The source is readable while post-processing continues.
- `completed`: all requested post-publication work completed successfully.
- `partial`: the source receipt remains readable, but at least one
  post-publication stage failed or was cancelled. Findings/summaries must be
  judged from their revision markers and recorded post-process status.
- pre-publication `failed` or `cancelled`: the prior ready head remains current.

A stale `published` run is closed as `partial` by recovery only when its atomic
receipt is valid; recovery does not move the source head backwards.

## 3. Atomic source publication

Each source run captures one base graph generation before its first stage
write. Staging stores one canonical candidate per logical identity and records
the producer coverage needed to decide which omissions may close facts. The
coverage seal freezes that deletion authority before promotion.

Promotion then performs one database transaction:

1. lock the project's `GraphHead` and the publishing run;
2. verify the captured base generation and write-once seal;
3. reconcile unchanged, changed, new, and authoritatively omitted Node/Edge
   candidates at one publication timestamp;
4. materialize the latest durable overlays on replacement physical versions;
5. advance the source generation and ready head;
6. persist the canonical atomic receipt and set the run to `published`.

The base-generation check prevents two runs from both publishing from the same
stale base. Replaying a run whose valid receipt already owns the head returns
the existing publication result instead of creating another generation.
Failures before this commit leave the old head intact. Network calls,
analyzers, runtime reconciliation, finding detectors, and LLM calls are kept
outside the publication transaction.

## 4. Durable overlays and derived revisions

`GraphHead.generation` is the source revision.
`GraphHead.overlay_generation` is the durable human/runtime evidence revision.
Human review and runtime-observation writers update logical-identity overlay
rows under the head lock, materialize the result onto the current physical
row, and advance the overlay generation once per unit of work. Source
promotion strips overlay-owned fields from analyzer hashes and re-materializes
the durable overlay, so a new bitemporal version does not erase human or
runtime evidence.

Current readers capture a `(generation, overlay_generation, current_run_id)`
stamp and revalidate it before returning. A concurrent publication or overlay
advance returns a structured retry error instead of a mixed READ COMMITTED
view.

At the repository's current Alembic head, findings and summaries are current
only when their stored source and overlay validation markers match the ready
head. A head change therefore hides old derived rows immediately; it does not
present stale prose or findings while regeneration is pending. Summary cache
reuse additionally requires a matching evidence hash. Finding rebuild locks
one head, stamps every surviving logical finding, and resolves graph-derived
rows that disappeared from that revision.

Every current serializer and grounding boundary projects the same durable
overlay view: `certainty` is effective, analyzer-owned `source_certainty`
remains visible, and human/runtime evidence is included in Summary input and
cache identity. A confirmation therefore resolves an unverified-claim finding
and invalidates affected summary evidence instead of merely changing a UI
badge.

Graph-derived side effects carry the same fence. A Plan records the canonical
publication run, full Git object ID, source generation, and overlay generation;
its detached worktree must resolve to that exact commit. Gate-B submissions
and break-glass grants record the review revision and are unapprovable after a
head change. Data samples record the exact run, Git object ID, source and
overlay generations, plus a canonical hash of the effective ProjectDB binding,
masking rules, sensitive-table set, and masking-engine policy version. HTTP and
MCP readers revalidate both graph and policy stamps immediately before return;
a graph, binding, or masking-policy change therefore hides the old sample.
Legacy null-marker rows are preserved for audit but fail closed as current
authorisation.

## 5. Historical comparison fails closed

`compare_runs` accepts only terminal source publications whose canonical
receipt agrees with the run's base generation, new generation, coverage seal,
authoritative sources, and publication timestamp. It uses the immutable
publication time to select bitemporal facts; `created_at` and a later
post-process `completed_at` are not source snapshot boundaries.

The current comparison reports finding deltas as unavailable because findings
are mutable current products, not immutable per-publication history. It does
not fabricate a historical finding set. Explicit historical source reads must
also resolve immutable run provenance. If the Git object/archive is gone, the
read fails closed.

Automatic Node/Edge history pruning is disabled. There is no retained-from
watermark yet, so pruning could otherwise turn a missing old generation into a
plausible but incomplete comparison.

## 6. Upgrade and recovery contract

Old direct-writer workers and new staging workers must not overlap. Operators
must drain analysis ingress and old workers, stop platform and worker
processes, back up PostgreSQL, migrate to the **current Alembic head**, and
start one immutable application image set. Pre-publication graphs are marked
`needs_rebuild`; an authoritative `scope=full` run with every required
producer complete is the only trusted transition to a ready head.

Do not use a blind Alembic downgrade as rollback after new publications.
Restore the tested pre-upgrade database together with the previous image. The
full procedure is [deployment §10](../operator-guide/deployment.md#10-upgrades).

## 7. Evidence gate (E0–E4)

These levels describe evidence for the combined state/schema/runtime contract.
They are not a general product score.

| Level | Status | Recorded evidence and limit |
|---|---|---|
| E0 — static contract | **Pass, local** | Model/migration constraints, state invariants, lint/compile and diff checks for the implemented boundary; this does not execute a service-backed workflow |
| E1 — unit behavior | **Pass, local** | Stage isolation, write-once seal, base-generation conflict, idempotent replay, invalid lifecycle combinations, overlay materialization, identity-map refresh, and revision/currentness checks |
| E2 — mock/offline integration | **Pass, local** | SQLite integration, mock worker/provider/reader concurrency, structured-flow replay, and offline PostgreSQL SQL compilation across focused publication, overlay, lifecycle, source-reader, finding, summary, flow, artifact, and comparison suites |
| E3 — live boundary canary | **Not run** | No recorded real PostgreSQL/Redis CI run for the combined branch, hard process-kill/restart exercise, or live-provider canary |
| E4 — representative workflow | **Not run** | No current-build 50 K-file representative repository soak, production-like upgrade rehearsal, or end-to-end measured provider/consumer workflow |

Focused local suites include the graph-publication Phase-A/Phase-B red-team,
graph-overlay, publication-lifecycle, MCP/current-reader, source-snapshot,
summary/finding-currentness, flow-contract, artifact, and run-comparison tests.
Their names are stable evidence pointers; this report intentionally does not
freeze a test count or claim a final full-suite/CI total.

## 8. Remaining limitations

1. Authoritative omission reconciliation still performs O(graph) database
   work. Candidate reads are bounded, but a 50 K-file/PostgreSQL performance
   claim has not been established.
2. Staging stores a canonical union of `created_by` producers, not durable
   per-producer contribution history. Conservative all-owner authority is
   required before shared facts can be closed.
3. The coverage seal proves the persisted run contract, but is not a signed
   remote analyzer attestation.
4. Worker/project-lock recovery still lacks a database fencing lease for fast
   takeover after a hard process kill.
5. Historical source availability depends on retained Git objects or a future
   immutable source archive.
6. Safe history pruning needs a retention watermark and explicit unavailable
   response contract before it can be enabled.
7. Optional flow narration is source-pinned structured hypothesis, not a
   verified graph fact; live-provider quality remains E3-unverified.
8. GitLab MR creation is an external side effect. Revision locks prevent a
   stale approval, but exactly-once delivery still depends on the existing
   GitLab/idempotency behavior rather than a transactional outbox.

## 9. Code and operator pointers

- Publication and read stamps: `server/app/graph_publication.py`
- Durable overlays: `server/app/graph_overlays.py`, `server/app/merge/runtime.py`
- Lifecycle and recovery: `server/app/orchestrator/jobs.py`,
  `server/app/orchestrator/cron_jobs.py`
- Derived currentness: `server/app/merge/findings.py`,
  `server/app/extractor/runner.py`
- Current/historical readers: `server/app/mcp/server.py`,
  `server/app/mcp/queries.py`, `server/app/source_snapshot.py`
- Operator first run: [getting started](../operator-guide/getting-started.md)
- Drain/migrate/rebuild: [deployment §10](../operator-guide/deployment.md#10-upgrades)
