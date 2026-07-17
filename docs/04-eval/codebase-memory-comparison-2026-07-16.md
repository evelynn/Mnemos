# Mnemos vs codebase-memory-mcp — 2026-07-16 재평가

## 결론

**현재 Mnemos가 전체적으로 더 좋다고 말할 수는 없다.**

`codebase-memory-mcp`(CBM)는 설치 단순성, 158개 언어 범위, 로컬 임베딩,
공개 대형 저장소 처리량, MCP 클라이언트 호환성, 공개 벤치마크와 사용자 검증에서
앞선다. Mnemos는 다른 문제를 더 깊게 푼다. 즉, 검증/추론 분리, 원자적 그래프 게시,
bitemporal 이력, 실행 추적 reconciliation, 근거가 있는 재질의, 그리고 선택적 LLM
호출의 crash-safe 원장/비용 경계가 필요한 팀 분석에서는 더 강한 계약을 제공한다.

따라서 현재의 정직한 선택 기준은 다음과 같다.

- 빠르게 설치해 많은 언어의 코드 관계를 로컬 MCP로 찾는 용도: **CBM 우세**
- 시간축·런타임·근거·승인·비용 통제가 필요한 지속적 팀 분석: **Mnemos 차별화**
- 일반적인 “어느 쪽이 더 좋은가”: **CBM 우세, Mnemos는 아직 검증 격차가 있음**

## 공개 근거

CBM의 현재 README는 158개 언어, 15개 MCP 도구, 번들 로컬
`nomic-embed-code`, 43개 클라이언트 표면, Linux kernel 28M LOC/75K files 약
3분, 관계 질의 1ms 미만을 주장한다. README의 “120x fewer tokens”는 제품 예시이며
독립 벤치마크와 구분해야 한다.

CBM 논문 v1은 31개 저장소에서 explorer 대비 83% 대 92% answer quality,
10배 적은 토큰, 2.1배 적은 tool call을 보고한다. 논문 시점의 언어 수는 66개이므로
현재 README의 158개와 같은 버전의 수치로 섞지 않는다.

근거:

- <https://github.com/DeusData/codebase-memory-mcp/blob/main/README.md>
- <https://github.com/DeusData/codebase-memory-mcp/commits/main/>
- <https://arxiv.org/abs/2603.27277>

## 기능별 판정

| 차원 | CBM | Mnemos | 현재 판정 |
|---|---|---|---|
| 언어 폭 | README 기준 158개 | 전용 deterministic analyzer + tree-sitter PoC의 더 좁은 집합 | CBM |
| 설치/운영 | 정적 C 중심, 로컬 우선 | API/worker/DB와 선택적 분석기 운영 필요 | CBM |
| 공개 대형 처리량 | 28M LOC/75K-file 수치 공개 | 50K synthetic file/50K-node PostgreSQL component artifact만 있음; 동일 조건 아님 | CBM |
| 관계 질의 지연 | README에 `<1ms` 주장 | 같은 형태의 공개 latency benchmark 없음 | CBM |
| 로컬 semantic search | 번들 로컬 embedding | cloud embedding은 회계 계약 전까지 fail-disabled, lexical/BM25 기본 | CBM |
| 기본 토큰 비용 | 구조 인덱스 중심 | deterministic 기본 분석은 0 LLM token | 동률 성격, A/B 미검증 |
| 답변 토큰 효율 | 논문 10x, README 예시 120x | hard ceiling은 있으나 unseen-repo A/B 없음 | CBM 증거 우세 |
| 근거/확실성 | 정적 관계와 검색 | verified/asserted/inferred 분리, DB-owned claim grounding | Mnemos |
| 시간축 | 코드베이스 메모리 | bitemporal graph + immutable run/publication provenance | Mnemos |
| 런타임 현실 | 주로 정적 구조 | OTLP runtime edge reconciliation | Mnemos |
| LLM crash/replay | 공개 계약에서 핵심 아님 | stable operation, encrypted candidate, at-most-once replay | Mnemos |
| 비용/토큰 통제 | 로컬 우선으로 비용 회피 | call/input/output/wall + 모든 paid dispatch의 필수 positive atomic worst-case dollar reservation | Mnemos 안전 계약, 효율은 미측정 |
| 공개 성숙도 | 대규모 사용자/별/활발한 공개 이력 | 훨씬 작은 검증 표면 | CBM |

## 이번 개선이 바꾼 것

이번 작업은 Mnemos의 기존 강점인 “통제 가능한 grounded analysis”를 실제 불변식으로
강화했다.

- 모든 실행 가능한 직접 LLM 생성 경로는 durable `STARTED`와 양수 project-dollar
  정책의 원자적 최악가격 예약 뒤에만 dispatch한다.
- stable operation 재시도는 비용/예산을 바꾸기 전에 terminal candidate를 재생한다.
- Summary, Flow, Second Opinion은 후보 receipt와 최종 제품을 검증하고, 시도 승인과
  제품 게시를 같은 트랜잭션으로 수렴시킨다.
- 모델·가격·공식 API 목적지는 하나의 immutable price catalog/digest에 묶인다. OpenAI
  custom/http/path/port/userinfo/query route와 Anthropic 환경 base override는 네트워크 전에
  차단된다.
- opaque Agent SDK는 provider-enforced output/immutable price-route 계약이 없으므로 token
  limit이나 SDK importability와 무관하게 production에서 네트워크 전에 fail-disabled다.
- provider-side output/cost/usage 계약이 없는 Atlas 생성과 cloud embedding은
  fail-disabled다.
- 실제 PostgreSQL 17.10에서 migration head와 LLM 원장/동시 예약/재실행 85개가 통과했고,
  전체 비통합 회귀는 2,749 passed/0 failed였다.
- batch 50의 50K synthetic-file component soak는 50K 노드 최초 게시와 동일-content
  refresh의 50K `unchanged`, bounded buffer 50, LLM 0회/0 token을 검증했다. Raw artifact는
  [`evidence/postgres-50k-soak-2026-07-16.json`](evidence/postgres-50k-soak-2026-07-16.json)이다.

이는 CBM보다 넓거나 빠르다는 증거가 아니다. 대신 Mnemos가 선택한 좁은 우위—시간축,
근거, 런타임, 팀 운영, AI 비용 실패 안전성—를 더 믿을 수 있게 만든다.

## “더 좋다”를 뒤집기 위한 남은 공개 게이트

다음이 없으면 broad superiority를 주장하지 않는다.

1. 같은 하드웨어와 같은 unseen repository corpus에서 index time/RSS/query p50-p95 비교
2. 같은 질문 세트에서 정답률·근거 정확도·tool calls·입출력 토큰 비교
3. Linux-kernel급 mixed-language/edge-rich workload의 재현 가능한 Mnemos artifact
4. 설치부터 첫 MCP 답변까지 time-to-value 비교
5. 지원 언어별 symbol/call/contract 정확도 표본 검증

이번 50K artifact는 파일당 Python 함수 하나(약 100K LOC), 50K nodes, 0 edges이며 Git,
Redis, HTTP/MCP, mixed analyzer verbs, optional LLM과 PostgreSQL 프로세스 RSS를 포함하지
않는다. CBM의 28M LOC/수백만 graph-row Linux kernel 결과와 직접 비교하지 않는다.

이 문서의 판정은 기능 목록이 아니라 공개 증거 강도를 포함한다. 새 수치가 생기면
날짜, commit, 하드웨어, corpus, 명령, raw artifact를 함께 갱신한다.
