# Atomic graph publication — Phase A contract (2026-07-15)

> Historical design checkpoint. Phase B is now wired into `run_ingest` with
> run-scoped stage writers, atomic promote-on-success, source/overlay reader
> stamps, and a separate post-publication `completed|partial` lifecycle. The
> remaining production evidence gaps are real PostgreSQL CI/fault injection and
> large-repository soak; the unconnected-runtime statements below describe the
> Phase-A checkpoint, not current code.

## Status

Phase A adds the persistence and transaction primitives needed for atomic
Node/Edge publication. It **does not yet change `run_ingest`**. Existing
analyzer jobs therefore continue to write the live graph until Phase B is
wired and old workers are drained. The legacy mixed-snapshot MCP guard must
remain enabled in that interval.

## Added contract

- `graph_node_stage` and `graph_edge_stage` isolate candidates by
  `analysis_runs.id`. Current readers still use `valid_to IS NULL` and cannot
  see staging rows.
- `graph_heads` stores one project CAS pointer: `generation`,
  `current_run_id`, `state`, and `published_at`. A database shape check requires
  `needs_rebuild` to be generation zero with no publication pointer/timestamp,
  and `ready` to have generation greater than zero plus both fields. Deleting
  the currently-published `AnalysisRun` is restricted rather than nulling its
  provenance pointer.
- `AnalysisRun.graph_base_generation` is captured once before staging. Producer
  deletion authority is canonicalized into
  `graph_authoritative_sources` under a write-once helper contract marked by
  `graph_coverage_sealed_at`; promotion does not accept either value from a
  retry-time caller. Direct database administration remains outside that
  application-level immutability boundary.
- Promotion reserves the persisted base generation, reconciles changed
  versions and sealed-authority omissions, advances the head, and completes
  the run in one database transaction.
- If the transaction fails, the old head/current graph remains unchanged and
  staging remains available for diagnosis or terminal-run cleanup.
- A retry after commit is idempotent when `graph_heads.current_run_id` already
  equals the run.
- Publication timestamps are strictly monotonic per project and are shared by
  all opened/closed versions and `AnalysisRun.completed_at`.

The Phase-A coverage seal persists and freezes an internal authorization
decision; it is **not itself evidence** that an analyzer scanned every expected
file. The helper still receives producer names from trusted orchestration. Phase
B must construct that set only after validating the immutable source manifest
and complete terminal producer records. The known zero-output/exit-0 ambiguity
remains until the analyzer protocol carries signed scanned-file coverage, so no
external caller should be allowed to mint this seal directly.

The correctness path uses ordinary SQLAlchemy transactions, a generation CAS,
bounded-memory batches, and row-value predicates supported by PostgreSQL and SQLite.
PostgreSQL also takes a row lock through `SELECT ... FOR UPDATE`; SQLite takes
its writer lock on the initial head CAS. Each statement sees committed old data
before promotion commits and committed new data afterward. This alone does
**not** pin several PostgreSQL `READ COMMITTED` statements to one generation: a
multi-query reader can straddle the commit. Phase A includes a head-stamp
read/revalidation primitive, but legacy readers are not wired to it yet. Phase B
must revalidate after the final graph query or use a repeatable-read/as-of
generation contract before claiming a concurrent request can never mix rows.
SQLite connections explicitly enable `PRAGMA foreign_keys=ON`; otherwise its
parsed FK/cascade/restrict declarations would not be enforced and local mode
would not share PostgreSQL's publication lifecycle contract.

“Bounded batches” does not mean the publication transaction is always short.
When deletion authority is supplied, Phase A scans all current Node/Edge rows
inside the transaction to find omissions. Memory is bounded, but DB work and
the head/write-lock duration are **O(current graph)**. Before a 50 K-file scale
claim, Phase B+ must precompute run-isolated deletion intents or use indexed
producer-contribution anti-joins, then make promotion proportional to the
prepared delta.

## Fail-closed bootstrap

