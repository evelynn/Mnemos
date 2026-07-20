# Mnemos — Project Charter (read this first, every session)

> **목적 (Purpose):** Mnemos는 **AI가 거대한 소스 코드를 분석할 때 쓰는 보조 도구**다.
> 일반 SaaS·챗봇·관리자 대시보드 제품이 아니다. 모든 변경은 *AI 보조 소스 분석*이라는
> 목적에 복무해야 한다. 그렇지 않은 기능은 범위 밖이다 — 멈추고 사용자에게 확인하라.
>
> Mnemos is an **AI-assist tool for analyzing large source code** — not a generic app.
> Every change must serve AI-assisted source analysis; if it doesn't, it is out of scope.

## Why it exists — the 5 pillars (each addresses a hard limit of "just prompt an AI")

A general AI analyzing a big repo directly fails in specific ways. Mnemos addresses each through
the mechanisms below. The deterministic core has code, test, and one external-repository evidence;
the evidence level is not uniform and the Phase-2 gaps below are part of the product contract:

1. **Scale (context window).** A huge repo doesn't fit in any prompt. Mnemos does deterministic
   source indexing into a re-queryable graph and gives an AI bounded task-oriented views.
   That index is the useful product and the default first pass uses **zero LLM tokens**. Optional
   **L1(symbol) → L2(file) → L3(module)** narration may condense graph evidence afterward; at
   L2+ it never re-reads raw files. All opt-in AI extraction/narration shares finite call/input/
   output/wall budgets; Agent + L1–L3 analysis scopes persist reservations/deadlines across worker
   restart. Chat has a system-inclusive 24K-character input ceiling. `pack_by_budget` bounds
   oversized items and priority ranking covers the important surface first. → `server/app/extractor/runner.py`,
   `extractor/packing.py`, `orchestrator/source_manifest.py`.
2. **Hallucination.** Mnemos grounds **structured LLM claims**: malformed or evidence-free claims,
   and claims whose project-scoped evidence node/edge is absent from the current graph, are
   **dropped** (`extractor/schema.py`, `extractor/validator.py`). Narrative prose is explicitly
   not source truth and must be checked through its claim evidence. Dangling LLM edges are
   filtered at ingest. Analyzer facts retain their emitted certainty; LLM-derived structure is
   `"inferred"` — **never conflated** (`extractor/agent_extract.py`).
3. **Determinism.** Language analyzers (`analyzers/ggoss-{ts,py,cpp,java,kotlin,web,csharp,
   sql-mssql,sql-oracle,binary-dotnet}` + `ggoss-treesitter`) use deterministic parsers or
   documented scanners (ggoss-py = stdlib `ast`; ggoss-cpp/java/kotlin = regex + brace scanner;
   ggoss-treesitter = tree-sitter, config-driven multi-language — go/rust/ruby PoC, opt-in dep).
   The graph is the source of truth; the LLM only narrates or fills gaps for uncovered languages.
4. **No reuse across sessions.** Results persist in a **bitemporal knowledge graph with current
   producer provenance** (`models/graph.py`: Node/Edge with `valid_from`/`valid_to`, `certainty`,
   `created_by`, and `AnalysisRun` per git_sha) and are re-queryable by **MCP tools**
   (`mcp/server.py`: `search_symbols`, `find_callers`, `find_callees`, `impact_analysis`,
   `get_contract`, `get_data_access`, `get_module_summary`, `list_flows`, `find_runtime_path`, …)
   and the dashboard chat. An AI **re-queries stored facts instead of re-ingesting the repo**.
5. **Blind to cross-service + runtime reality.** **Contract-id normalization** links a C#
   endpoint and a TS `fetch` to the *same* node (`merge/contract_id.py`); **OTLP runtime
   reconciliation** marks which edges are *actually exercised in production*
   (`merge/runtime.py`); risk scoring weights blast-radius + exercised (`merge/findings.py`).

