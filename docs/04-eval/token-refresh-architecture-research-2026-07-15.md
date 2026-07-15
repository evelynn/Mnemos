# 토큰·갱신 중단 구조 연구와 개선 결과 — 2026-07-15

> **Historical checkpoint.** 이 문서는 원인 조사와 Phase-A 시점의
> 상태를 기록한다. 현재 `run_ingest`는 run-scoped staging, coverage seal,
> atomic `GraphHead`/receipt publication, durable overlay, source/overlay-pinned
> readers를 사용한다. 아래의 “live Node/Edge writer”와 “원자적 게시
> 미연결” 표현은 현행 코드가 아닌 당시 문제를 설명한다. 현재 계약과
> 증거는 [Phase-B 보고서](atomic-graph-publication-phase-b-2026-07-15.md)를
> 본다.

## 결론

Mnemos는 **AI가 저장소 전체를 먼저 읽거나 요약하게 만드는 도구**로 사용하면 비용과
지연 면에서 가치가 낮다. 반대로 다음 계약에서는 유효한 source-reference / analysis
guide다.

> 소스를 결정적으로 인덱싱하고, AI가 질문에 필요한 symbol·edge·contract·data-access와
> 정확한 source range만 작게 재조회하게 한다. AI는 근거를 참고해 분석하되 optional
> narration을 source truth로 사용하지 않는다.

따라서 기본 full/incremental은 `summarize=false`, `agent_extract_limit=0`이며 LLM을
생성하지 않는다. 기본 최초 분석의 LLM 비용은 구조적으로 0이다. AI 파일 추출과
L1→L3 narration은 명시적 opt-in이며, 이번 변경으로 둘이 하나의 유한한 실행 예산을
공유한다.

이번 연구 결과는 개선 효과가 충분히 커서 런타임 개선과 원자적 게시 Phase A 설계를
개발했다. 다만 기존 `run_ingest`는 아직 live Node/Edge writer를 사용한다. 그러므로
실패한 run의 mixed-current-graph 근본 버그가 완전히 해결됐다고 주장하지 않는다.

## 측정 결과

아래 값은 이 작업 트리와 Windows 로컬 환경의 재현값이다. provider별 실제 tokenization은
다르므로 prompt 문자는 토큰의 근사 지표이며, live provider 청구량은 아니다.

| 경로 | 이전 위험/측정 | 변경 후 계약 | 판정 |
|---|---:|---:|---|
| 기본 source index | narration이 기본 흐름과 섞이면 수십 회 호출 가능 | LLM client 0개, 0 LLM tokens | 제품 기본값으로 채택 |
| Chat 상한 입력 fixture | provider 입력 185,696 chars (약 46K tokens@chars/4) | 두 로컬 재현에서 20,011–21,468 chars, hard max 24,000 chars | 보수적으로 최소 88.44% 감소 |
| Optional AI 작업 | API 상한 조합의 이론적 최악은 약 4,800 physical calls / 16.2M input tokens | 공유 run budget 64 call reservations / 120K estimated input tokens / 600s; 실제 호출은 별도 ledger | 무제한 확장 제거 |
| Exact Git manifest | clean detached checkout byte hash cold 9.197s | Git tree 1.537s | cold 약 6배 개선 |
| Warm small repo manifest | byte hash 1.045–1.341s | Git tree 1.537–1.652s | 15–58% 느릴 수 있음 |
| Changed Python family | 4 verbs 약 23.5s, 22,184 records | 아직 동일 family 전체 재처리 | 다음 구조 개선 대상 |
| DB replay sample | 10K edge replay 약 22.9s; 해당 측정에서 stage의 약 93%가 DB replay | 아직 live per-record merge | staging/bulk promotion 필요 |

Git 결과는 source body를 열지 않는 이점이 OS cache가 차가운 새 checkout에서 크고, 이미
warm한 2.9MB급 작은 저장소에서는 Git process 고정비가 더 클 수 있음을 보여준다.
따라서 “항상 빠르다”가 아니라 exact detached worktree의 cold/large-repo 경로에 채택했다.

