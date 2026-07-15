# 소스 분석 목적성·효용 검증 및 근본 개선 기록 — 2026-07-14

> **Historical checkpoint.** 이 평가는 2026-07-14의 remediation 기준선을
> 보존한다. 당시 핵심 결함이었던 atomic staging/promotion 미연결은
> 현재 Phase B에서 `run_ingest`에 연결됐다. 아래의 “staging 없음”,
> “mixed current graph”, “full repair 전 불신” 판정은 당시 상태이며,
> 현재 계약·한계·E2 증거는
> [Phase-B 보고서](atomic-graph-publication-phase-b-2026-07-15.md)가
> authoritative하다.

> 평가 대상: 현재 Mnemos 작업 트리의 source index, incremental refresh,
> MCP/agent context, optional LLM 경로
>
> 판정 기준: “기능이 많다”가 아니라 **AI가 큰 소스를 더 적은 문맥으로,
> 출처와 불확실성을 보존한 채 분석하도록 실제로 돕는가**

## 1. 결론

Mnemos의 유효한 제품 역할은 다음 한 문장이다.

> **Mnemos는 AI 대신 결론을 만드는 도구가 아니라, 지원 언어의 소스를
> 결정적으로 인덱싱하고 근거·관계·커버리지·정확한 소스 범위를 작게 재조회하게
> 하는 source-reference / analysis guide다.**

이 역할에는 실제 효용 증거가 있다. 외부 실제 저장소를 분석한 과거 검증에서
11,391 symbols, 57,491 CALLS를 만들고 실제 MCP stdio 시나리오 25/25를 통과했다.
AI가 project index → search → task pack → callers/callees/impact → 좁은 source read
순서로 탐색하는 구조도 동작했다.

그러나 현재 상태를 “대규모 증분 분석이 완성됐다”거나 “production-ready”라고
부를 수는 없다. 가장 큰 이유는 **run 단위 atomic staging/promotion이 없기 때문**이다.
실패한 analyzer의 삭제 권한은 차단했지만, 실행 중 추가된 graph row는 이미 current
graph에 기록된다. authoritative refresh의 identity memory는 임계치 뒤 private temporary
disk index로 spill되고 sweep도 page 단위가 됐지만, DB work 자체는 여전히 O(graph)다.
50 K-file/Postgres soak도 하지 않았다.

따라서 현재 판정은 다음과 같다.

- **지원 언어 + 성공한 완료 run + coverage 확인** 조건에서는 source guide로 유용하다.
- 기본 최초 인덱스의 LLM 소비는 구조적으로 **0 tokens**다.
- optional narration/agent extraction 품질은 실 provider 검증 전까지 보조 정보다.
- 실패/부분 refresh 뒤에는 diagnostic project index와 명시적 historical source read를
  제외한 current-graph MCP 조회가 fail-closed된다. 원인은 여전히 atomic publication
  부재이므로 성공한 full repair 전에는 graph snapshot을 신뢰하면 안 된다.

## 2. 제품 계약

### 2.1 기본 산출물

기본 산출물은 장문의 AI 요약이 아니라 다음의 결정적·재조회 가능 정보다.

- source-located Symbol/Component/Contract/DataEntity와 관계;
- producer가 부여한 `verified` / `asserted` / `inferred` certainty;
- 어떤 언어/producer가 완전하게 실행됐는지 나타내는 coverage;
- AI가 작업 하나에 필요한 이웃만 받는 project index와 task context pack;
- 완료 run의 Git revision에 묶인 bounded source range read.

권장 AI 사용 순서는 다음과 같다.

1. `get_project_index`에서 last completed run, active/failed refresh, coverage gap을 본다.
2. `search_symbols`로 후보를 좁힌다.
3. `get_task_context_pack`과 callers/callees/contracts/data-access/impact를 재조회한다.
4. certainty와 truncation을 보존해 가설을 세운다.
5. 수정 또는 중요한 결론 전에 완료 Git snapshot의 작은 line range를 읽는다.

### 2.2 기본 비용 계약

정상 full/incremental 요청의 기본값은 `summarize=false`,
`agent_extract_limit=0`이다. 이 경로는 LLM client를 만들 필요가 없고 **초기 LLM
token 비용이 0**이다. L1→L2→L3 narration과 미지원 언어 agent extraction은 서로
독립적인 명시적 opt-in이다.

이것은 “전체 작업의 AI token이 항상 0”이라는 뜻이 아니다. AI가 MCP 결과를 읽고
추론하거나 source range를 검증하는 비용은 남는다. Mnemos가 보장하는 것은 비싼
whole-repository opinion을 최초 분석의 전제조건으로 만들지 않는다는 점이다.

