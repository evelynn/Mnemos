# 속도·토큰 근본 개선 연구·설계·검증 — 2026-07-16

> 범위: deterministic source indexing, optional Agent/L1–L3, grounded Chat,
> Flow, Second Opinion, durable LLM accounting, PostgreSQL publication, MCP
> re-query. 이 문서는 구현 전 추정이 아니라 현재 코드와 실행 증거를 기록한다.

## 1. 결론

이번 작업으로 닫은 문제는 “LLM을 덜 쓴다”는 홍보 문구가 아니라 다음 실패 모드다.

1. provider 호출이 durable 원장보다 먼저 나가는 crash window;
2. 동시에 시작한 호출이 같은 project dollar cap을 각각 통과하는 check-then-act;
3. retry/restart가 같은 의미의 작업을 다시 과금하는 문제;
4. 공식 가격으로 승인했지만 custom proxy나 환경 override로 실제 목적지가 바뀌는 문제;
5. 후보는 저장됐지만 최종 제품 게시와 시도 분류가 갈라지는 문제;
6. opaque Agent SDK/Atlas/cloud embedding의 비용·출력·사용량을 증명하지 못하면서
   실행 가능하다고 취급하던 문제.

고정된 안전·회계 완료 기준에서는 새 root blocker가 없다. 기본 source index는 여전히
LLM client를 만들지 않고 0 call/0 token이다. 선택적 유료 생성은 이제 양수 project cap,
immutable worst-case price/route contract, 원자 예약, durable `STARTED`, provider-enforced
output cap을 모두 만족해야 한다. 하나라도 없으면 네트워크 전에 실패한다.

이 결과는 실제 사용 토큰 절감률이나 `codebase-memory-mcp`(CBM) 대비 성능 우위를
증명하지 않는다. 오히려 전체 model input ceiling을 no-refund로 예약하므로 실제 prompt보다
크게 과예약될 수 있다. 이는 비용 폭주 방지의 안전성 개선이며 utilization 개선이 아니다.

## 2. 최종 설계

### 2.1 index first, AI on demand

- 정상 full/incremental source analysis는 LLM backend를 구성하지 않는다.
- analyzer JSONL, bounded queue, batch staging, generation CAS, bitemporal publication이 먼저다.
- Chat/Summary/Second Opinion 등 AI 제품은 이미 게시된 project/run/revision/evidence에
  결합된다.
- prompt와 provider output은 공통 attempt row에 저장하지 않는다. 입력은 fingerprint,
  결과는 암호화된 schema-normalized candidate로만 보존한다.

### 2.2 하나의 physical-attempt lifecycle

모든 production paid-generation owner는 공통 lifecycle capability를 설치한다.

| 제품 경로 | network transport | production 판정 |
|---|---|---|
| Chat OpenAI | official Chat Completions | price-attested, policy가 있을 때 실행 가능 |
| Chat Gemini | official generateContent | price-attested, policy가 있을 때 실행 가능 |
| Chat/summary/Second Opinion Anthropic | official Messages API, SDK retry 0 | price-attested, policy가 있을 때 실행 가능 |
| Agent SDK Summary/Flow/AI extraction | opaque `query()` | immutable price/route/output 계약 부재로 pre-network 차단 |
| Atlas generation | session + message 2-POST | output/usage/price receipt 부재로 pre-network 차단 |
| Voyage/OpenAI cloud embedding | legacy scaffold | project attempt/settlement/price 계약 부재로 실행 함수에서 차단 |

한 attempt의 순서는 다음과 같다.

1. stable operation/input/product binding 계산;
2. exact terminal candidate replay 확인;
3. project policy와 price-catalog version 확인;
4. project row serialization 아래 worst-case USD 원자 예약;
5. reservation과 `STARTED`를 commit;
6. DB가 반환한 remaining wall time 안에서 한 번만 dispatch;
7. usage/model/finish reason/schema/grounding 검증;
8. encrypted canonical candidate와 terminal outcome commit;
9. 제품 소유자가 exact binding을 다시 검증하고 product + classification을 원자 commit.

재시작 시 `STARTED`는 완료로 추측하지 않는다. terminal candidate가 있으면 provider를
호출하지 않고 replay하고, terminal-without-candidate는 같은 실패를 재현한다. 정책이 나중에
삭제되거나 cap이 소진되어도 이미 승인·완료된 terminal replay는 가능하다.

### 2.3 immutable worst-case dollar policy

`server/app/llm/pricing.py`의 계약은 exact provider, billing route, model id, official API
base, 공식 input ceiling, 공식 output ceiling, 보수적 input/output 가격을 하나의 versioned
fact로 묶는다. catalog canonical form의 SHA-256에서 project policy version을 파생하므로
가격·model ceiling·목적지가 바뀌면 기존 worker/policy 조합은 자동으로 fail closed한다.