## 확인된 근본 원인

1. Chat history·context·provider output에 합산 hard cap이 없었다.
2. L1–L3의 target/chunk 제한은 있었지만 전체 run 호출·입력·절대시간 상한이 없었다.
   달러 budget 기본값은 꺼져 있고 subscription은 usage를 반환하지 않아 보호가 되지 않았다.
3. AI file extraction은 파일당 150초, 최대 500개까지 순차 호출할 수 있었고 전체 예산을
   narration과 공유하지 않았다. 큰 파일은 prefix만 보고도 완전한 결과처럼 보일 수 있었다.
4. `StageTracker`는 progress 시점의 경과시간만 검사했다. analyzer가 끝난 뒤 DB
   SELECT/upsert/commit이 막히면 stage deadline이 실제 중단 경계가 아니었다.
5. incremental manifest가 파일 본문을 다시 읽었고, changed analyzer family는 결과를
   per-record로 전부 replay했다.
6. analyzer가 50-row 단위로 current Node/Edge를 commit했다. 후반 stage 실패 시 이전
   완료 graph와 실패 run의 일부 row가 섞였다.
7. MCP guard가 먼저 도입됐지만 Ask/Data/Flow/Chat 등 HTTP graph consumer 일부가 같은
   fail-closed 계약을 사용하지 않았다.

## 구현한 개선

### 1. 토큰과 provider 작업의 절대 상한

- 기본 indexing은 `summarize=false`, `agent_extract_limit=0`이며 LLM client를 만들지 않는다.
- `LLMRunBudget`은 AI file extraction과 L1–L3 전체가 공유한다.
- 기본 hard cap은 64 call reservations, 120K estimated input tokens, 600초다. 이
  in-memory reservation과 실제 provider 호출 원장은 서로 다른 계약이다.
- 입력 예산은 provider 호출 전에 예약하고, 남은 절대시간으로 inflight call을 취소한다.
- `LLMCall`은 L1–L3 map/reduce partial, Agent file extraction, Agent flow의 실제 호출을
  Summary/graph 제품과 분리해 기록한다. timeout·거부·schema/grounding 실패도 호출 사실을
  유지하고, SDK가 신뢰할 token usage를 주지 않으면 `tokens_used=NULL`로 남긴다.
- provider 전 preflight·budget·no-backend 실패는 physical call로 꾸미지 않는다. 같은
  no-backend 상태는 shared run budget을 중단해 대상마다 반복하지 않는다. 비용 API는
  `physical_call_count`와 `unknown_token_calls`를 따로 노출하며 달러 추정은 알려진 token만 합산한다.
- 한 파일 AI extraction은 16K chars를 넘으면 provider 전에 거부하고, orchestrator도
  16,001자까지만 읽어 giant file을 메모리에 전부 올리지 않는다.
- agent summary output은 64KiB, extraction/flow output은 256KiB로 제한한다.

### 2. Chat을 bounded graph guide로 변경

- history 10K, graph context 8K, overview 2K, source excerpt 총 6K/항목 1.6K chars.
- system+user provider input 전체를 24K chars 이하로 검증한다.
- history message, overview fact, symbol record, source line은 중간 절단하지 않는다. 너무 큰
  record는 건너뛰고 뒤의 작은 유효 record를 계속 pack한다.
- 정적 한국어 concept expansion이 강한 hit를 만들면 rewrite LLM call을 생략한다.
- rewrite는 exact JSON array(3–8 unique terms, 각 64 chars 이하)만 허용한다.
- answer 1,200 output tokens, rewrite 128 tokens를 요청한다. OpenAI 공식 endpoint와
  reasoning 계열은 `max_completion_tokens`, legacy 호환 proxy는 `max_tokens`, Gemini와
  Anthropic API는 각 provider field를 사용한다. Claude subscription은 stream character
  ceiling, Atlas는 bounded streaming JSON byte ceiling과 message character ceiling으로
  제한하며 초과 응답 전체를 거부한다.