### 2.3 비목표

- generic chatbot/SaaS/admin 제품이 되는 것;
- optional summary를 source truth로 승격하는 것;
- 미지원 언어 또는 coverage gap을 추측으로 메우고 `verified`라고 표시하는 것;
- repository 전체를 한 번에 prompt에 넣는 것.

## 3. 조사에서 확인한 원래 문제

### 3.1 최초 실행의 과도한 token 경로

이전 기본 흐름은 source index와 L1/L2/L3 narration, 미지원 언어 agent extraction의
경계를 흐렸다. 기본 제한 조합상 수십 회(대략 80–105회)의 summary 호출을 만들 수
있었고, API 허용 상한을 조합하면 최대 4,900개 target까지 열릴 수 있었다. provider가
없는 환경에서는 그 비용 구조를 거친 결과의 상당수가 stub summary여서 source
reference 가치도 낮았다.

근본 조치는 “요약 최적화”가 아니라 **기본 제품을 deterministic index로 재정의**한
것이다. narration은 명시적 2차 작업이며, index/MCP에 필수가 아니다.

### 3.2 변경/갱신 시 멈춤과 과부하

다음 결함이 겹쳐 있었다.

- 이름만 incremental이고 매번 전체 언어 tree를 다시 걸음;
- `.uv-cache`, `.uv-python` 같은 숨김 도구 cache를 Python source로 오인해
  최초 fingerprint와 analyzer walk를 수천 파일까지 증폭;
- 같은 내용도 temporal fact를 다시 닫고 써서 diff와 DB write가 커짐;
- analyzer stdout queue와 line/record 크기, wall time가 충분히 bounded되지 않음;
- stderr/malformed/truncated output 뒤에도 누락을 “삭제”로 해석할 위험;
- webhook queue를 소비하지 않거나 cwd를 source로 오인할 수 있음;
- Compose의 standalone analyzer image와 실제 worker 실행 경로가 분리돼
  `completed` empty graph가 가능함;
- 임시 worktree 절대경로가 symbol/component identity에 섞임;
- branch/SHA/source snapshot이 서로 다른 revision을 가리킬 수 있음.

## 4. 이번 근본 개선

### 4.1 결정적 기본값과 비용 경계

- API/UI/orchestrator 기본값을 zero-token index로 고정;
- narration과 agent extraction을 별도 명시 옵션으로 분리;
- evidence hash로 unchanged optional summary를 건너뜀;
- L2/L3 evidence를 approximate-token pack으로 잘라 model input을 제한;
- LLM call ledger와 evidence hash를 물리 필드로 분리.

### 4.2 analyzer 격리

- queue 256 records, JSONL record 1 MiB, stream/line bound;
- analyzer wall timeout, terminate → kill, cancellation;
- 모든 manifest/analyzer walker에서 숨김 directory와 file symlink/junction을
  source contract 밖으로 통일;
- non-zero exit, malformed output, stderr, truncated output을 incomplete로 기록;
- 부분 producer에는 deletion authority를 주지 않음;
- worker job retry를 1회로 제한해 partial write 중복을 막음.

### 4.3 source revision과 identity

- manual Git 분석도 요청 revision의 detached worktree에서 실행;
- dirty manual Git source는 거부;
- webhook은 `<project UUID>[.git]` mirror에 exact pushed SHA가 있어야 enqueue;
- symbol/location/component identity를 project-relative path와 stable project ID로 생성;
- MCP `read_file`은 latest completed run의 Git snapshot에서만 읽고 byte/line cap 적용;
- absolute/UNC/`..` traversal, binary/invalid UTF-8, giant line을 거부.

### 4.4 incremental 의미 보강

- analyzer-family별 relevant input/config/runtime availability fingerprint;
- API/UI 기본 refresh는 incremental이며 baseline이 없는 최초 run만 전체 producer를
  선택한다. full은 명시적 repair/reconciliation 동작이다;
- same-content run은 analyzer spawn과 semantic graph rewrite를 건너뜀;
- changed family만 재실행하되, 그 family 내부 tree는 아직 전부 재순회;
- producer coverage가 complete + authoritative + required verbs complete일 때만
  omitted facts를 close;
- shared ownership fact는 모든 owning producer가 authoritative일 때만 sweep;
- 현재 contribution table이 없는 동안에는 이전/current deterministic producer
  전체가 **이번 run에서 실제로** authoritative refresh된 경우에만 global sweep;
