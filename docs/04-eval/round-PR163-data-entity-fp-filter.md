# Round PR-163 — DataEntity 시스템 카탈로그/키워드 false-positive 필터 (dogfood)

작성: 2026-06-15 · 브랜치 `claude/round-pr160-analyzer-write-contention` · 이전 commit `6647bca`
트리거: PR-162 dogfood(Smart-AI-Report-V4)의 후속. 같은 실행에서 DataEntity 추출이 SQL
시스템 카탈로그와 파서 아티팩트를 도메인 테이블로 잘못 잡는 것을 provenance까지 실측.

## 발견된 결함 (dogfood provenance로 노출)

Smart-AI-Report-V4 분석의 distinct DataEntity 34개 중 ~8개가 **도메인 테이블이 아닌 노이즈**였다.
provenance(`investigate.py`로 실측한 READS/WRITES 출처):
- `sqlite_master`(×5 ref) ← `schema.ts:initializeSchema`, `drivers.ts:introspect` (스키마 intro스펙션)
- `information_schema` 계열 `schemata`/`tables`/`columns`, `pg_namespace` ← `drivers.ts:introspect`
- Oracle 데이터 딕셔너리 `user_tables`/`user_tab_columns`/`user_views`, `dual` ← `drivers.ts:introspect`, `testConnection`
- `set` ← `app-settings.ts:setAppSetting`, `ocr-cache.ts:saveCache` — **`UPDATE x SET ...`를 테이블 `set`으로 오파싱**(ggoss-ts SQL 파서 버그)

이들은 앱이 실제로 질의하는 시스템 카탈로그(introspection)이거나 파서 아티팩트라, 제품의
**도메인 데이터 모델이 아니다**. 데이터 맵을 오염시키고 schema_mismatch 오탐 위험을 만든다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 163-1 | `_is_system_data_entity(name)` + 보수적 denylist(`_SYSTEM_DATA_ENTITY_NAMES`/`_PREFIXES`). `_record_payload`의 data_entity 분기에서 시스템 엔티티 노드 생성 skip, edge 분기에서 그 엔티티로의 READS/WRITES skip. 모호한 일반 단어(`tables`/`columns`/`views`)는 **의도적으로 제외**(도메인 테이블 오삭제 방지) | `app/orchestrator/jobs.py` |
| 163-2 | 헬퍼 단위 테스트 + 통합 테스트(시스템 엔티티 노드+엣지 drop, 도메인 엔티티 보존) | `tests/test_pr163_system_entity_filter.py` |

## 검증 결과 (실측 — dogfood 재실행)

| | fix 전 (PR-162 후) | fix 후 |
|---|---:|---:|
| distinct DataEntity | 34 | **26** |
| 제거된 FP | — | set, dual, sqlite_master, pg_namespace, schemata, user_tables, user_tab_columns, user_views (8) |
| 보존된 도메인 테이블 | — | users, reports, dx_*(15), billing_events, workspace_subscriptions, audit_logs, apps, db_reports, user_sessions … (전부) |
| 잔존(의도적, 모호) | — | tables, columns (2) |

실제 Smart-AI-Report-V4 `run_ingest` 재실행으로 실측. 게이트 GREEN — ruff 0, mypy 68(불변),
pytest not-integration **1570 pass / 19 사전존재 Windows-환경**(PR-162 베이스라인 동일집합·회귀 0),
신규 테스트 2 pass.

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| 그래프 데이터 품질 | 8.9 | **9.0** | PR-162(정확 카운트) + PR-163(도메인-only 데이터 맵)으로 데이터 차원이 질의 결과와 일치. dogfood 실측 + 단위테스트가 외부 증거. **단, 구조적 잔존 격차 있음**(아래) → 9.0 상한 |

가중평균: 그래프품질 0.08×(+0.1) → +0.008 → 약 **91.4/100**.

## 잔존/근본 격차 (정직 표기)
- **ggoss-ts `set` 파서 버그**(근본): `UPDATE..SET`을 테이블로 오파싱. 본 라운드는 ingest에서
  중앙 필터로 가렸을 뿐, 분석기 SQL 파싱 수정이 근본 해결(별도 analyzer 라운드 후보).
- `tables`/`columns` 모호 잔존(보수적 설계로 미필터).
- **EXPOSES 엣지 부재**(PR-164 후보): duplicate_endpoint + OTLP reconcile가 TS에서 무발화.
