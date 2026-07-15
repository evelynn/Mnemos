# Round PR-162 — run.stats가 distinct 현재 그래프를 보고 (dogfood 과대계수 결함)

> **Historical round record.** 아래 `upsert_node` supersede/insert 설명은
> 당시 live-writer 구현을 기록한다. 현재 production ingest는
> run-scoped staging→seal→atomic head publication을 사용하며, 이 보고의 수치와
> 수정은 해당 dogfood round의 historical evidence다.

작성: 2026-06-15 · 브랜치 `claude/round-pr160-analyzer-write-contention` · 이전 commit `792f339`
트리거: Mnemos를 **실제 외부 프로젝트**(Smart-AI-Report-V4 — Next.js 16, src 343파일/57.6k LoC)에
dogfood로 분석해 본 결과 발견. 사용자 요청("부족한 점을 제대로 확인해 개선점을 도출하고 실제로
도움이 될 것을 철저히 검증하라")에 따른 라운드.

## 발견된 결함 (dogfood 실행으로 노출)

Smart-AI-Report-V4를 실제 `run_ingest`로 분석하니 `run.stats`가
**data_entities 66 / contracts 63 / edges 10,826** 을 보고했으나, 실제 distinct 현재
그래프(`valid_to IS NULL`)는 **34 / 47 / 7,808**. 여러 곳에서 참조되는 엔티티(예: 6개 SQL 문에서
쓰인 `dx_ingest_jobs` 테이블 → 6개 data_entity 레코드)가 **참조 횟수만큼 중복 계수**됐다.

근본 원인 — `jobs.py`가 `run.stats = totals`로 설정. `totals`는 `_record_payload`가 ingest
레코드마다 +1 하는 카운터라, 한 노드가 N곳에서 참조되면 N번 계수된다. **그래프 자체는
`upsert_node`의 temporal versioning(supersede+insert)으로 올바르게 dedup**되어 current 1행 +
history N-1행을 유지한다(검증: `data.dx_ingest_jobs` 6행 중 `valid_to IS NULL` 1행). 즉 결함은
**보고 수치에만** 있고 그래프 무결성은 정상. 운영자가 보는 헤드라인이 최대 ~2배 부풀려졌다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 162-1 | `_graph_inventory(session, project_id)` 신설 — distinct 현재 노드(Symbol/Contract/DataEntity, `valid_to IS NULL`) + 현재 엣지 수 계산. `run_ingest` 완료 시 `run.stats = {**totals, **inventory}`로 노드/엣지 헤드라인을 distinct 현재 그래프로 덮어씀. `totals`(진행률·per-stage stats·`_record_payload`)와 temporal upsert 의미는 **불변** | `app/orchestrator/jobs.py` |
| 162-2 | 결정적 회귀 테스트: 같은 data_entity 3회 ingest → 3행(1 current + 2 superseded), `_graph_inventory`가 **1** 보고(`totals`는 3) | `tests/test_pr162_distinct_graph_stats.py` |

## 검증 결과 (실측 — dogfood 재실행)

| metric | fix 전 (`totals`) | fix 후 (distinct 현재) | 보정 |
|---|---:|---:|---:|
| symbols | 1816 | **1768** | −48 |
| contracts | 63 | **47** | −25% |
| data_entities | 66 | **34** | **−48%** |
| edges | 10,826 | **7,808** | −28% |

실제 Smart-AI-Report-V4 `run_ingest` 재실행(157s, completed, 0 errors)으로 fix 전/후 차이 **실측**.
단위테스트에 mutation 대비 내장(`totals`=3 vs `_graph_inventory`=1 대조).

게이트: ruff `server/` **0** · mypy `app` **68**(불변) · pytest `not integration`
**1568 pass / 19 failed**(전부 사전존재 Windows-환경, PR-161 베이스라인 **동일집합 → 회귀 0**) ·
신규 테스트 1 pass.

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| 그래프 데이터 품질 | 8.8 | **8.9** | 운영자-대면 분석 헤드라인이 실제 질의 가능한 그래프와 일치(과대계수 최대 ~2x 제거). 단위테스트 + 실프로젝트 dogfood 실측이 외부 증거(§6-A). FP/EXPOSES 격차는 잔존(아래) → 보수적 +0.1 |

가중평균: 그래프품질 0.08×(+0.1) → +0.008 → 약 **91.3/100**.

## dogfood가 노출한 후속 결함 (검증됨 — 다음 라운드 후보)

- **시스템 카탈로그/키워드 FP**: `sqlite_master`, `pg_namespace`, `dual`, `set`(`UPDATE..SET` 오파싱),
  `schemata`, `user_tables`/`user_tab_columns`/`user_views`, `tables`, `columns` 등이 DataEntity로
  추출됨(provenance 실측: `drivers.ts:introspect`, `setAppSetting`/`saveCache`). 34개 distinct 중 ~9개가
  도메인 아닌 노이즈. → **DataEntity FP 필터 (PR-163 후보)**.
- **EXPOSES 엣지 부재**: ggoss-ts가 Contract 노드는 만들지만 handler→contract `EXPOSES` 엣지를
  안 만듦(edge_kinds = CALLS/READS/WRITES만). → `detect_duplicate_endpoints` + OTLP 런타임
  reconcile(PR-159)가 TS 프로젝트에서 **영구 무발화**. → **PR-164 후보**.
- **0 findings**: finding taxonomy가 그래프 위생 전용(duplicate_endpoint/unverified_claim/dead_path/
  dynamic_call/schema_mismatch)이고 대부분 30일 staleness나 OTLP 런타임 신호 필요 → 신선한 1회
  분석에서 구조적으로 0건. 보안·인가·로직 결함 룰은 부재(설계적 한계). → 별도 설계 논의.
