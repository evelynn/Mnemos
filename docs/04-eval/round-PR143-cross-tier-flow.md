# Round PR-143 — cross-tier flow / process analysis via Claude Code

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `387681d`
트리거: "전체 프로세스뿐 아니라 FE→BE→DB 를 오가는 신호·세부 플래그 값·그 의미까지 분석"

## 요구

심볼 나열을 넘어, **하나의 프로세스를 프론트엔드 → 백엔드 → 데이터베이스까지
횡단 추적**하고, 각 경계에서 오가는 **신호(요청/응답/쿼리)의 필드·플래그 값과
그 의미, 값이 어디서 어떻게 형성되는지**, 그리고 어떤 데이터가 읽히고 쓰이는지를
세부까지 분석. 스펙 §2 "경계는 contract 로 연결" 원칙의 실현.

## 보완

`POST /api/v1/projects/{id}/trace_flow` 신설 — 진입점(프로세스명)과 관련 FE/BE/DB
소스 파일을 받아 **Claude Code 구독**이 횡단 흐름을 구조화 추출. 결과는 **레벨-4
Summary**(`flow:<slug>`)로 영속되어 지식 그래프/`/summaries` 에서 조회된다.

| ID | 변경 | 파일 |
|---|---|---|
| 143-1 | `analyze_flow_via_agent_sdk` — 단계/신호/플래그-의미/data_touched/open_questions 구조화 추출 프롬프트 + 정규화 | `app/extractor/agent_flow.py` (신규) |
| 143-2 | `POST /trace_flow` 엔드포인트 — 파일별 tier/언어 분류, operator 권한, 레벨-4 Summary 영속, 감사 로그 | `app/api/flow.py` (신규) + `app/main.py` |
| 143-3 | **검증 오류 핸들러 강화** — pydantic v2 의 `ctx.error`(ValueError 객체) 직렬화 실패로 422 가 500 으로 둔갑하던 버그 fix (`json.dumps(..., default=str)`) | `app/obs/errors.py` |
| 143-4 | 회귀 테스트 5건 (분류·슬러그·정규화·프롬프트·라우트) | `tests/test_pr143_flow_trace.py` |

> 143-3 은 PR-142 의 레지스트리 기반 언어검증(field_validator → ValueError)이
> 노출시킨 기존 핸들러 취약점. 이제 어떤 field_validator 오류도 깨끗한 422 반환.

## 실증 (live, 3-tier shop: checkout.js + orders_handler.py + schema.sql)

`trace_flow(entry="Place an order (checkout)")` → **200**, 레벨-4 Summary 영속.
플랫폼이 산출한 분석:

- **8 단계 횡단 추적**: FE `placeOrder` → HTTP `POST /api/orders` → BE
  `handle_create_order` → 위험 게이트 → SQL `INSERT orders RETURNING id` →
  `INSERT order_items ×N` → HTTP 응답 → FE 응답 핸들러(`status===2` 분기).
- **플래그 값별 의미 + 형성 위치**:
  - `status`: 0=PENDING_PAYMENT / 1=PAID / 2=FAILED_RISK. **"1=PAID 는 스키마·FE
    주석엔 있으나 이 핸들러는 안 씀 → 별도 결제 웹훅 추정"** 까지 통찰.
  - `is_gift`: true=기프트 포장+가격 숨김 / false=일반. 3-tier 강제변환 추적.
  - `channel`: web/mobile/kiosk. **kiosk + total≥50000 → status=2** 위험 규칙.
- **data_touched**: `INSERT orders(customer_id,total_cents,status,is_gift,channel)`,
  `INSERT order_items(order_id,sku,qty)`.

(전체 결과: `docs/04-eval/flow-trace-shop3tier-result.json`)

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-143 단위 | **5 passed** |
| pytest `not integration` (−pr114) | **1463 passed / 6 failed** (회귀 0; 기존 환경 실패 6) |
| mypy | **69** (불변) |
| live trace_flow | **200** — 8 steps · 3 flags(값별 의미) · 2 data_touched, 레벨-4 Summary 영속 |
| 422 핸들러 fix | 잘못된 언어 입력이 500→**깨끗한 422** |

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B/E. Claude Code 위임(분석 깊이) | 8.8 | **9.3** | 심볼→**횡단 프로세스/데이터플로우** 분석으로 확장 |
| D. 운영 안전망 | 9.0 | **9.2** | 검증 오류 핸들러가 비직렬화 ctx 에 견고 |

## 정직한 한계
- 무거운 흐름은 Claude 호출 ~3-5분(동기 엔드포인트, timeout 300s). 다파일 대형
  흐름은 비동기 잡 전환이 후속 과제.
- 현재 `source_paths` 를 명시. 그래프에서 자동으로 관련 파일을 모으는 entry→files
  해석(contract 추적 기반)은 후속.
- MCP 툴(`trace_flow`)로 노출하면 Claude Code 가 직접 호출 가능 — 후속.
