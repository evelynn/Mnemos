# Round PR-146 — "show the process" surfaces (MCP + GUI)

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `39ab406`
트리거: "다양한 질문에 대답 + 프로세스 요청 시 **보여주는 부분**도 문제없는지 확인"

## 검증으로 드러난 갭

질의응답(search/get_symbol/get_data_access/data-entity)은 PR-145 에서 실측 OK.
"프로세스를 **보여주는** 부분"을 코드로 점검하니 두 표시 surface 모두 결함:

1. **MCP**: trace_flow(PR-143)는 REST 전용 — MCP 도구 목록에 흐름 관련이 전무.
   Claude Code 가 "어떤 프로세스가 분석됐나 / 그 프로세스 보여줘" 를 물을 도구가
   없고, 영속된 level-4 흐름을 발견할 길도 없음(`get_module_summary` 는 정확한
   `target_id`+`level` 을 이미 알아야 함).
2. **GUI**: report 탭이 `?level=3` 만 fetch → **level-4 흐름은 어디에도 렌더 안 됨**.

`/summaries?level=4` 는 흐름 전체(summary/detailed/claims=steps·flags)를 이미
API 로 제공하므로, 데이터는 있고 **표시만 누락**된 상태였다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 146-1 | `list_flows(project_id)` 쿼리 헬퍼 — level-4 요약 + step/flag/data 섹션 반환(1콜로 프로세스 표시) | `app/mcp/queries.py` |
| 146-2 | MCP `list_flows` 도구 등록 + dispatch — Claude Code 가 프로세스 발견·표시 | `app/mcp/server.py` |
| 146-3 | report 탭에 "Cross-tier processes (flows)" 패널 — `?level=4` fetch, 단계(tier별)·플래그(값별 의미) 렌더 | `app/dashboard/templates/report.html` |
| 146-4 | 회귀 테스트 3건(헬퍼 존재·MCP 등록/dispatch·GUI 와이어링) | `tests/test_pr146_flow_display.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-146 단위 + 템플릿 렌더(pr129) | **80 passed** |
| mypy | **69** (불변) |
| pytest `not integration` (−pr114) | *(채움)* |
| live MCP `list_flows` / Q&A 도구 | *(채움)* |
| live GUI `/report` + `/summaries?level=4` | *(채움)* |

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B MCP 쿼리(Q&A) | 9.2 | **9.4** | 프로세스 발견/표시 도구(list_flows) 추가 — Claude Code 가 흐름을 보여줄 수 있음 |
| H UX/표시 | 8.8 | **9.0** | report 탭이 횡단 프로세스를 단계·플래그 의미와 함께 렌더 |
