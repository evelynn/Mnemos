# Mnemos — Project Charter (read this first, every session)

> **목적 (Purpose):** Mnemos는 **AI가 거대한 소스 코드를 분석할 때 쓰는 보조 도구**다.
> 일반 SaaS·챗봇·관리자 대시보드 제품이 아니다. 모든 변경은 *AI 보조 소스 분석*이라는
> 목적에 복무해야 한다. 그렇지 않은 기능은 범위 밖이다 — 멈추고 사용자에게 확인하라.
>
> Mnemos is an **AI-assist tool for analyzing large source code** — not a generic app.
> Every change must serve AI-assisted source analysis; if it doesn't, it is out of scope.

## Why it exists — the 5 pillars (each removes a hard limit of "just prompt an AI")

A general AI analyzing a big repo directly fails in specific ways. Mnemos removes each — and
these are **implemented and verified in code**, not aspirations:

1. **Scale (context window).** A huge repo doesn't fit in any prompt. Mnemos does **recursive,
   bottom-up analysis**: deterministic analyzers build a structured graph, then
   **L1(symbol) → L2(file) → L3(module)** summaries condense upward. At L2+ the LLM **never
   re-reads raw files** — it only condenses already-condensed summaries; `pack_by_budget`
   map-reduces oversized inputs; priority ranking (entry points + caller in-degree) covers the
   important surface first. → `server/app/extractor/runner.py`, `extractor/packing.py`.
2. **Hallucination.** Mnemos **grounds** every LLM claim: a claim whose evidence node/edge isn't
   in the graph is **dropped** (`extractor/validator.py`); dangling LLM edges are filtered at
   ingest. Analyzer facts are `certainty="verified"`; LLM-derived structure is `"inferred"` —
   **never conflated** (`extractor/agent_extract.py`).
3. **Determinism.** Language analyzers (`analyzers/ggoss-{ts,py,csharp,sql-mssql,sql-oracle,
   binary-dotnet}`) are AST/parser-based and deterministic (ggoss-py = stdlib `ast`). The graph
   is the source of truth; the LLM only narrates or fills gaps for uncovered languages.
4. **No reuse across sessions.** Results persist in a **bitemporal, provenance-tracked knowledge
   graph** (`models/graph.py`: Node/Edge with `valid_from`/`valid_to`, `certainty`, `created_by`,
   `NodeSource`, `AnalysisRun` per git_sha) and are re-queryable by **MCP tools**
   (`mcp/server.py`: `search_symbols`, `find_callers`, `find_callees`, `impact_analysis`,
   `get_contract`, `get_data_access`, `get_module_summary`, `list_flows`, `find_runtime_path`, …)
   and the dashboard chat. An AI **re-queries stored facts instead of re-ingesting the repo**.
5. **Blind to cross-service + runtime reality.** **Contract-id normalization** links a C#
   endpoint and a TS `fetch` to the *same* node (`merge/contract_id.py`); **OTLP runtime
   reconciliation** marks which edges are *actually exercised in production*
   (`merge/runtime.py`); risk scoring weights blast-radius + exercised (`merge/findings.py`).

It then **presents results as clear tables/views** in the dashboard, and answers questions in the
**Chat tab** grounded in the analysis (multi-provider: Claude/OpenAI/Gemini/Atlas — `api/chat.py`,
`api/llm_providers.py`, configured in Settings → AI 제공자).

## Hard guardrails — do NOT drift

- **Stay on purpose.** New work must map to a pillar above, or to clearer table presentation, or
  to AI-reuse. If it doesn't, it is scope creep — ask first. Don't build generic-app features.
- **Honesty over polish.** Preserve the **verified vs inferred** distinction. An LLM guess must
  never masquerade as a verified fact. Document analyzer limitations (as `ggoss-py` does).
- **The graph is the source of truth.** LLM output narrates/fills gaps; it never overrides
  deterministic extraction.
- **Scale-safe always.** Anything touching analysis stays bounded (budget chunking, priority
  ranking, incremental `evidence_hash` skip). Never assume the whole repo fits in memory/context.
- **Not a generic app.** Auth/secrets/settings/orgs exist *only* to serve the analysis mission.

## Known gaps (Phase-2 — do not claim as done)

- ggoss-py cross-module call resolution is name-based only (no full import graph).
- L3 module boundary is a path-segment heuristic; L4/L5 summaries are not built.
- First-pass coverage is bounded by a symbol `limit`; the long tail of a 100k-symbol repo may be
  shallow on the first pass.
- LLM-extracted languages (Go/Rust/C++/…) are `certainty="inferred"` only — lower trust than the
  deterministic analyzers.

## Stack quick-map

`analyzers/ggoss-*` (deterministic extractors) · `server/app/analyzers/runner.py` (subprocess
runner) · `server/app/extractor/` (graph ingest + L1–L3 recursive summaries + LLM fallback +
grounding) · `server/app/models/graph.py` (bitemporal graph) · `server/app/merge/` (contract-id,
runtime reconcile, findings/risk) · `server/app/mcp/` (AI-reuse tools) · `server/app/orchestrator/`
(staged pipeline, budgets, SSE progress) · `server/app/dashboard/` + `server/app/api/` (UI + API).
Docker-free local run: `python -m app.serve_local` (SQLite + fakeredis + inline jobs).