It then **presents results as clear tables/views** in the dashboard, and answers questions in the
**Chat tab** grounded in the analysis (price-attested direct Claude/OpenAI/Gemini routes —
`api/chat.py`, `api/llm_providers.py`, configured in Settings → AI 제공자). Atlas remains a
configuration surface, but generation is fail-disabled until it has a provider-enforced output,
usage, immutable price, and durable-attempt contract.

## Hard guardrails — do NOT drift

- **Stay on purpose.** New work must map to a pillar above, or to clearer table presentation, or
  to AI-reuse. If it doesn't, it is scope creep — ask first. Don't build generic-app features.
- **Honesty over polish.** Preserve the **verified vs inferred** distinction. An LLM guess must
  never masquerade as a verified fact. Document analyzer limitations (as `ggoss-py` does).
- **The graph is the source of truth.** LLM output narrates/fills gaps; it never overrides
  deterministic extraction.
- **Index first; AI on demand.** A normal full/incremental analysis must not instantiate an LLM
  client. AI narration and AI extraction for uncovered languages require explicit opt-in and
  must report their own limits/costs. Mnemos guides an AI with evidence; it does not precompute
  an expensive AI opinion about the whole repository.
- **Scale-safe always.** Anything touching analysis stays bounded (budget chunking, priority
   ranking, analyzer-group content fingerprints, semantic no-op graph writes, bounded subprocess
  queues, hidden-cache/link exclusion, incremental `evidence_hash` skip). Never assume the whole
  repo fits in memory/context.
- **Not a generic app.** Auth/secrets/settings/orgs exist *only* to serve the analysis mission.

## Known gaps (Phase-2 — do not claim as done)

- ggoss-py cross-module call resolution is name-based only (no full import graph).
- ggoss-cpp does not evaluate the preprocessor (both `#ifdef` branches are seen), does not
  resolve function-pointer/member calls, and skips `vendored/` trees by default.
- ggoss-java extracts only body-bearing methods (abstract/interface + Spring-Data repository
  query methods are not yet symbols) and resolves calls by name (no import/FQN resolution).
- Web+Java direction (2026-07-08 eval): deterministic parity on the web-stack language set
  (TS/JS strong; Java/Kotlin/HTML/CSS added) + differentiate on the grounded / runtime-aware /
  summarization layer. See docs/04-eval/.
- Language extensibility (2026-07-08, absorption review T2): `ggoss-treesitter` absorbs cbm's
  tree-sitter mechanism natively — one config-driven analyzer, 100+ grammars available, go/rust/
  ruby wired as PoC. Contracts/routes for tree-sitter langs are future work. All contract-emitting
  analyzers use `http_endpoint`+`spec` so cross-service links normalize to one node.
- L3 module boundary is a path-segment heuristic; hierarchical domain L4/L5 summaries are not
  built. The separately named `trace_flow` L4 contract is a bounded source-window hypothesis,
  not a hierarchical domain summary and not a verified graph fact.
- Optional narrative coverage is bounded by a symbol `limit`; the deterministic graph index is
  not. The quality/cost break-even of eager whole-project narration is not established, so it is
  disabled by default.
- A changed analyzer family currently re-walks its relevant tree; fingerprints skip unchanged
  analyzer families and no-op runs, but per-file parser resume is still Phase-2 work.
- C# fingerprints include source/project/common MSBuild config files, but the complete imported
  SDK/NuGet/MSBuild environment is not yet an immutable manifest closure. `ggoss-csharp` is
  therefore deliberately non-cacheable and reruns instead of authorizing an unsafe stale skip.
- Source publication now uses run-scoped Node/Edge staging, a persisted base-generation and
  write-once producer-coverage seal, one graph-head CAS, and an immutable publication receipt.
  Analyzer failure/cancellation cannot expose staged rows; a committed source generation remains
  readable while runtime reconciliation, findings, and optional summaries finish. Those derived
  stages close the run as `completed` or explicitly `partial`; they do not roll the head back.
  Current HTTP/MCP/source readers pin and revalidate both source and durable-overlay generations.
  This contract has SQLite/unit/mock evidence plus a PostgreSQL 17.10 migration/ledger suite and a
  50 K-file/50 K-node component soak. Hard process-kill fault injection now covers the SQLite WAL
  path (a real subprocess dies via `os._exit` after staging, after seal, inside the open promotion
  transaction after the head CAS was issued, and right after publication commit; recovery and retry
  promotion are asserted — `tests/test_publication_hard_kill.py`). A PostgreSQL process-kill run and
  a representative end-to-end repository workload are still required before a production-atomicity
  claim.