- mutable non-Git tree는 sweep 직전 manifest를 다시 확인.

### 4.5 운영/API 보강

- project mutation lock과 terminal-state compare-and-set;
- enqueue 실패/취소를 ghost `queued`가 아닌 terminal run으로 기록;
- project lock 경합은 run을 실패/유실시키지 않고 동일 revision/options로 지연 재enqueue;
  lock 소유 전에는 `started_at`을 쓰지 않으며 completed/failed owner의 잔류 lock은
  compare-owner 방식으로 회수;
- fixed queue, bounded local IDs/TTL, default-branch webhook filter;
- SSE가 장시간 DB session/connection을 점유하지 않도록 짧은 재조회 사용;
- unpublished refresh가 있으면 current-graph MCP와 추가 incremental/continuation을
  구조화 오류로 차단하고 성공한 full repair만 허용;
- standard Compose worker 이미지에 runnable in-repo source analyzers를 포함.

### 4.6 structured LLM 경계

- provider envelope/alias를 입구에서 canonical schema로 normalize;
- unknown/oversized/invalid shape를 reject;
- provider prompt는 완전한 JSON evidence만 받고 blind character slicing을 하지 않으며,
  L1 scope가 일부이면 truncation marker를 함께 전달;
- L2/L3 map-reduce claim은 file/module pseudo-ID가 아니라 실제 current graph ID만 인용;
- claim evidence를 현재 project의 제공 evidence set과 대조;
- model이 `verified`를 주장해도 LLM-derived structure는 `inferred`로 정규화;
- prompt가 아니라 validator/persistence가 project scope, certainty, evidence를 소유;
- user-facing diagnostics에 raw provider secret/response를 노출하지 않음.

## 5. 실제 효용 증거

| 증거 | 관찰 | 정직한 해석 |
|---|---|---|
| 작은 in-repo Python 실제 pipeline | full index와 same-content refresh 경로 실행 | deterministic L0와 no-op incremental의 실경로 canary. 대규모 증거는 아님 |
| 숨김 cache 수정 전 Mnemos manifest | 6,621 producer-file / 71,742,390 bytes / 53.079 s | `.uv-cache` 4,513 files·50.4 MB와 `.uv-python` 689 files·11.4 MB를 source로 오인한 실제 초기 부하 결함 |
| 같은 조건 최종 manifest 3회 | 473 producer-file / 3,399,297 bytes / 1.037 s cold, 0.530·0.551 s warm | 수정 중 추가된 소스/테스트를 포함해도 수정 전 대비 file assignment 92.9%, bytes 95.3%, wall time 약 98% 감소. producer-file은 한 파일이 여러 analyzer에 속하면 중복 집계됨 |
| 최종 ggoss-py 실제 subprocess | probe 0.681 s, inventory 0.996 s, symbols 3,454/3.339 s, calls 17,622/3.275 s, contracts 210/2.619 s, data-access 427/2.671 s | 현재 Mnemos 전체 tree의 실경로 canary이며 6개 verb 모두 exit 0, stderr 0자. 정확도 전체 증거는 아님 |
| 외부 `codebase-memory-mcp` 재검증 | 11,391 symbols, 57,491 CALLS, MCP 25/25 | 실제 저장소에서 graph/MCP guide가 유용함을 보인 강한 방향성 증거 |
| ground-truth spot check | `gb_intern` caller 함수 집합이 원본 grep과 일치 | 해당 표본의 정밀도/재현율 증거이지 전체 analyzer 정확도는 아님 |
| 현재 build + 과거 외부 graph artifact | 기본 top-10 project index 23,457 B/~5,865 estimated tokens/0.374 s, task pack 14,954 B/~3,739 tokens/0.081 s | 11.7 K node/59.9 K edge 로컬 graph 사본에서 측정. 기본 index top-k도 25→10으로 낮췄다. 둘 다 raw source 없이 새 50 KiB serialized hard cap 안이며 emergency transport truncation 미발동 |

외부 저장소 결과의 자세한 SHA/run/검증 항목은
[agent-context-real-repo-retest-2026-07-07.md](agent-context-real-repo-retest-2026-07-07.md)에
있다. 이 결과는 이번 remediation 이전 build의 실제 효용 증거다. 현재 변경 전체를
같은 저장소/새 SHA에서 재실행하지 않았으므로 현재 build의 E4 증거로 재사용하지 않는다.