- 임의 host `source_root`를 읽지 않고 완료 run의 immutable snapshot reader만 사용한다.

### 3. 갱신이 끝나지 않는 경로의 중단 보장

- analyzer verb stage의 30분 absolute deadline이 subprocess stream뿐 아니라 output
  validation, graph SELECT/upsert, commit, progress flush까지 감싼다.
- DB await 중 timeout/cancel에도 async generator를 닫아 child process를 terminate→kill한다.
- timeout은 partial/failed로, cancellation은 cancelled로 terminalize한다.
- analyzer process 자체도 queue 256, JSONL record 1MiB, line/stream/wall bounds를 갖는다.

### 4. 증분 fingerprint와 stale sweep의 정확성·비용

- exact Git snapshot은 `git ls-tree -rlz --full-tree`의 path/mode/type/blob OID/size로
  analyzer-family fingerprint를 만들며 source body를 열지 않는다.
- symlink mode, submodule, hidden/generated/excluded path를 producer contract에 맞춰 제외한다.
- 임의 direct caller는 exact SHA, checkout HEAD/prefix, tracked/untracked/ignored dirty 상태를
  fail-closed 검증한다.
- `prepare_source`가 방금 만든 detached worktree만 trusted fast path를 사용한다.
- contract v4 전환으로 기존 manifest는 한 번 authoritative rebuild한다.
- ggoss-ts fingerprint는 root `tsconfig.json`뿐 아니라 상대경로와 Node-style package
  resolution으로 연결되는 bounded `extends` closure도 포함한다. exact Git 경로는 해당
  config blob OID를, mutable 경로는 bounded content digest를 사용한다.
- visible `node_modules/@types` 또는 source authority 밖의 package config가 있으면 ggoss-ts는
  외부 파일을 읽어 지문을 꾸미지 않고 non-cacheable로 내려 실제 analyzer를 다시 실행한다.
- ggoss-csharp는 `.props`/`.targets`, `global.json`, NuGet config/lock 입력도 지문에 넣는다.
  그러나 MSBuildWorkspace가 읽는 외부 SDK·NuGet/import closure 전체를 아직 고정하지 못하므로
  이 지문만으로 skip하지 않고 항상 non-cacheable로 fail closed한다.
- read-only source mount의 수동 Git 분석은 원본 repository에 worktree metadata를 쓰지
  않고 Mnemos-controlled private mirror를 만든 뒤 detached worktree를 등록한다.
- deterministic shared fact 삭제는 이전/현재 producer 전체가 authoritative refresh 또는
  명시적 제거된 경우에만 수행한다. 그렇지 않으면 ownership sweep을 보류한다.
- Agent fact는 격리된 `agent:<language>` owner가 모든 대상 파일을 성공했거나 retire된 경우만
  sweep한다. 변경 파일에서 사라진 Agent symbol은 call linker 입력에서 먼저 제외하고,
  `link_calls` edge는 완전 재계산이 끝난 뒤에만 sweep한다. partial Agent pass는 agent
  owner sweep을, subtree run은 source deletion sweep 전체를 허가받지 못한다.

### 5. 실패한 refresh의 소비 차단

- unpublished refresh가 있으면 current-graph MCP와 Ask/Data/Flow/Chat/current analysis HTTP
  routes가 동일한 structured 409 오류로 fail closed한다.
- diagnostic project index와 명시적으로 pinned historical source read는 복구를 위해 남긴다.
- 이는 mixed graph를 고치는 것이 아니라 잘못 소비하지 못하게 하는 interim safety다.

### 6. Atomic publication Phase A

Migration 0027과 `graph_heads`, `graph_node_stage`, `graph_edge_stage`, CAS promotion primitive를
추가했다. 동일 transaction에서 version close/insert, head generation 교체,
`AnalysisRun.completed`를 commit하고, 강제 실패 시 모두 rollback하며 stage는 유지한다.
기존 project는 `needs_rebuild`로 시작해 legacy graph를 자동 신뢰하지 않는다.