- `created_by` materializes the canonical union of identical current producer contributions, but
  there is not yet a durable per-producer contribution history. Deletion authority therefore stays
  conservative and is granted only to producers whose complete coverage was sealed for the run.
- Authoritative refresh keeps run identities in bounded memory and spills them to a private
  temporary disk index; current-row sweep is paged. The sweep is still **O(graph) DB work**. It
  passed a narrow 50 K-file/50 K-node/zero-edge PostgreSQL component soak, which is not evidence
  for Linux-kernel complexity, mixed analyzer verbs, or production query latency.
- Analyzer success is inferred from bounded process/JSONL/verb completion; the contract does not
  yet include a signed terminal coverage record with scanned-file counts. An analyzer bug that
  exits 0 and emits zero valid facts cannot always be distinguished from legitimately empty source.
- Completed Git runs retain a full commit SHA and provenance, but Mnemos does not yet create a
  durable retention ref/content archive. Repository GC can therefore make an old source snapshot
  unreadable; non-Git immutable content archives are a documented contract, not an implementation.
- Automatic bitemporal graph-history pruning is disabled. Safe pruning still needs a retained-from
  watermark/history revision so historical run comparison cannot silently become incomplete.
- The default Compose worker bundles the in-repo Python/TS/JS/C/C++/Java/Kotlin/Web/tree-sitter
  path. C#, live-DB, and .NET-binary analyzers exist in the repository but are not wired into that
  worker image; their standalone profile images are contract-test artifacts, not an execution path.
- Rapid webhook pushes are coalesced at enqueue: once a newer push has a committed run row and a
  live job, older still-`queued` webhook runs for that project are superseded, so the worker
  short-circuits at its eligibility check instead of running an analysis. Runs already `running`,
  and manual runs, are never touched. The queued ARQ job is **not** aborted — the worker still
  dequeues it, opens a session, reads the row and publishes a terminal event per superseded push;
  only the analysis is skipped. Two pushes whose transactions interleave can still both stay
  queued, and a burst arriving while a run executes still queues one follow-up by design.
- Project-lock contention preserves the queued run and retries it, but a hard-crashed owner cannot
  be safely pre-empted without a fencing token/DB advisory lock. The fail-safe Redis lease can delay
  the next run for up to its nine-hour TTL rather than risking two current-graph writers.
- The remediated optional LLM path has static/unit/mock and real-PostgreSQL accounting evidence.
  No provider credential is present in this environment, so a live OpenAI/Anthropic/Gemini canary
  and a representative E4 answer-quality/token workflow remain unrun. Do not convert hard budget
  safety into a claim of measured token savings or provider interoperability.
- Cloud embeddings (Voyage/OpenAI) are deliberately non-executable even when legacy environment
  variables are populated. The old adapter had no project-scoped durable attempt identity, usage
  settlement, or immutable worst-price reservation, so MCP search remains deterministic lexical/
  BM25 until that full accounting contract is implemented. RRF/cosine helpers and the opt-in
  pgvector schema remain scaffolding, not evidence of a live vector-search product path.
- Every production paid-generation owner (Chat, direct Anthropic Summary, Second Opinion, Agent
  extraction, and Flow) requires a positive project-dollar policy and an atomic worst-case
  reservation before network dispatch. Immutable contracts bind the exact provider, model,
  official API base, full documented input ceiling, requested provider-enforced output cap, and
  conservative input/output prices. Project-row serialization makes concurrent reservations
  atomic; STARTED is committed before dispatch; terminal replay is allowed after policy removal.
  Unknown models, routes, catalogs, and zero/unset caps fail before network. This deliberately
  over-reserves and never refunds, so it is a safety invariant rather than a utilization claim.