아직 하지 않은 가장 중요한 효용 시험은 **unseen repository에서 direct AI와
Mnemos-guided AI를 비교**하는 것이다. 동일 질문 세트에 대해 정답률, 잘못된 참조,
source citation, 총 input/output tokens, latency를 비교해야 “최종적으로 token을 얼마나
절감하는가”를 수치로 말할 수 있다.

## 6. E0–E4 품질 게이트

E0–E4는 일반적인 제품 점수가 아니라 **model output이 parser/validator/persistence를
통과하는 optional structured LLM 경로**의 증거 수준으로만 적용한다. deterministic
zero-token index의 실제-repo 효용과 섞어 점수를 부풀리지 않는다.

### 6.1 계약 맵

| 층 | 현재 계약 |
|---|---|
| outer envelope | Anthropic/OpenAI/Gemini/agent adapter의 provider response |
| producer dialect | documented alias/envelope만 boundary adapter에서 수용; 충돌은 reject |
| canonical schema | bounded summary/claim/edge objects + explicit evidence/certainty |
| consumers | validator → Summary/LLMCall persistence → MCP/artifact readers |

### 6.2 검증 수준

| 수준 | 상태 | 근거 / 제한 |
|---|---|---|
| E0 static contract | **통과** | repository-wide Ruff, Python `compileall`, Node syntax, `git diff --check` 통과 |
| E1 unit behavior | **통과** | canonical/alias/invalid shape, project/evidence scope, certainty laundering, pack overflow regression |
| E2 mock integration | **통과** | mock provider/adapter → parse/normalize/validate/persist 및 extractor/MCP focused suites |
| E3 real-provider canary | **미실행** | 이 환경에서 provider credential/cost를 사용하지 않음. “live model verified” 주장 금지 |
| E4 representative real workflow | **미실행** | current build + real provider + representative repo/Postgres + rendered/consumed result 검증 없음 |

따라서 optional structured LLM 경로의 최고 정당화 수준은 **E2**다. 외부 실제 저장소
MCP 25/25는 deterministic core의 효용 증거이지, E3 live-provider 통과가 아니다.

현재 작업 트리의 전체 non-integration 회귀는 `1,888 passed, 19 skipped,
19 deselected`로 통과했다. 이 호스트에는 .NET SDK와 Docker가 없어 C# build 및
Compose/PostgreSQL 통합 경로는 실행하지 못했으며, deselected integration을 통과한
것으로 해석하지 않는다.

Manifest 수치는 2026-07-14 현재 Mnemos root와 등록된 source language 전체를
동일한 `build_source_manifest` 경로로 읽은 로컬 Windows 실측이다. 수정 전/후의
working tree가 완전히 동일한 benchmark fixture는 아니므로 microbenchmark처럼
소수점 성능을 일반화하지 않는다. 다만 수정 전 파일의 78% 이상이 두 숨김 uv
directory에서 왔고 수정 후 동일 directory가 0건인 점은 원인과 효과를 직접 입증한다.

## 7. 남은 근본 한계

### P0 — 신뢰성과 대규모 갱신

1. **Atomic staging/promotion 없음.** Node/Edge가 run ID가 붙은 immutable snapshot으로
   승격되지 않는다. 실패/partial run 추가분이 current graph에 섞일 수 있다. 현재
   current-graph MCP와 incremental/continuation은 newer unpublished refresh를 감지해
   fail-closed하지만 이는 오염을 원자적으로 방지하는 대체물이 아니다.
2. **O(graph) refresh work.** `seen` identity는 bounded-memory + temporary-disk spill이고
   current-row 조회도 page 단위라 peak Python memory 경로는 차단했다. 그러나 deletion
   sweep의 DB work와 temporary-disk 크기는 graph 크기에 비례한다.
3. **Changed family full walk.** family fingerprint는 unchanged family를 건너뛰지만,
   한 파일 변경도 해당 analyzer family의 관련 tree 전체를 다시 걷는다.
4. **Rapid push coalescing 없음.** 실행 시 older webhook supersession은 있지만 enqueue
   전에 여러 push를 하나로 합치지 않아 queue/manifest work가 남는다.
5. **Producer contribution table 없음.** 안전을 위해 multi-producer incremental
   deletion을 보류하므로 stale fact가 다음 authoritative full reconciliation까지 남을
   수 있다. 안전한 보수성이지 완전한 증분 삭제가 아니다.
6. **Hard-crash lock recovery 지연.** 경합 run은 유실되지 않고 30초 단위로 재시도하지만,
   lock owner process가 SIGKILL되면 fencing token/DB advisory lock이 없어 안전하게 lease를
   조기 탈취할 수 없다. 최악에는 9시간 Redis TTL까지 queued 상태로 지연된다.

