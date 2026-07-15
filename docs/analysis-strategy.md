# Incremental analysis strategy — how Mnemos analyses systems the LLM can't read at once

> Companion to `Mnemos_spec.md` §2.6, §2.7, and §10.
> Short version: the useful default is a **deterministic, re-queryable source
> index with zero LLM tokens**. No LLM call ever sees the whole codebase.
> Optional summaries are stacked bottom-up over bounded graph evidence; every
> stage is tracked and streamed so an operator can see what actually ran.

## 1. The core problem

Even the largest context windows fit tens of thousands of tokens, not the
millions of tokens in an enterprise repo + stored procedures + legacy DLLs.
Three failure modes follow from ignoring this:

1. **Context exhaustion** — feeding a whole repo into one prompt drops detail
   and truncates unpredictably.
2. **Hallucinated references** — without grounding, the model invents symbol
   names, endpoints, or columns that don't exist.
3. **No incremental story** — "run once and pray" gives the operator nothing
   to monitor; failures surface at the end, after tokens are already spent.

Mnemos is built around the opposite defaults:

- **Facts first, summaries second.** Static analysers, DB introspection, and
  runtime traces produce graph rows with no LLM involvement and retain the
  producer's `verified`/`asserted`/`inferred` certainty.
- **Index is complete without narration.** L1–L3 is disabled unless the caller
  sends `summarize=true`; AI extraction for uncovered languages separately
  requires `agent_extract_limit > 0`.
- **Local neighbourhoods only.** When the LLM is called, its prompt is built
  from at most a 1-hop neighbourhood of the target node.
- **Visible progress without partial publication.** Stage state is persisted and
  streamed, while analyzer rows stay in run-scoped staging. Only a sealed,
  successfully reconciled run can atomically advance the graph head.

## 2. Source index plus optional hierarchy (L0–L5)

```
L5 system summary      ← L4 domain summaries + system-boundary contracts (future)
L4 domain summary      ← L3 module summaries + cross-module edges
L3 module summary      ← L2 file summaries + module-boundary edges
L2 file summary        ← L1 function summaries within the file
L1 function summary    ← one symbol node + bounded 1-hop graph evidence
L0 source index        ← analysers (default product, no LLM) — symbols, edges, contracts, data
```

| Layer | Who produces it                   | Input size    | Output size  |
|-------|-----------------------------------|---------------|--------------|
| L0    | Static analysers, DB introspect.  | files, schemas| graph rows   |
| L1    | Extractor + 1-hop neighbours      | ~500 tokens   | ~200 tokens  |
| L2    | Extractor over L1 of one file     | ~2K tokens    | ~500 tokens  |
| L3    | Extractor over L2 of one module   | ~4K tokens    | ~1K tokens   |
| L4    | (Phase 2) Extractor over L3       | ~8K tokens    | ~2K tokens   |
| L5    | (Phase 2) Extractor over L4       | ~20K tokens   | ~5K tokens   |

The schema boundary requires each structured claim to contain evidence.
Validator then requires every cited node/edge to exist in the same project's
current graph. Invalid claims are dropped. Summary prose is optional narration,
not an independently verified fact source.

## 3. Staged execution pipeline

Every analysis run goes through a fixed sequence of stages. Each stage is one
row in `analysis_stages` with its own `status`, `items_total`, `items_done`,
and timestamps. Stages run sequentially in the current single-mutation worker;
the row is updated as records are consumed so the GUI can show progress rather
than only a spinner.