- Direct OpenAI/Gemini/Anthropic routes are the only price-attested generation routes. OpenAI must
  use the official HTTPS `api.openai.com/v1` root; Anthropic clients explicitly use the official
  base and disable SDK retries. Opaque Claude Agent SDK summary/extraction/Flow has no immutable
  price/route contract and is therefore fail-disabled before `query()`, even if the SDK is
  importable or an operator raises token limits. Atlas generation is likewise disabled.
- Chat, Summary, Flow, Agent extraction, and Second Opinion all use durable stable operation/input
  identities, encrypted normalized candidates, terminal-aware replay, and privacy-safe failure
  codes. Summary/Flow/Second Opinion validate the exact candidate binding and commit accepted
  product publication with attempt classification in one transaction. A committed candidate is
  replayed without provider redispatch after a crash; a STARTED attempt is never guessed complete.
- Optional `trace_flow` L4 narration is pinned to bounded files from one atomically published Git
  snapshot and uses the canonical `mnemos.flow_result.v1` contract. Its current Agent SDK
  transport is production-fail-disabled by the missing immutable route/price contract. If a
  price-attested transport is added, its step/flag prose still lacks graph node/edge or line-range
  grounding and must remain a hypothesis-bearing explanation, never a verified graph fact.
- L2/L3 narration selection is pushed into SQL and bounded by its target limit (L2: file
  DISTINCT+LIMIT on `data.location.file`; L3: full-row loads restricted to the selected modules).
  The remaining pre-call scan is L3's narrow key scan of one `target_id` string per current L2
  summary — O(#summarized files), no longer O(#symbols) full-object materialization.
- Lexical symbol search keeps its 2,000-row candidate cap and its single filtered scan, but orders
  that scan by priority tier (exact name → name prefix → anything else matched, `id` breaking ties),
  so exact/prefix matches can no longer be crowded out of the cap. This costs more than the old
  unordered scan, not less: without an ORDER BY the planner could stop at 2,000 qualifying rows,
  whereas the tier key is an unindexed expression over `data->>'name'`, so every matching row is
  now evaluated and top-N sorted. The gap is worst for very common terms. An expression index on
  `lower(data->>'name')` would remove it and has not been added. A very large graph can still miss
  a globally best substring/stem-only match beyond the cap, and cloud vector search stays disabled
  — do not claim CBM-like local semantic-search coverage or latency.
- Gate-B submissions are bounded at ingress: `DiffSubmit.diff` rejects past
  `DIFF_INPUT_MAX_CHARS` (1 M chars, 422) and `run_pipeline` fail-closes its non-HTTP callers
  (break-glass rerun, MCP dev tools) with `DiffInputTooLarge` before any deterministic pass scans
  the string. Second Opinion additionally selects bounded diff-line evidence on its own.
- AI extraction for uncovered languages is `certainty="inferred"` only and lower trust than
  deterministic analyzers. Its current Agent SDK transport is fail-disabled by the immutable
  price/route requirement; explicit opt-in alone does not enable network dispatch. Go/Rust/Ruby
  have a deterministic tree-sitter path when its optional dependency is installed; C/C++ is
  deterministic.

## Stack quick-map

`analyzers/ggoss-*` (deterministic extractors) · `server/app/analyzers/runner.py` (subprocess
runner) · `server/app/extractor/` (graph ingest + L1–L3 recursive summaries + LLM fallback +
grounding) · `server/app/models/graph.py` (bitemporal graph) · `server/app/merge/` (contract-id,
runtime reconcile, findings/risk) · `server/app/mcp/` (AI-reuse tools) · `server/app/orchestrator/`
(staged pipeline, budgets, SSE progress) · `server/app/dashboard/` + `server/app/api/` (UI + API).
Docker-free local run: `python -m app.serve_local` (SQLite + fakeredis + inline jobs).
