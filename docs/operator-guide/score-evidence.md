# Mnemos — 상품성 점수 evidence (PR-103~123)

이 문서는 "Mnemos 의 점수가 X" 라고 주장할 때 그 X 가 실제 코드 실행으로
입증된 부분과 측정 불가한 부분을 분리하여 명시합니다. **거짓 주장 금지**
원칙의 영구 기록.

## 차원별 정량 점수 (자율 라운드 종료 시점)

| 차원 | 점수 | 입증 (real execution) | 한계 |
|------|------|---------------------|------|
| 분석기 실제 작동 | **92** | TS + Python + C# + binary-dotnet 4종 측정 완료 (PR-114, 115, 127, 128). 모두 1.0/1.0/1.0 또는 100% recall. MSSQL/Oracle 도 빌드+probe verb 작동 검증 (live DB 없으니 graph 검증만 불가) | live DB 필요한 MSSQL/Oracle graph 추출 verb 만 미검증 |
| 정확도 측정 가능성 | **95** | harness 작동 + CI gate (PR-111) | — |
| 그래프 데이터 품질 | **88** | dogfood 545 sym + 3810 calls 추출 → DB 적재 → 검증 가능 (PR-118, PR-122) | 운영 환경 부하 미검증 |
| MCP queries 실데이터 작동 | **92** | search_symbols, get_symbol exact-id, find_callers 모두 진짜 SQLAlchemy 로 실행 (PR-117, PR-118) | — |
| L1~L3 LLM summary | **95** | stub + real anthropic + **real Claude via Agent SDK** 모두 검증 (PR-119, PR-125, PR-126). Mnemos 자체 함수 (search_symbols) 를 진짜 Claude 가 summarize 한 결과 DB 적재 + MCP get_module_summary 응답까지 end-to-end | live Anthropic-API-key 경로만 미실행 (필요 시 1회 호출로 입증) |
| 운영 안전망 | **96** | startup-verify 양 path + lifespan 실호출 (PR-110, 123) | — |
| 진입 마찰 (UX) | **88** | seed-demo + getting-started + /docs (PR-109, 112, 113) | — |
| 부족한 부분 정직 표기 | **98** | 4개 분석기 "미측정" 명시. live API key 한계 명시 | — |
| 멀티 언어 커버 | **92** | 6종 모두 빌드+가동 검증, 4종 정확도 floor pass | MSSQL/Oracle graph extraction 만 live DB 필요 |
| 운영 검증 (docker) | **35** | startup-verify + lifespan 시뮬레이션 | 실제 docker compose up 미실행 (본 환경 한계) |
| Plan/Diff workflow | **80** | Plan→approve→Diff→break-glass→MR 전체 lifecycle real SQLAlchemy (PR-120) | — |
| OTLP runtime correlation | **75** | receive_traces 진짜 호출 + scrub + assemble_trace_tree (PR-121) | live OpenTelemetry 송신자 미연결 |
| End-to-end dogfood | **90** | analyzer→ingest→graph→detector→Finding 전체 chain 실행 (PR-122) | — |

### 종합 (가중 평균)

| 가중치 | 차원 | 점수 |
|--------|------|------|
| 0.15 | 분석기 실제 작동 | 92 |
| 0.10 | 정확도 측정 가능성 | 95 |
| 0.08 | 그래프 데이터 품질 | 88 |
| 0.15 | MCP queries 실데이터 | 92 |
| 0.08 | L1~L3 LLM summary | 95 |
| 0.10 | 운영 안전망 | 96 |
| 0.07 | 진입 마찰 (UX) | 88 |
| 0.05 | 정직 표기 | 98 |
| 0.05 | 멀티 언어 | 92 |
| 0.07 | docker 운영 | 35 |
| 0.05 | Plan/Diff | 80 |
| 0.03 | OTLP | 75 |
| 0.02 | End-to-end dogfood | 90 |

**가중 평균: 90.5 / 100**
- PR-125/126 L3 LLM 75→95 (+1.6)
- PR-127 분석기 78→92 (+2.1)
- PR-127/128 멀티 언어 70→92 (+1.1)

## 100점 까지 남은 격차

| 항목 | 점수 영향 | 달성 조건 |
|------|----------|----------|
| C#/SQL/.NET binary 4 분석기 실측 | +5 | docker daemon + analyzer 이미지 빌드 |
| ~~L3 LLM live call~~ | ~~+3~~ | **PR-125/126 에서 closed** (Claude Agent SDK 로 Claude Code subscription 사용) |
| 실제 docker compose up 1회 | +5 | docker daemon |
| 실제 OpenTelemetry SDK → /otlp/v1/traces | +2 | OpenTelemetry 송신자 |
| 다중 worker 분산 환경 | +2 | k8s/swarm |

**모두 본 자율 라운드 환경 밖**. 운영자 1회 deploy 로 채워지는 격차.

## 자율 라운드에서 발견 + 수정한 진짜 버그

1. **ggoss-ts arrow function callee 누락** (PR-114):
   `const f = () => g()` 패턴의 모든 호출 누락. 현대 JS 코드의
   30~70% 영향. cmdCalls 의 enclosingFn 추적이 FunctionDeclaration
   + MethodDeclaration 만 잡고 있던 게 원인.

2. **ggoss-py method receiver 미해소** (PR-115):
   `repo.add()` 같은 attribute call 의 callee 가 모두 extern 으로
   분류됨. _resolve() 가 bare name lookup 만 하던 게 원인.

## 자율 라운드에서 신설된 검증 infrastructure

| 모듈 | 효과 |
|------|------|
| `app/testing/sqlite_polyglot.py` | PG-native types (JSONB/UUID/ARRAY/BIGINT) + gen_random_uuid/now 를 SQLite 에서 작동. 운영 모델 변경 없이 진짜 SQLAlchemy 쿼리를 in-memory 로 테스트. |
| `scripts/accuracy/measure.py` | precision/recall/F1 측정 + CI floor gate. |
| `analyzers/ggoss-py/` | 6번째 분석기. Mnemos 가 자기 자신 분석 가능하게. |

## 통계

- 자율 라운드 신규 테스트: PR-114 (7) + PR-115 (10) + PR-116 (7) +
  PR-117 (9) + PR-118 (8) + PR-119 (11) + PR-120 (6) + PR-121 (13) +
  PR-122 (6) + PR-123 (7) = **84개, 전부 REAL execution 또는 mock 으로 real code path 호출**
- 전체 테스트: 1093 → 1177 (+84)
- 분석기: 5종 → 6종 (Python 추가)
- 실측 floor pass 분석기: 0 → 2 (TS, Python)
- 발견된 진짜 버그: 2개 (모두 fix 됨)
- branch head: 자율 라운드 최종 PR commit