```
┌───────────────┐   ┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ probe         │ → │ inventory    │ → │ symbols      │ → │ contracts    │
└───────────────┘   └──────────────┘   └──────────────┘   └──────────────┘
                                                                 │
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌────▼────────┐
│ merge        │ ←  │ data_entities│ ←  │ calls        │ ←  │ data_access │
└───────┬──────┘    └──────────────┘    └──────────────┘    └─────────────┘
        │
        ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ runtime      │ →  │ findings      │ →  │ optional L1-3 │ →  │ final status  │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

Rules the pipeline enforces:

- Source-stage output is persisted in run-scoped tables but an interrupted
  pre-publication run is not resumed; stale jobs become terminal failures and
  their staging is disposable. A run whose source receipt is already durable
  may resume only its post-publication work after the receipt is matched to the
  current graph head.
- Analyzer subprocesses have a bounded queue, 1 MiB record contract, wall
  timeout, and terminate→kill cancellation. A 30-minute analyzer-stage absolute
  deadline also covers validation, graph upsert, commit, and progress flush;
  cancellation closes the record stream and child even while DB work is blocked.
- An incomplete analyzer stage records the gap and is denied deletion
  authority. Its staged additions are never visible unless the whole source
  publication contract succeeds.
- Stages emit SSE events (`stage_started`, `stage_progress`, `stage_done`,
  `stage_failed`) on the run's progress channel. The Analysis tab renders
  them as a pipeline with per-stage bars.

## 4. Budget controls

| Budget       | Enforcement point                         | Default   |
|--------------|-------------------------------------------|-----------|
| Initial LLM  | API request (`summarize=false`, agent limit 0) | 0 tokens |
| LLM evidence | deterministic approximate-token packer   | 3K (L2) / 4K (L3) evidence chunks |
| Symbols/L1   | `summarise_l1` `limit` argument           | 25/run    |
| Files/L2     | `summarise_l2` limit                      | 25/run    |
| Modules/L3   | `summarise_l3` limit                      | 25/run    |
| Optional AI calls | shared `LLMRunBudget` across agent extraction + L1–L3 | 64 provider attempts/run |
| Optional AI input | pre-call approximate-token reservation | 120K estimated tokens/run |
| Optional AI wall time | one absolute deadline around every provider call | 600s/run |
| Chat provider input | whole-message/record packing, system included | 24K chars/request |
| Chat output | provider token field or client stream ceiling | answer 1,200 / rewrite 128 tokens |
| Per-stage    | `analysis_stages.time_budget_sec`         | stage-specific (120–3600s) |
| Whole run    | Worker-level wall clock                   | 8h (§1.5) |

The shared optional-AI call/input/wall limits and Chat input/output limits are
hard boundaries. Approximate input tokens are deliberately an estimate rather
than a provider billing claim. Analyzer output, queue, record, process, stage,
and whole-job limits are also hard safety boundaries. The worker runs one graph
publication at a time, never exposes a partially staged run, and supports job
abort. A source receipt can remain usable even when later derived work closes
the run as `partial`.

## 5. What the operator sees

1. Trigger a deterministic full index or content-aware incremental refresh.
2. A new row appears in **Recent runs** with live counters (symbols, edges,
   contracts, findings, and optional summaries).
3. Click the row → **Pipeline view** with one card per stage:
   - Stage name + language
   - Status badge (`pending` / `running` / `published` / `partial` / `completed` / `failed`)
   - Progress bar (`items_done / items_total`)
   - Elapsed time, final stats when complete
   - Open questions + claims for summariser stages
4. Failures are never silent. A failed stage pins the card red and shows the
   stderr JSON lines the analyser emitted.
5. The same data is available via `/api/v1/analysis_runs/{id}/stages` for
   dashboards outside Mnemos.

## 6. When analysis doesn't fit at all

Some systems are simply too large for a single run even with budgeting. The
current escape hatch is scope narrowing:

- **Directory filter** on run trigger (`source_path` is an absolute path to one
  subtree visible to the worker and contained by that project's operator-set
  `SOURCE_PROJECT_ROOTS` binding).
- **Project language registration** determines which analyzer families run.
- A failed source stage is retried by creating a new analysis run. There is no
  per-stage retry endpoint that resumes L0; only post-publication work may be
  resumed against an exact receipt/head match.

Current exact-Git incremental refresh fingerprints blob OID/path/size from the
commit tree without opening source bodies; mutable/non-Git source still hashes
relevant file bytes per analyzer family.
An unchanged run spawns no analyzer; a Python-only change skips TS/Java/C++;
deletion/rename closes omitted facts after a successful authoritative refresh.
The changed analyzer still re-walks its relevant tree. Per-file parser resume,
dependency-closure refresh, and L4/L5 remain Phase 2.

Here L4/L5 means the hierarchical domain/system roll-up shown in §2. The MCP
`trace_flow` feature also persists a row labelled level 4, but it is a separate
`mnemos.flow_result.v1` source-window hypothesis. It must not be counted as a
delivered hierarchical L4 summary or promoted to verified graph evidence.

## 7. Scaling to large files and many files

The hierarchy in §2 handles a system of ordinary files. Two separate
pathologies still need explicit handling: **a single file or function that
is itself enormous**, and **a codebase whose sheer symbol count (>100k)
exceeds any per-run budget**. Current bounds and scope controls mitigate these
cases; a real 50 K-file multi-language soak has not proved them solved.

### 7.1 Large file / large function

| Risk                                    | Strategy                                                                                                 |
|-----------------------------------------|----------------------------------------------------------------------------------------------------------|
| 5k-line method body won't fit a prompt  | L0 analysers never send bodies — they emit `signature`, `location`, and the CALLS neighbourhood only.    |
| 500-method single file overflows L2     | Token-budget packer groups L1 summaries into N chunks, produces N "partial L2"s, then folds them.        |
| Huge generated file (e.g. `obj/`)       | Producer-specific discovery exclusions; coverage must disclose what was skipped.                         |
| Binary/non-UTF8 file                    | Snapshot `read_file` rejects binary/invalid UTF-8 and reports a bounded unavailable result.               |
| An AI needs to read a huge file         | Snapshot `read_file` accepts a line range and enforces byte/line caps; clients request another window.    |

Concretely:

- **Signatures only, never bodies.** L1 evidence for a function is
  `{signature, callers, callees, data_access}`, not `body`. If the body is
  needed, Claude Code fetches it via `read_file` with a line range.
- **L2 chunking.** When a file has more L1 summaries than the L2 token
  budget allows, the packer splits them into disjoint chunks of ~3K
  tokens each. Each chunk produces a partial L2; a final rollup pass
  condenses the partials into one L2 for the file.
- **L3 directory-folding.** Same idea one level up: large directories
  are split into subtrees, each summarised, then folded.

### 7.2 Many files (100k+ symbols)

| Risk                                       | Strategy                                                                                                                        |
|--------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------|
| Per-run optional L1 limit leaves symbols without narration | **Continuation runs** reuse L0 and run narration only when `summarize=true`. |
| Re-running analysis re-summarises unchanged code | **Content-hash skip**: each Summary stores the hash of its evidence; the next run skips targets whose hash matches the latest. |
| Which 25 to summarise first matters        | **Priority ordering**: entry points (HTTP contracts, Main methods, controller actions) first, then high in/out degree.          |
| Budgets hide the work remaining            | **Pending-count stats** on the pipeline card show (done / total / pending) so operators know how many continuation runs to queue. |
| Repeated refresh is slow                   | Content manifests skip unchanged analyzer families; semantic upserts preserve unchanged temporal rows. |

The intended workflow for a large system is one deterministic index followed by
bounded MCP re-query. Project indexes and task packs cap nested data and omit raw
source, then enforce a 50 KiB hard serialized-byte ceiling before the independent
MCP transport guard. This is a byte bound, not an exact tokenizer-specific token
promise. Optional narration is not a prerequisite for source lookup,
caller/callee, contract, data-access, or impact analysis. Capacity at 200k symbols
is a target, not a verified claim.

### 7.3 Subtree runs

If even continuation runs aren't fast enough, the operator narrows the
scope:

- Bind the project UUID to its repository root in `SOURCE_PROJECT_ROOTS`, then
  trigger with `source_path=<subtree>` beneath that root to add/update facts.
- If the path is below a detected Git root, the run is marked
  `authoritative_root=false` and **does not close omitted facts** elsewhere;
  absence in a partial scan is not evidence of deletion.
- Project language registration controls which deterministic analyzers run.

The authoritative-root decision and analyzer content fingerprints are stored
in `AnalysisRun.stats`, so consumers can see whether deletion semantics were
applied.
