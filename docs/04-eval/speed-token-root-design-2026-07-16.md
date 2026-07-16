# 속도·토큰 근본 개선 연구와 설계 — 2026-07-16

> 범위: deterministic source indexing, optional Agent/L1–L3, grounded Chat,
> run-scoped graph staging. Mnemos의 일반 제품 기능이 아니라 `CLAUDE.md`의
> scale/grounding/determinism/AI-reuse 목적만 다룬다.

## 1. 결론

이번 조사에서 서로 다른 세 문제가 확인됐다.

1. Agent extraction과 게시 후 L1–L3가 **같은 분석 실행인데도 각각 새
   `LLMRunBudget`을 만들어** 구성상 64 calls/120 K estimated-input ceiling을 두 번
   받을 수 있다.
2. Chat의 rewrite와 answer, Claude의 retry/fallback은 bounded prompt/output은
   갖지만 **요청 전체의 물리 호출 예산과 공통 원장**이 없다. provider adapter가
   반환한 usage도 버린다.
3. analyzer JSONL ingest는 staged fact마다 `session.get()`을 실행한다. live graph의
   atomic publication은 보존되지만, staging DB round trip이 fact 수에 비례한다.

해결 경계는 다음과 같다.

- 기본 full/incremental은 계속 `summarize=false`, `agent_extract_limit=0`이며 LLM
  client와 budget을 만들지 않는다. 최초 LLM token 비용은 구조적으로 0이다.
- 한 번의 중단 없는 source worker 실행에서는 Agent와 L1–L3가 하나의 유한 budget을
  공유한다. budget은 첫 실제 provider 필요 시점 전에만 만든다.
- provider usage는 provider별 원본 구성요소를 잃지 않고 저장한 뒤 canonical 합계를
  파생한다. estimated input은 actual/billed usage로 가장하지 않는다.
- staging은 50개 단위 identity 조회와 동일 reducer로 바꾼다. graph publication CAS,
  coverage seal, certainty, owner union, conflict, semantic hash 의미는 바꾸지 않는다.

## 2. 확인한 실행 경로

### 2.1 optional analysis budget

`run_ingest`는 unavailable-language Agent를 실행할 때 budget A를 만들고 전달한다.
그러나 source receipt가 durable해진 뒤 `_run_published_postprocess`가 summary용 budget
B를 새로 만든다. Agent와 summary를 모두 켜면 하나의 opt-in run이 두 ceiling을 받는다.

올바른 수명은 다음과 같다.

| 실행 경로 | budget 계약 |
|---|---|
| Agent off, summary off | 생성하지 않음 |
| summary only | 첫 summary 직전에 하나 생성 |
| Agent only | 첫 실제 non-skipped Agent stage 직전에 하나 생성 |
| Agent + summary | Agent에서 만든 같은 객체를 L1–L3까지 전달 |
| Agent 옵션 on, 실제 fallback 없음 | Agent 때문에 생성하지 않음 |
| published hard-crash resume | 새 worker execution budget; 이전 physical calls는 ledger에 보존 |
| continuation | 새 `AnalysisRun`의 하나의 fresh budget을 L1–L3가 공유 |

현재 budget은 process-local이다. 따라서 worker crash 뒤 같은 published run을 resume할
때 AnalysisRun 전체에 걸친 영구 reservation ceiling을 복원하지는 못한다. 이를
해결하려면 DB atomic reservation counter가 필요하다. 이번 변경에서 “run 전체 영구
상한”이라고 과장하지 않고 **한 worker execution의 hard ceiling**으로 명시한다.

### 2.2 physical provider attempts

하나의 논리 Chat 요청이 다음 물리 시도를 만들 수 있다.

```text
grounded chat request
  ├─ optional search-term rewrite
  │    └─ selected backend attempt(s)
  └─ answer
       ├─ selected backend attempt
       └─ retry or API↔subscription fallback attempt
```

예산과 원장은 논리 `provider_chat()` 호출이 아니라 실제 network/SDK 시도 직전에
reservation/start를 기록해야 한다. init 실패 뒤 동일 prompt를 다시 보내는 시도도
별도 physical attempt다. process가 중단돼 finish update가 없으면 `started`가 남아야
미지 비용을 0으로 오인하지 않는다.

## 3. provider usage 정규화 계약

공식 API 계약을 확인한 결과 provider별 필드를 하나의 `total_tokens` 숫자로 먼저
뭉개면 cache/reasoning 차이를 복구할 수 없다.

