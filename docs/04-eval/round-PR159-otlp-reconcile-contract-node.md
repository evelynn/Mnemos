# Round PR-159 — OTLP reconcile matches the route on the contract node

작성: 2026-06-11 · 브랜치 `claude/update-readme-docs-0baecs`
트리거: 자율 라운드 단계 1 정찰 — OTLP Tier-2(영역 I, 최저 75점)를 실제로
끝까지 행사해 봄. 토큰 설정 → 트레이스 수신 → 분석 run → reconcile 까지.

## 발견된 결함 (실행으로 노출)

`POST /otlp/v1/traces` 로 `service=orders-api, http.route=/orders, kind=SERVER`
스팬을 보내면 `runtime_observations` 에 `(orders-api, /orders, EXPOSES)` 로
버퍼된다(scrub 도 정상 — 원문 SQL/PII 미저장 확인). 그러나 분석 run 의
findings 단계가 호출하는 `reconcile_observations` 가 이 관측을 **그래프 엣지에
연결하지 못한다**: 관측은 `project_id=NULL` 로 영원히 남고 EXPOSES 엣지의
`exercised` 플래그가 켜지지 않는다.

근본 원인 — `reconcile_observations` 는 관측 operation 을 후보 엣지의
`edge.data["operation"|"path"|"route"]` 와만 비교한다. 그런데 정적 분석기와
seed 는 라우트를 **엣지가 가리키는 contract 노드 id**(`contract:POST /orders`,
analyzer 형 `contract:http:POST:/orders`, 정규형 `http.POST./orders`)에 담고
`edge.data` 는 비워 둔다. 따라서 표준 EXPOSES/CALLS 엣지는 절대 매칭되지 않아
런타임 상관(spec §7.6 Tier-2)이 사실상 무작동이었다.

격리 재현(fresh SQLite + `ensure_sqlite_schema`):
- fix 전: 관측 1건, EXPOSES 엣지 `→contract:POST /orders`(data `{}`) → **matched=0**.
- fix 후: **matched=1**, 엣지 data `{"exercised":"true","hit_count":3,"last_seen_at":…}`,
  decoy `→contract:GET /orders/{id}` 는 미매칭, 관측 project_id 핀.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 159-1 | `_operation_from_node_id(node_id)` 추가 — `contract:POST /orders` / `contract:http:POST:/orders` / `http.POST./orders` 3종 contract id 형태에서 HTTP 경로를 추출(비-HTTP 노드는 None). reconcile 후보 매칭에 `edge.target_id` 경로도 포함 | `app/merge/runtime.py` |
| 159-2 | 헬퍼 단위 테스트(3종 형태 파싱 + 비-HTTP None + 경로템플릿 매칭) | `tests/test_pr69_runtime_path_match.py` |
| 159-3 | end-to-end reconcile 통합 테스트: contract 노드 id 경유 매칭 → exercised/hit_count/last_seen 영속, decoy 미매칭, 관측 project_id 핀 | `tests/test_pr25_otlp_tier2.py` |

## 검증 결과 (실측)

| 항목 | fix 전 | fix 후 |
|---|---|---|
| `(orders-api,/orders,EXPOSES)` 관측 reconcile | matched 0 (unmatched) | **matched 1** |
| EXPOSES 엣지 `→contract:POST /orders` | `exercised` 미설정 | `exercised=true, hit_count=3, last_seen_at` 영속 |
| decoy `→contract:GET /orders/{id}` | — | 미매칭 (정확) |
| 관측 project_id | NULL 영구 | 매칭 시 project 로 핀 |
| PII scrub (부수 확인) | — | 원문 SQL/이메일 미저장 (구조만 보관) |

게이트: ruff 0 · pytest 1577 pass (not integration, −pr114; +3 신규 단위, 통합 1건은
CI Postgres 레인) · mypy 69(불변) · boot ready 200.

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| I. OTLP runtime correlation | 7.5 | **8.6** | Tier-2 reconcile 가 표준 그래프(라우트가 contract 노드에 있는 케이스)에서 처음으로 실제 매칭·`exercised` 표시. 그동안 무작동이던 핵심 경로 복구 |

가중평균 영향: I(가중 0.05) +1.1 → 약 +0.055/10.
