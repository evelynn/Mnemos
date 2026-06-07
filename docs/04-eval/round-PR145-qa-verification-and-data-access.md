# Round PR-145 — Q&A verification + function→table data-access edges

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `788a5ec`
트리거: "분석 완료 후 다양한 질문·상세 프로세스 요청에 제대로 답하는지 검토"

## Q&A 검증 (분석된 shop3tier: python 핸들러 + sql 스키마, docker-free)

Mnemos 의 Q&A 는 Claude Code 가 MCP 쿼리 도구를 조합해 답하는 구조. 분석된
그래프에 대해 실제 도구를 호출해 "제대로 답하는지" 실측:

| 질문 | 도구 | 결과 |
|---|---|---|
| "주문 생성 로직 어디?" | `search_symbols("create order")` | ✅ `handle_create_order` + `data:orders/order_items` |
| "그게 뭘 하나?" | `get_symbol` | ✅ 시그니처 + **L1 요약**("요청 파싱→고액 kiosk 위험게이팅→주문·품목 DB 저장→order_id/status/reason_code 반환") |
| "이 함수가 건드리는 DB는?" | `get_data_access` | **처음엔 빈 값(갭)** → PR-145 후 ✅ `writes=[data:orders, data:order_items]` |
| "PII 테이블은?" | DataEntity 노드 | ✅ 테이블 + 컬럼 목록 |
| "주문 처리 프로세스 전체?" | level-4 flow summary(trace_flow) | ✅ (trace_flow=200, 8단계 영속) |

## 검증으로 드러난 갭과 보완

검증 중 `get_data_access` 가 빈 값을 반환했다 — `handle_create_order` 가 명백히
`orders`/`order_items` 에 INSERT 하는데도. 원인: 에이전트 코드 추출이 CALLS/
CONTAINS 만 만들고 **함수→테이블 READS/WRITES 엣지를 안 만들었음**. "이 함수가 어떤
DB 를 건드리나" 는 상세 프로세스 Q&A 의 핵심인데 답을 못 했다.

| ID | 변경 | 파일 |
|---|---|---|
| 145-1 | 코드 추출 프롬프트에 `data_access`(symbol→table READS/WRITES) 추가 — SQL 문자열/ORM 호출에서 추론 | `app/extractor/agent_extract.py` |
| 145-2 | `to_envelopes` 코드 분기: data_access → `data:<table>` 대상 READS/WRITES 엣지(데이터 엔티티 타깃이라 symbol dangling 필터 우회) | `app/extractor/agent_extract.py` |
| 145-3 | 회귀 테스트 3건 (envelope 변환·producer/consumer 동기화·DB분기 무영향) | `tests/test_pr145_data_access_edges.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-145 단위 | **3 passed** |
| pytest `not integration` (−pr114) | **1460 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live 재분석 | edges 1→**3** (데이터접근 엣지 적재), get_data_access **writes=[orders, order_items]** 실측 |

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B MCP 쿼리(Q&A) | 9.0 | **9.2** | search/get_symbol/data_access/data-entity Q&A 실측 + data-access 갭 해소 |
| F 그래프 품질 | 8.8 | **9.0** | 함수↔테이블 횡단 엣지로 그래프 연결성 향상 |

## 정직한 한계
- find_callers/callees 는 단일 파일 추출이라 intra-file 호출만 — 다파일/전역 호출
  그래프는 limit 상향 + 파일 간 id 일치에 의존.
- data:table id 정합(스키마 유무: `data:orders` vs `data:public.orders`)은 동일
  추출기 내에선 일관되나 분석기↔에이전트 혼합 시 머지 레이어 reconcile 필요(기존 과제).
- trace_flow 프로세스 답변은 해당 프로젝트에 1회 실행 필요(자동 트리거는 후속).
