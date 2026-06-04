# Round PR-147 — trace_flow auto-trigger (entry → graph-collected files)

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `9bcba80`
트리거: "trace_flow 자동 트리거 — 진입점만 주면 그래프에서 관련 파일 수집"

## 보완

기존 `trace_flow` 는 운영자가 FE/BE/DB 파일 경로를 일일이 줘야 했다. PR-147 은
**진입점(자연어/심볼명) + repo 루트**만으로 관련 파일을 **지식 그래프에서 수집**:

| ID | 변경 | 파일 |
|---|---|---|
| 147-1 | `_gather_files_from_graph` — 진입어와 name/id/signature 가 매칭되는 심볼(티어 무관: "place order" → FE `placeOrder` + BE `handle_create_order`) + 1-hop CALLS/READS/WRITES 이웃(데이터접근 엣지로 SQL-스키마 DataEntity 파일 도달)의 `data.file` 수집 | `app/api/flow.py` |
| 147-2 | `POST /trace_flow/auto` 엔드포인트 — 진입점+source_root → 그래프 파일 수집 → 분석+영속 (기존 `_analyze_and_persist` 재사용) | `app/api/flow.py` |
| 147-3 | 회귀 테스트 2건 (라우트/모델 + 시드 그래프에서 티어 횡단 파일 수집) | `tests/test_pr147_flow_autotrigger.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-147 단위 | **2 passed** (FE+BE 이름매칭 + DB 엣지수집 + 무관 제외 실측) |
| mypy | **69** (불변) |
| pytest `not integration` (−pr114) | *(채움)* |
| live `/trace_flow/auto` | *(채움 — 진입점만으로 FE/BE/DB 자동수집 + 흐름)* |

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B/H 프로세스 분석·표시 | 9.4/9.0 | **9.5/9.1** | 진입점만으로 횡단 흐름 추적 — 운영 마찰 ↓ |
