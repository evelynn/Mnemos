# Round PR-152 — DataEntity (table) L1 summaries

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `decbf29`
트리거: `/loop` — "1번 계속" (PR-142 가 남긴 마지막 실기능 갭)

## 보완

PR-142 의 정직한 한계: 테이블이 그래프(컬럼·PII·FK)엔 있으나 **L1 LLM 요약은 안
붙었다**(`_priority_symbols` 가 `kind=="Symbol"` 전용). "이 테이블은 무엇을 담고
누가 건드리나?" 에 서사가 없었다.

| ID | 변경 | 파일 |
|---|---|---|
| 152-1 | `_priority_data_entities` — DataEntity 를 incoming READS/WRITES/REFERENCES degree 로 랭크(가장 많이 쓰이는 테이블 우선) + filler | `app/extractor/runner.py` |
| 152-2 | `summarise_l1` 이 심볼 + (limit/5 개) 데이터 엔티티를 함께 요약(기존 evidence/budget/persist 로직 재사용) | `app/extractor/runner.py` |
| 152-3 | 회귀 테스트(degree 랭킹) | `tests/test_pr152_data_entity_l1.py` |

### 발견·수정한 버그
`Node.id.in_(top_ids)` 재조회가 **degree 정렬을 보존하지 않음**(IN 임의순서) →
top_ids 인덱스로 재정렬. (`_priority_symbols` 도 동일 패턴이나 entry_rows 와
결합돼 덜 드러났던 잠재 이슈; 본 helper 는 명시적 정렬로 해결.)

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-152 단위 | **1 passed** (degree 랭킹 실측) |
| pytest `not integration` (−pr114) | **1478 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live | SQL 스키마 분석 → **테이블 2/2 가 L1 요약 획득**: `data:orders`("고객 구매 주문 기록…"), `data:customers`("중심 신원·인증 저장소…") |

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| J 데이터 lookup | 8.5 | **8.7** | 테이블 LLM 요약으로 "이 데이터 무엇/누가" 질의 강화 |
| F 그래프 품질 | 9.0→(보수재채점 8.5) | — | (아래 거짓점수 검토 참조) |