### P1 — 증거 범위

7. **50 K-file / real Postgres / crash-soak 미검증.** bounded runner unit/stress와 한
   외부 저장소는 이 조합을 대신하지 않는다.
8. **C# standard Compose 미포함.** C# analyzer source/profile image가 존재해도 worker
   실행 경로가 아니므로 C# production workflow라고 주장할 수 없다. live DB와
   .NET-binary도 별도 통합이 필요하다.
9. **E3/E4 미검증.** live provider narration/agent extraction과 대표 workflow의
   품질·비용을 측정하지 않았다.
10. **Agent extraction deletion semantics 제한.** inferred producer를 authoritative
   deletion source로 쓰기 위한 안정적인 identity/coverage 계약이 부족하다.
11. **LLM 비용 ledger 범위 제한.** 새 extraction path의 call은 기록하지만 모든 AI
    surface를 하나의 독립 durable ledger로 완전히 포괄하지 않는다.
12. **Submodule/LFS content.** Git snapshot reader는 일반 blob에는 정확하지만
    submodule/LFS는 checkout/pointer semantics 이상의 content를 보장하지 않는다.
13. **Analyzer terminal coverage proof 없음.** bounded process와 required verb가 exit 0이면
    usable로 판단한다. analyzer가 bug로 유효 record 0건을 내는 경우와 실제로 fact가
    없는 source를 항상 구분할 scanned-file/terminal coverage record가 아직 없다.
14. **Git object 장기 보존 없음.** completed run은 full SHA와 repository provenance를
    보존하지만 `refs/mnemos/runs/<run>` 같은 retention ref/content archive를 만들지 않는다.
    repository GC 뒤 historical source read가 사라질 수 있고 non-Git immutable archive는
    아직 계약만 있다.
15. **L4 flow grounding 제한.** bounded completed-snapshot source와 strict schema는
    적용됐지만 step/flag prose가 graph node/edge 또는 line-range evidence에 연결되지는
    않는다. verified graph fact가 아니라 source-pinned optional narration이다.

## 8. 다음 수용 기준

“근본 문제가 해결됐다”고 말하려면 최소한 다음 순서가 필요하다.

1. `analysis_run_id`로 격리된 staging Node/Edge/Source를 쓰고, producer coverage가
   authoritative일 때 한 transaction으로 current snapshot promote.
2. Node/Edge producer contribution을 별도 보존하고 producer별 contribution을 닫은 뒤
   contribution 0건인 logical fact만 close.
3. server-side temporary table/anti-join 또는 generation marker로 deletion diff를
   계산해 paged O(graph) application sweep와 run-local disk index까지 제거.
4. per-file manifest + dependency closure로 changed family full walk 축소.
5. 50 K-file/3-language real Postgres soak에서 peak RSS, queue depth, stage time,
   cancellation, worker kill/restart, failed-run visibility를 측정.
6. unseen repositories의 동일 질문으로 direct AI vs Mnemos-guided AI A/B 측정.
7. 작은 live-provider canary(E3), 이어 대표 repository + Postgres + 실제 consumer
   workflow(E4)를 별도로 통과.
8. target deployment에 C#이 필요하면 standard worker 안에서 analyzer contract와
   labelled fixture를 통과시킨 뒤 지원 목록에 추가.

## 9. 현재 안전한 사용법

- 첫 run은 `summarize=false`, `agent_extract_limit=0`, exact Git SHA로 실행한다.
- `completed`만 보지 말고 `producer_coverage`, authoritative flag, coverage gap을 본다.
- incomplete/failed refresh 뒤 current-graph MCP는 구조화된 repair 오류로 차단된다.
  diagnostic project index에서 실패 run을 확인하고 원인 제거 후 성공한 full refresh로
  복구한다. 명시적 historical `run_id` source read만 안전하게 허용된다.
- AI는 MCP graph를 탐색 지도처럼 쓰고, 중요한 결론/수정 전에 latest completed Git
  snapshot의 좁은 source range를 확인한다.
- narration은 source truth가 아니라 evidence-linked 설명으로만 취급한다.
- 대형 저장소에서는 첫 run의 files/bytes/rows/stage time/RSS를 기록하고 용량 수치를
  사전에 약속하지 않는다.

관련 운영 설명은 [analysis-strategy.md](../analysis-strategy.md),
[getting-started.md](../operator-guide/getting-started.md),
[large_system_readiness.md](../operator-guide/large_system_readiness.md)를 따른다.
