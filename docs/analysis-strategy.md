# Incremental analysis strategy — how Mnemos analyses systems the LLM can't read at once

> Companion to `Mnemos_spec.md` §2.6, §2.7, and §10.
> Short version: **no LLM call ever sees the whole codebase.** The graph is
> built from small, local facts; summaries are stacked bottom-up; every stage
> is tracked in the DB and streamed to the Analysis tab so an operator can
> watch what the platform is doing in real time.

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
  runtime traces produce *objective* graph rows with no LLM involvement.
- **Local neighbourhoods only.** When the LLM is called, its prompt is built
  from at most a 1-hop neighbourhood of the target node.
- **Append-only progress.** Every stage writes into the database; the GUI
  and MCP see partial results the moment they land.

## 2. Bottom-up hierarchy (L0–L5)

```
L5 system summary      ← L4 domain summaries + system-boundary contracts
L4 domain summary      ← L3 module summaries + cross-module edges
L3 module summary      ← L2 file summaries + module-boundary edges
L2 file summary        ← L1 function summaries within the file
L1 function summary    ← one function body + its 1-hop calls
L0 raw facts           ← analysers (no LLM) — symbols, edges, contracts, data
```

| Layer | Who produces it                   | Input size    | Output size  |
|-------|-----------------------------------|---------------|--------------|
| L0    | Static analysers, DB introspect.  | files, schemas| graph rows   |
| L1    | Extractor + 1-hop neighbours      | ~500 tokens   | ~200 tokens  |
| L2    | Extractor over L1 of one file     | ~2K tokens    | ~500 tokens  |
| L3    | Extractor over L2 of one module   | ~4K tokens    | ~1K tokens   |
| L4    | (Phase 2) Extractor over L3       | ~8K tokens    | ~2K tokens   |
| L5    | (Phase 2) Extractor over L4       | ~20K tokens   | ~5K tokens   |

Validator enforces that every claim's `evidence` cites a node or edge ID that
actually exists; hallucinated references are dropped (§10.4).

## 3. Staged execution pipeline

Every analysis run goes through a fixed sequence of stages. Each stage is one
row in `analysis_stages` with its own `status`, `items_total`, `items_done`,
and timestamps. Stages run sequentially at the top level; per-file /
per-symbol work within a stage fans out through the worker queue but updates
the same row as it progresses, so the GUI can show a progress bar rather
than a spinner.

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
│ findings     │ →  │ L1 summaries │ →  │ L2 summaries │ →  │ L3 summaries │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

Rules the pipeline enforces:

- Later stages depend only on earlier stages' **outputs in the graph**, never
  on in-memory state — restarting mid-pipeline is safe.
- A stage never blocks on a long-running LLM call: summariser stages iterate
  per target and commit after each, so cancellation is granular.
- If a stage runs out of budget (time/tokens) it exits in `status=partial`,
  records what it did, and leaves the next stage's work intact.
- Stages emit SSE events (`stage_started`, `stage_progress`, `stage_done`,
  `stage_failed`) on the run's progress channel. The Analysis tab renders
  them as a pipeline with per-stage bars.

## 4. Budget controls

| Budget       | Enforcement point                         | Default   |
|--------------|-------------------------------------------|-----------|
| LLM tokens   | `Extractor.max_input_tokens` per call     | 6,000     |
| Symbols/L1   | `summarise_l1` `limit` argument           | 25/run    |
| Files/L2     | `summarise_l2` limit                      | 25/run    |
| Modules/L3   | `summarise_l3` limit                      | 25/run    |
| Per-stage    | `analysis_stages.time_budget_sec`         | 600       |
| Whole run    | Worker-level wall clock                   | 8h (§1.5) |

All budgets are advisory: exhausting one marks the stage `partial` and
hands control back to the scheduler. Subsequent runs pick up the unfinished
work (spec §10.5 — Phase-1 does this in full re-run form; per-file
re-summaries are Phase-2).

## 5. What the operator sees

1. Trigger an analysis from the Analysis tab.
2. A new row appears in **Recent runs** with live counters (symbols, edges,
   contracts, findings, summaries).