| provider | 보존할 원본 구성요소 | canonical 파생 규칙 |
|---|---|---|
| OpenAI Chat Completions | `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens`, completion reasoning tokens, provider total | input=`prompt_tokens`, output=`completion_tokens`; cached/reasoning은 subset으로 별도 보존 |
| Anthropic Messages | `input_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `output_tokens` | total input은 세 input 구성요소의 합; 각 cache 구성요소 보존 |
| Gemini generateContent | `promptTokenCount`, `cachedContentTokenCount`, `candidatesTokenCount`, `thoughtsTokenCount`, `totalTokenCount` | prompt/candidate를 input/output으로 정규화하되 cached/thoughts는 subset으로 보존 |
| subscription/Atlas/unknown proxy | usage가 없으면 actual fields는 `NULL` | request 직전 estimate만 별도 필드에 저장 |

참고한 공식 계약:

- OpenAI Chat Completions API와 prompt caching:
  <https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create>,
  <https://developers.openai.com/api/docs/guides/prompt-caching>
- Anthropic Messages와 rate-limit token counting:
  <https://platform.claude.com/docs/en/api/messages/create>,
  <https://platform.claude.com/docs/en/api/rate-limits>
- Gemini generateContent와 token counting:
  <https://ai.google.dev/api/generate-content>,
  <https://ai.google.dev/api/tokens>

### 3.1 네 층의 structured boundary

| 층 | 소유 계약 |
|---|---|
| outer envelope | bounded HTTP/SDK response object와 provider request/response id |
| provider dialect | OpenAI/Anthropic/Gemini의 documented usage fields만 adapter가 읽음 |
| canonical schema | physical attempt, provider/backend/model, operation, actual raw components, estimated input, output cap, status/reason |
| consumers | request/run hard budget, project cost view, audit/diagnostics; unknown usage를 0으로 간주하지 않음 |

필드가 없거나 타입/범위가 잘못되면 adapter는 추측하지 않는다. provider가 준 total과
구성요소가 모순되면 raw evidence를 보존하고 usage를 `invalid`로 표시하며, 비용
계산의 authoritative input으로 쓰지 않는다.

## 4. graph staging 성능 설계

첫 slice는 schema나 publication 의미를 바꾸지 않는 bounded batch merge다.

1. immutable node/edge candidate를 최대 50개 모은다.
2. batch transaction에서 owning `AnalysisRun`을 lock/check한다.
3. batch identities의 기존 stage rows를 table별 한 번에 조회한다.
4. 입력 순서대로 기존 singleton reducer와 같은 규칙을 적용한다.
5. flush/commit 뒤에만 progress를 게시한다.

반드시 보존할 불변식:

- 동일 payload의 producer owner는 canonical union이다.
- 다중 owner identity에 다른 payload가 오면 전체 batch는 conflict로 실패한다.
- 같은 producer의 certainty downgrade는 무시한다.
- durable runtime overlay field는 stage hash에 들어가지 않는다.
- coverage seal 뒤의 모든 write는 거부한다.
- promotion 전 live `nodes`/`edges`는 변하지 않는다.
- singleton API와 batch API의 최종 row/semantic hash가 동일하다.

PostgreSQL 전용 upsert는 첫 slice에 사용하지 않는다. bounded `IN`/tuple-`IN`, generic
SQLAlchemy ORM insert/update로 SQLite와 PostgreSQL 의미를 같이 유지한다.

O(graph) omission sweep을 없애는 contribution ledger는 별도 migration/cutover가
필요하다. 안전한 순서는 shadow contribution schema → dual-write/materialization
검증 → producer-indexed omission → generation-pinned deletion intent다. batch staging과
한 번에 섞어 배포하지 않는다.

## 5. 수용 기준과 증거 수준

### 5.1 correctness

- Agent 10 calls 뒤 summary 20 calls면 최종 shared budget은 30 calls다.
- exhausted supplied budget을 postprocess가 새 객체로 교체하지 않는다.
- Chat retry/fallback 각각이 별도 physical attempt와 reservation이다.
- usage가 없는 subscription path의 actual token columns는 `NULL`이며 estimate와
  분리된다.
- 1,000 staged facts에서 identity SELECT 수는 row 수가 아니라 batch 수에 비례한다.
- singleton/batch owner union, conflict, downgrade, seal 결과가 동일하다.

### 5.2 성능 측정

wall-clock 단독 CI gate는 host noise 때문에 사용하지 않는다. 다음을 같이 기록한다.

- SQL statement/identity SELECT count;
- facts/sec와 batch 수;
- peak RSS;
- 10 K/50 K fixture stage/promotion time;
- cancellation/rollback과 commit-before-progress.

### 5.3 E0–E4 honesty

| 수준 | 이 변경의 목표 |
|---|---|
| E0 | migration/model/adapter/budget 계약 정적 일치 |
| E1 | provider usage parser, shared budget, batch reducer unit regression |
| E2 | mock provider → physical ledger → Chat/summary consumer, analyzer JSONL → stage batch → promotion |
| E3 | credential가 있는 작은 real-provider canary; 없으면 미실행으로 명시 |
| E4 | representative repository + real PostgreSQL + 50 K soak/A-B token 비교; 현재 미실행 |

E1/E2를 통과해도 live provider 비용 정확도나 50 K production 성능을 완료했다고
주장하지 않는다.

## 6. 개발 순서

1. shared Agent/L1–L3 budget과 회귀 테스트;
2. portable stage batch reducer와 analyzer buffer integration;
3. provider raw usage canonicalization과 durable physical-attempt schema;
4. Chat rewrite/answer/retry/fallback의 request-scoped budget/ledger 연결;
5. focused + broad regression, query-count benchmark;
6. commit/push와 GitHub PR 검증;
7. 후속 contribution ledger와 real PostgreSQL/50 K soak.

## 7. 2026-07-16 구현 결과

### 7.1 완료된 변경

- source run의 Agent extraction과 게시 후 L1–L3가 동일한
  `LLMRunBudget`을 공유한다. deterministic fallback이 실제로 필요하지 않으면 budget을
  만들지 않는다.
- Chat rewrite, answer, Claude SDK init retry, API↔subscription fallback은 요청 하나의
  call/input/output/wall budget을 공유한다. 실제 dispatch 직전에 매번 reservation하며
  fallback은 ceiling이나 deadline을 초기화하지 않는다.
- `LLMRunBudget`은 기존 call/input/wall에 cumulative requested-output ceiling을
  추가했다. legacy caller는 output reservation 0으로 호환되고, Chat처럼 provider cap을
  아는 caller는 dispatch 전에 전량 예약한다.
- deterministic analyzer stage는 최대 50 fact를 buffer하고 node/edge batch reducer를
  실행한 뒤 commit하고 progress를 게시한다.
- `mnemos.llm_usage.v1` / `mnemos.llm_physical_attempt.v1` 정규형과 0036 expand-only
  `llm_calls` schema를 추가했다. 기존 writer/reader와 legacy columns는 유지된다.

### 7.2 재현 가능한 개선 폭

| 항목 | 변경 전 | 변경 후 | 해석 |
|---|---:|---:|---|
| Agent + L1–L3 한 worker 실행의 구성상 ceiling | 128 calls / 240 K estimated input / 두 600 s budget | 64 calls / 120 K estimated input / 하나의 600 s deadline | 상한 중복 제거: call/input 50% 감소 |
| Chat Claude rewrite+answer의 구성상 physical attempts | 각 논리 호출당 API/SDK retry·fallback 최대 3, 합계 최대 6 | 요청 전체 최대 4 | fallback 최악 호출 수 33.3% 감소; 일반 단일 성공 호출 수는 변하지 않음 |
| Chat cumulative reservation | 없음 | input 120 K, requested output 4,800, wall 10–300 s | provider usage가 없어도 finite; UTF-8 byte 기반 input은 billed-token 주장이 아님 |
| 120 unique symbol staging identity SELECT | 120 | 3 (`ceil(120/50)`) | test에서 97.5% 감소; 전체 wall time 개선률로 일반화하지 않음 |

batch reducer 자체는 table별 batch당 identity SELECT 한 번을 검증했다. 실제 처리량은
DB latency, insert volume, analyzer subprocess 비중에 따라 달라지므로 97.5%를 전체 분석
시간 단축률이라고 부르지 않는다.

### 7.3 검증 결과

- 변경 focused suites: shared budget/Chat, graph batch/deadline/publication, provider usage
  contract/migration/legacy compatibility 모두 통과;
- non-integration 전체 suite를 파일 기준 네 shard로 정확히 한 번씩 실행:
  **2,318 passed, 26 skipped, 29 integration deselected, 0 failed**;
- Ruff와 Python 3.12 compileall 통과;
- Alembic single head: `0036_llm_physical_attempt`;
- `git diff --check` 통과.

### 7.4 정직한 미완료 범위

- 0036은 expand-only contract/schema와 strict normalizer까지다. 현재 기존 summary,
  Agent, Flow, Chat writer를 전부 v1 started/finalized row로 전환하지 않았으므로 durable
  physical-attempt ledger coverage가 완성됐다고 주장하지 않는다.
- 실제 PostgreSQL upgrade/downgrade와 concurrent dollar reservation은 실행하지 않았다.
- live OpenAI/Anthropic/Gemini call로 usage component를 확인하지 않았다. E3 미실행이다.
- real PostgreSQL 50 K-file soak, contribution-ledger omission, unseen-repository direct-AI
  대비 A/B token·latency·정답률은 E4 후속 gate다.

따라서 이번 결과는 **주요 call/input budget 중복과 per-fact staging 조회의 직접 원인을
제거하고, 전면 호출 원장 전환을 위한 손실 없는 schema를 배포 가능하게 만든 단계**다.
Phase-2의 contribution ledger와 모든 AI surface의 v1 dual-write까지 완료한 것으로
확대 해석하지 않는다.