Migration 0027 inserts a head for every existing project with:

```text
generation = 0
current_run_id = null
state = needs_rebuild
published_at = null
```

It deliberately does not infer cleanliness from the latest completed run: a
legacy failed run may already have committed partial rows. Only a staged run
whose persisted scope is `full`, promoted with the explicit rebuild capability,
and whose sealed deletion coverage includes every owner on every legacy current
row can change this state to `ready`. Additive partial producers may stage new
facts but receive no deletion authority. Omitted covered rows are closed in
that same transaction. API-created projects create the same fail-closed head
alongside the project row; any future project-creation path must do likewise.
`seed-demo` is the deliberate exception: its synthetic graph, completed run,
publication receipt, and generation-one ready head are inserted in one database
transaction, so the bundled demo is immediately readable without pretending an
unowned analyzer writer is still running.

## Phase B required before claiming the bug fixed

1. Redirect deterministic analyzer, agent, and live-schema records from
   `upsert_node`/`upsert_edge` into the run staging tables. Every stage writer
   must lock/check the owning run and reject writes after
   `graph_coverage_sealed_at`; the Phase-A FK/schema alone does not freeze a
   writer that bypasses that helper contract.
2. Capture `graph_base_generation` before the first stage write. Verify
   immutable source and complete producer coverage, seal authority for only
   those proven producers, then call promotion without caller-supplied CAS or
   deletion values.
3. Move findings, runtime reconciliation, history pruning, audit/network
   notifications, and optional LLM narration outside the core
   publication transaction. A derived-product failure must not unpublish a
   valid source index.
   Human confirmation and runtime `exercised` metadata currently mutate
   `Node.data`/`Edge.data` in place. A staged source candidate can overwrite
   those overlays, and a racing update can land on a just-closed historical
   version. Move them to durable overlay/contribution rows (or define an
   explicit merge+CAS contract) and test survival across publication before
   calling the whole graph atomic.
4. Replace timestamp/status-based mixed-run guards and incremental blocking
   with the graph-head contract. Failed staged runs leave the published head
   readable and must not require a full repair.
   The interim guard now catches active runs and failed/cancelled runs whose
   terminal time overlaps the latest completed baseline, including runs that
   started earlier. It still cannot prove safety when lifecycle timestamps are
   absent/skewed, nor can a one-shot HTTP/MCP guard prevent a legacy writer from
   starting immediately after the check. This remains a deployment blocker,
   not an atomicity proof.
5. Require historical graph comparison to resolve a completed run's stored
   `graph_publication` metadata; failed/running runs are not snapshots.
6. Drain every legacy worker before enabling Phase B. A mixed deployment lets
   an old worker bypass staging and invalidates the atomic guarantee.

The Phase-A stage primary key admits one materialized candidate per logical
Node/Edge identity and carries one `source_name`; it is not a contribution
ledger. Existing writer output is normally singleton-owned, but the schema and
imported/current data can contain different or several `created_by` owners. A
cross-owner replacement fails closed with `MultiProducerContributionRequired`
unless sealed complete coverage includes every current owner; only then can
omission of the prior owners be distinguished from incomplete refresh. Two producer payloads for the
same new identity still cannot be preserved independently in this stage schema.
Phase B must make stage conflict handling conservative or add durable Node/Edge
contribution tables before claiming precise simultaneous multi-producer merge.
Per-file analyzer resume remains separate scale work.

## Required Phase-B/soak evidence

- concurrent reader sees only old or new graph, never mixed;
- analyzer error, cancellation, worker kill, and DB disconnect leave the head
  and current rows unchanged;
- two runs from the same base generation produce one winner and one stale-base
  rejection;
- post-commit retry is idempotent;
- full/add/change/delete/rename and semantic no-op preserve bitemporal history;
- SQLite local mode and real PostgreSQL pass the same contract suite;
- 50 K-file crash soak records stage size, promotion lock time, WAL growth,
  peak RSS, and cleanup latency.