- Anthropic Sonnet 4.6: 1M input 전체와 requested output을 보수적 장문 가격으로 예약;
- OpenAI GPT-4o dated model: 128K input 전체와 requested output 예약;
- Gemini 2.5 Flash: 1,048,576 input 전체와 requested output 예약;
- unknown alias, custom proxy, opaque subscription route: 계약 없음, dispatch 없음;
- reservation은 환불하지 않는다. provider usage는 실제 비용 관측용이지 과거 승인을
  소급해서 느슨하게 만드는 근거가 아니다.

unset/0 cap은 “무제한”이 아니라 `project_dollar_budget_required`다. 동시에 들어온 호출은
같은 project account row와 advisory/row lock 아래 직렬화되어 각각의 worst-case 금액을
합산한다.

### 2.4 목적지 attestation

- OpenAI는 HTTPS, exact `api.openai.com`, exact `/v1`, default/443 port, no userinfo/query/
  fragment인 의미적으로 동일한 공식 root만 가격 계약을 가진다.
- custom/http/wrong path/port/userinfo/query URL은 billing route를 잃고 lifecycle start에서
  network 전에 거절된다. 모델 목록 probe도 `httpx` client 생성 전에 거절되어 API key를
  custom host로 보내지 않는다.
- 세 Anthropic client는 contract-derived `https://api.anthropic.com`을 명시하므로
  ambient `ANTHROPIC_BASE_URL`이 source-bearing request를 우회하지 못한다.
- Gemini endpoint도 같은 price contract에서 파생한다.

### 2.5 candidate와 제품 게시 원자성

- Chat answer, Summary, Flow, Agent extraction, Second Opinion candidate는 contract name과
  project/run/revision/evidence binding을 가진다.
- Summary/Flow/Second Opinion은 exact candidate lock → binding/schema 검증 → product upsert
  → attempt classification을 한 transaction으로 수행한다.
- product insert가 실패하면 classification도 rollback된다. 재시작은 저장된 candidate를
  사용하며 provider call을 반복하지 않는다.
- `Summary.tokens_used`/`model_used`는 선택된 최종 candidate receipt만 가리킨다. 전체
  map/reduce 물리 호출량은 component `LLMCall` ledger가 source of truth다.

## 3. 검증 게이트

| 게이트 | 2026-07-16 결과 |
|---|---|
| 계약 변경 집중 회귀 | 192 passed |
| 최종 전체 비통합 회귀 | 2,749 passed, 26 skipped, 39 deselected, 0 failed, 446.43 s |
| 실제 PostgreSQL migration | PostgreSQL 17.10, `0041_summary_llm_provenance (head)` |
| 실제 PostgreSQL ledger/replay/concurrency 묶음 | 85 passed, 62.80 s |
| provider route/price 집중 묶음 | 147 passed |
| provider route/price 넓은 회귀 | 130 passed, 3 skipped; 별도 실제 PostgreSQL atomic-dollar 3 passed |
| static | production/tests/benchmark Ruff green, compileall green, `git diff --check` green |
| E3 live provider | 환경 차단: OpenAI/Anthropic/Gemini/Google/Atlas/OAuth key 모두 없음 |
| Redis integration tier | 환경 차단: Redis/Docker/Podman/redis-server 없음 |

전체 회귀의 유일한 warning은 사용자 소유 voice test의 Starlette `TestClient` deprecation
warning이었다. provider credential를 제거한 프로세스에서 전체 회귀를 실행했다.

## 4. 실제 PostgreSQL 50K component soak

Raw artifact:
[`evidence/postgres-50k-soak-2026-07-16.json`](evidence/postgres-50k-soak-2026-07-16.json)
(SHA-256 `d32e00f3c9498145493a2d592533f73e6d09e0546afdf3f4d70778de82b88243`).

실행 조건:

```powershell
.\server\.venv\Scripts\python.exe scripts\benchmarks\postgres_50k_soak.py `
  --database-url postgresql+asyncpg://postgres@127.0.0.1:55432/mnemos_test `
  --files 50000 --batch-size 50 `
  --output-json docs\04-eval\evidence\postgres-50k-soak-2026-07-16.json
```

- Windows 11 Pro 10.0.26200, Intel i5-1340P, 16 logical processors, 약 31.7 GiB RAM;
- Python 3.12.12, PostgreSQL 17.10;
- base HEAD `91456141f63760e7b56352c43af7af2f4e501db7`, working-tree implementation;
- 100 directories, 50,000 Python files, 파일당 함수 하나, 약 100K LOC/1.89 MB source;
- production worker flush와 같은 batch 50, analyzer queue max 64.

실측 결과:

| 지표 | 결과 |
|---|---:|
| corpus 생성 | 86.005 s |
| 최초 analyzer + stage | 152.836 s, 327.147 records/s |
| 최초 stage DB 누적 | 31.494 s, 1,000 batches |
| 최초 atomic promotion | 65.945 s, 50,000 nodes inserted |
| same-content analyzer + stage | 125.987 s, 396.865 records/s |
| same-content promotion | 11.355 s, 50,000 nodes unchanged |
| same-content 전체 refresh | 137.387 s, semantic no-op true |
| 최대 in-memory candidates | 50 |
| controller + direct analyzer sampled peak RSS | 111,190,016 bytes(약 106.0 MiB) |
| LLM | 0 calls, 모든 token field 0 |
| 전체 script wall | 541.037 s |
| cleanup | private schema drop + temporary source removal 성공 |