3. Click the row → **Pipeline view** with one card per stage:
   - Stage name + language
   - Status badge (`pending` / `running` / `partial` / `completed` / `failed`)
   - Progress bar (`items_done / items_total`)
   - Elapsed time, final stats when complete
   - Open questions + claims for summariser stages
4. Failures are never silent. A failed stage pins the card red and shows the
   stderr JSON lines the analyser emitted.
5. The same data is available via `/api/v1/analysis_runs/{id}/stages` for
   dashboards outside Mnemos.

## 6. When analysis doesn't fit at all

Some systems are simply too large for a single run even with budgeting. The
Phase-1 escape hatch is scope narrowing:

- **Directory filter** on run trigger (`source_path` points to one subtree).
- **Language filter** on run trigger (run only the C# analyser today, TS
  tomorrow).
- **Re-run specific stages** from `/analysis_runs/{id}/stages/{stage}/retry`
  without repeating expensive L0 work.

Phase 2 extends this with true incremental graph updates keyed on git
deltas, and with L4/L5 summary fan-out across domains detected via Louvain
community detection (spec §10.2, §15.4).

## 7. Scaling to large files and many files

The hierarchy in §2 handles a system of ordinary files. Two separate
pathologies still need explicit handling: **a single file or function that
is itself enormous**, and **a codebase whose sheer symbol count (>100k)
exceeds any per-run budget**. Both are solved by shrinking inputs further
and persisting progress across runs so later runs only do what's left.

### 7.1 Large file / large function

| Risk                                    | Strategy                                                                                                 |
|-----------------------------------------|----------------------------------------------------------------------------------------------------------|
| 5k-line method body won't fit a prompt  | L0 analysers never send bodies — they emit `signature`, `location`, and the CALLS neighbourhood only.    |
| 500-method single file overflows L2     | Token-budget packer groups L1 summaries into N chunks, produces N "partial L2"s, then folds them.        |
| Huge generated file (e.g. `obj/`)       | Directory ignore-list at `probe` time; generated files remain L0 facts but are skipped for summaries.    |
| Binary/non-UTF8 file                    | `read_file` returns `encoding=binary_hex` with byte cap; summariser refuses.                             |
| Claude Code needs to read a huge file    | `read_file` accepts `start_line`/`end_line` so the client streams the file in windows.                   |

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
| Per-run L1 limit leaves most symbols cold  | **Continuation runs**: `scope=continuation` skips L0, chews only pending summariser targets.                                    |
| Re-running analysis re-summarises unchanged code | **Content-hash skip**: each Summary stores the hash of its evidence; the next run skips targets whose hash matches the latest. |
| Which 25 to summarise first matters        | **Priority ordering**: entry points (HTTP contracts, Main methods, controller actions) first, then high in/out degree.          |
| Budgets hide the work remaining            | **Pending-count stats** on the pipeline card show (done / total / pending) so operators know how many continuation runs to queue. |
| Single-threaded L1 is slow on big systems  | `summarise_*` accepts a `limit` argument; the scheduler can enqueue multiple `run_ingest(scope=continuation)` jobs concurrently, each with a different limit window. |

The combined effect: an operator on a 200k-symbol system runs one full
analysis (L0 facts land, budgets cap L1/L2/L3 at 25 each), then queues N
continuation runs until `pending=0`. Each continuation run takes hours,
not days, because it skips analyser work and touches only unchanged
targets through hash-equality checks.

### 7.3 Hard ceiling: opt-in scope

If even continuation runs aren't fast enough, the operator narrows the
scope:

- Trigger with `source_path=<subtree>` — only that subtree's L0 facts go
  into the graph this run, and only its symbols are eligible for L1/L2/L3.
- Flip a module to `analysis_scope=skip` in the Projects settings to
  permanently exclude it (vendored libraries, generated code, tests).
- Per-language runs: `languages=["csharp"]` on trigger.

All three knobs are recorded in `audit_log` so partial analyses never
silently bias the graph — the operator can always see exactly what was
analysed.