Phase A의 한계도 의도적으로 fail closed한다.

- 현재 stage는 graph producer/file contribution ledger가 아니므로 changed multi-owner fact는
  `MultiProducerContributionRequired`로 전체 rollback한다.
- authoritative deletion은 transaction 안에서 current graph를 page scan하므로 memory만
  bounded이고 DB work는 O(graph)다.
- 무엇보다 `run_ingest`가 아직 stage writer로 전환되지 않았다. Phase B 전까지 실제 current
  graph publication은 atomic하지 않다.

## 채택·기각한 설계

| 제안 | 결정 | 이유 |
|---|---|---|
| 기본 eager AI narration | 기각 | source reference에 필수 아님, 비용·지연이 먼저 발생 |
| deterministic index + MCP re-query | 채택 | 재사용 가능하고 질문별 context가 작음 |
| dollar budget만 사용 | 기각 | 기본 disabled, subscription usage unknown |
| call/input/wall shared budget | 채택 | backend와 청구 방식에 무관한 hard boundary |
| Git tree OID manifest | 조건부 채택 | cold exact checkout에 효과, warm small repo 이점 없음 |
| Redis/DB advisory lock만으로 writer 안전화 | 기각 | worker가 다른 session으로 계속 쓸 수 있어 fencing/atomicity가 없음 |
| staging + generation CAS promotion | 채택 | 실패 run의 row를 current graph에서 격리하는 올바른 경계 |

## 검증 수준

Structured-output quality gate 기준으로 현재 증거는 다음과 같다.

- **E0**: 목적·schema·budget·snapshot·promotion 계약과 실패 의미를 코드/문서에 명시.
- **E1**: Ruff, compileall, Node syntax, Alembic single-head/DDL compile, diff check.
- **E2**: fake provider/SDK, real SQLite transaction, subprocess/timeout, HTTP/MCP guard,
  manifest Git repository와 strict extractor normalizer를 사용하는 regression 경로가 있다.
  최종 worktree의 229개 `test_*.py`를 정렬된 4개 shard로 나눠 검증했고, 부하성
  subprocess timeout을 단독 재현해 구분한 뒤 실패 shard와 변경 영향군을 재실행했다.
  최종 합계는 **2,015 passed, 19 skipped, 19 deselected**다.
- **E3 미수행**: live provider token/latency, real PostgreSQL promotion/concurrency 미검증.
- **E4 미수행**: 50K-file crash/refresh soak와 실제 사용자 workflow 비교 미검증.

따라서 이 문서는 토큰과 중단 위험이 유의하게 감소했다는 E2 근거는 제공하지만,
production-ready 또는 대규모 원자 refresh 완료를 주장하지 않는다.

## 다음 개발 순서

1. **Phase B staging 전환**: analyzer/agent/live-schema writer를 run stage로 교체하고 기존
   worker를 drain한 뒤 generation CAS promotion을 활성화한다.
2. **Graph producer/file contribution ledger**: 다중 producer ownership과 file별 deletion intent를
   보존하고 O(graph) sweep을 indexed anti-join/bulk SQL로 대체한다.
3. **Prepared-delta 비례 promotion**: per-record SELECT/upsert 대신 stage bulk load와 semantic
   merge로 DB round trip/WAL/lock 시간을 줄인다.
4. **Per-file dependency-aware cache**: changed family 전체 walk를 file fingerprint + dependency
   closure로 축소한다.
5. **Fenced lifecycle**: crashed owner의 9시간 Redis lease 문제와 rapid webhook queue를
   fencing/coalescing으로 개선한다.
6. **E3/E4**: live provider, real PostgreSQL concurrent reader/failure matrix, 50K-file soak를
   통과한 뒤에만 대규모 운영 적합성을 판정한다.
