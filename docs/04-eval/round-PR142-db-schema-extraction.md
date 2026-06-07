# Round PR-142 — DB schema extraction from SQL/DDL via Claude Code

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `8504899`
트리거: "디비정보를 저장하면 그 정보도 포함해 분석되는지 / DB 형태는 실제 접속 또는 SQL 제공"

## 조사 — DB 정보가 분석에 들어오는 현재 경로

| 경로 | 형태 | 한계 |
|---|---|---|
| `data_access` (소스 분석기) | 소스코드 내 테이블 참조 → DataEntity + READS/WRITES | 분석기 있는 언어만. C++/분석기-없는 언어는 없음 |
| `live_schema` (`ProjectDB` 등록) | **실제 DB 접속** | **mssql/oracle 방언만**, 분석기 바이너리 필요, docker-free blind |
| (없음) | **SQL/DDL 파일로 제공** | **경로 부재** — `.sql` 스키마는 분석 안 됨 |

→ C++ 와 동일한 구조적 갭: DB 분석이 **방언(mssql/oracle)** 과 **형태(실접속만)** 에
묶여 있고, "sql 로 제공" 형태는 들어올 길이 없었다.

## 보완 — Claude Code 위임을 DB/SQL 로 확장

PR-140 의 에이전트 추출에 **DB 모드**를 추가: 결정적 분석기가 없는 `sql` 언어의
`.sql`/`.ddl` 파일을 Claude Code 가 읽어 **테이블=DataEntity(컬럼·PK·nullable·
PII 플래그) + FK=REFERENCES 엣지**를 추출. 임의 방언(Postgres/MySQL/SQLite/
T-SQL/PL-SQL) 무관.

| ID | 변경 | 파일 |
|---|---|---|
| 142-1 | `sql` 을 agent 언어(`.sql`/`.ddl`) + DB 언어로 등록; DB 전용 system/extract 프롬프트; 추출 결과 `entities` 정규화 | `app/extractor/agent_extract.py` |
| 142-2 | `to_envelopes` DB 분기: `entities` → `record_type=data_entity`(컬럼·민감도 포함), FK → edge | `app/extractor/agent_extract.py` |
| 142-3 | 에이전트 스테이지 `accept` 에 `data_entity` 추가 — **없어서 테이블 노드가 버려지던 버그** fix (edges 만 적재되던 1차 결과) | `app/orchestrator/jobs.py` |
| 142-4 | DB 추출 회귀 테스트 4건 | `tests/test_pr142_db_extraction.py` |

## 실증 (live, Postgres 방언 `schema.sql` 4테이블)

플랫폼이 SQL 파일에서 산출한 분석 결과:
```
run.stats: data_entities=4, edges=3
/data_entities:
  customers       is_sensitive=true   (email, full_name, phone, password_hash → 컬럼별 sensitive)
  payment_methods is_sensitive=true   (card_last4)
  orders          is_sensitive=false
  order_items     is_sensitive=false
customers 컬럼: id(PK), email(PII), full_name(PII), phone(PII,nullable),
               password_hash(PII), created_at  — 타입·PK·nullable·sensitive 전부
FK edges: orders→customers, order_items→orders, payment_methods→customers (전부 inferred)
```

→ **"저장된 DB 정보가 분석에 포함되는가?" → 예.** SQL 로 제공 시, 임의 방언으로
테이블·컬럼·PII·FK 가 그래프 + 데이터-조회(`/data_entities`) 에 들어온다.

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-142 단위 | **4 passed** |
| pytest `not integration` (−pr114) | **1458 passed / 6 failed** (회귀 0; 기존 환경 실패 6) |
| mypy | **69** (불변) |
| live SQL 분석 | **completed** — 4 DataEntity + 컬럼 + PII + 3 FK |

## 정직한 한계
- DataEntity 는 현재 L1 요약 대상이 아님(`_priority_symbols` 가 Symbol 만) — 테이블이
  그래프엔 있으나 LLM 요약은 안 붙음. 후속 과제.
- `component_map` 이 DataEntity FK 의 source/target 을 None 으로 직렬화(컴포넌트
  매핑 기준) — 엣지는 적재됨(stats edges=3). 표시 개선 후속.
- "실제 DB 접속" 형태는 여전히 mssql/oracle live_schema(분석기 필요). Claude 경유 라이브
  introspection 은 DB 격리 원칙상 별도 설계 필요 — 본 라운드는 안전한 "SQL 제공" 형태에 집중.

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| A. 분석기/언어 커버 | 9.0 | **9.2** | DB 분석이 방언·형태 제약에서 해방(SQL 제공 경로 신설) |
| J. 데이터 lookup 안전 | 8.0 | **8.5** | SQL→DataEntity+컬럼별 PII 플래그가 마스킹 레이어로 연결 |