`total_wall_seconds`는 corpus 생성, schema setup, 두 번의 분석/게시, cleanup 전체다. 이를
“50K 단일 index 시간”으로 인용하지 않는다. RSS는 controller와 direct analyzer child의
표본이며 PostgreSQL server RSS를 포함하지 않는다.

이 soak가 증명하는 범위는 `AnalyzerRunner → bounded stage → seal → O(graph) promotion`과
동일-content bitemporal no-op이다. calls/contracts/data-access/edge sweep, deletion write,
mixed language, real Git checkout, Redis, HTTP/MCP query, optional LLM은 포함하지 않는다.
따라서 CBM의 Linux kernel 28M LOC/75K files/수백만 nodes·edges 결과와 직접 비교할 수 없다.

## 5. 속도와 토큰 문제에 대한 정확한 답

### 개선된 것

- 기본 index의 LLM 비용은 구조적으로 0이며 50K 실제 PostgreSQL 경계에서도 0이었다.
- retry/restart가 terminal candidate를 재사용하므로 완료된 동일 작업의 추가 provider
  call은 0이다.
- provider SDK retry를 0으로 고정하고 logical fallback을 durable physical attempts로
  분리했다.
- batch staging과 bounded queue로 50K 레코드를 메모리에 모으지 않았다.
- same-content refresh는 graph row를 새로 만들지 않고 50K 전부 `unchanged`였다.
- 비용 상한은 동시성 아래에서도 원자적이며 unknown route를 0원으로 간주하지 않는다.

### 아직 측정하지 않은 것

- unseen-repository 질문 A/B의 input/output token 절감률;
- answer correctness, evidence precision/recall, tool calls, query p50/p95;
- 실제 provider usage dialect와 latency의 live canary;
- CBM과 같은 corpus/하드웨어/질문 세트의 head-to-head 결과.

전체 input ceiling 예약 때문에 small prompt도 Anthropic 1M/OpenAI 128K/Gemini 1,048,576
input 최악가격으로 승인된다. 이 방식은 under-reservation을 막지만 cap utilization을 낮춰
후속 호출을 일찍 차단할 수 있다. 더 정밀한 예약은 provider가 실제 hard input limit 또는
검증 가능한 request-specific ceiling을 제공할 때만 안전하게 도입한다.

## 6. CBM과의 현재 판정

Mnemos가 전체적으로 더 좋다는 판정은 아니다. CBM은 현재 README 기준 158개 언어,
15개 MCP tool, 43개 client surface, bundled local semantic embedding, single static binary,
Linux kernel 처리량과 sub-ms structural query 수치를 공개한다. 논문은 31개 real repository에서
explorer 대비 10배 적은 tokens와 2.1배 적은 tool calls를 보고했지만 quality는 83% 대
92%였다. README의 120배 예시는 논문 결과와 구분해야 한다.

Mnemos의 현재 우위는 속도/언어 폭이 아니라 bitemporal history, atomic publication,
verified/asserted/inferred 분리, runtime reconciliation, encrypted crash-safe candidate replay,
mandatory worst-case cost authorization이다. 상세 판정은
[`codebase-memory-comparison-2026-07-16.md`](codebase-memory-comparison-2026-07-16.md)에 있다.

## 7. 고정 완료 기준과 분리된 Phase-2 backlog

다음은 실제 product gap이지만 이번 안전·회계 root gate를 다시 열 이유와 혼동하지 않는다.

- L2/L3 summary는 target limit을 적용하기 전에 current lower-level summary/node 전체를
  materialize한다. token/call은 bounded지만 pre-call DB/RAM work는 O(project)다.
- lexical search는 unanchored `ILIKE` 후보를 ORDER BY 없이 2,000개에서 자른 뒤 점수화해
  대형 graph에서 globally best result를 놓칠 수 있다.
- authoritative omission sweep는 paged/bounded memory지만 여전히 O(graph) DB work다.
- analyzer success에는 signed terminal coverage record/scanned-file count가 없다.
- hard process-kill fault injection, edge-rich mixed-language 50K+, retained Git content archive,
  contribution history, automatic history pruning은 별도 production-qualification 과제다.

## 8. 현재 운영 차단 증거

- live-provider E3: 모든 지원 provider key/OAuth token 부재로 실행 불가;
- Redis integration: `127.0.0.1:6379`와 Docker/Podman/redis-server 부재;
- GitHub publish: 이 환경에 `gh`가 없어 repository 변경의 commit/push/PR 단계는
  `github:yeet` 안전 절차상 진행할 수 없음.

첫 두 항목은 구현 실패가 아니라 환경 검증 공백이며 문서에서 pass로 바꾸지 않는다. 마지막
항목은 로컬 코드/검증 결과와 별개인 게시 도구 차단이다.
