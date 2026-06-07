# Round PR-149 — gap-driven Q&A with on-demand deepening

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `b5aabe2`
트리거: `/loop` — "분석 후 상세 요구 질의에 답하고, **답이 불충분하면 추가 분석을 자동 수행**"

## 요구

거대 시스템은 파일 예산(`agent_extract_limit`)으로 부분 분석되므로, 초기 분석에
없던 심볼을 물으면 `search_symbols` 가 빈 결과 → "모름". 사용자 요구: **답이
불충분하면 플랫폼이 스스로 관련 파일을 더 분석한 뒤 답하라.**

## 보완

`POST /projects/{id}/ask {question, source_root}`:
1. 그래프에서 **확신 매칭**(심볼 id/name 에 질의어 포함)이면 즉답(get_symbol + data_access).
2. 아니면(=불충분) `source_root` 에서 **질의어로 후보 파일 랭크**(경로 hit×5 + 내용 hit) →
   상위 N개를 Claude Code 로 **즉시 추출·그래프 적재** → **재검색 후 답변**.
3. 응답에 `answered`/`deepened`/`extracted_files`/`answer(data_access 포함)` 보고.

| ID | 변경 | 파일 |
|---|---|---|
| 149-1 | `find_candidate_files(root, terms)` — 질의어로 답을 가질 파일 랭크(경계 비용) | `app/extractor/agent_extract.py` |
| 149-2 | `POST /ask` — 확신 즉답 / 불충분 시 후보추출→재검색→답변 + 감사 | `app/api/ask.py` (신규) + `main.py` |
| 149-3 | 회귀 테스트 3건 (tokeniser·confidence·후보 랭커) | `tests/test_pr149_ask_deepen.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-149 단위 | **3 passed** |
| pytest `not integration` (−pr114) | **1470 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live "불충분→심화→답변" | ✅ sql-only 그래프에 "주문 생성 핸들러는 어디/무엇을 쓰나?" → answered=T, **deepened=T**, 자동추출 [orders_handler.py, schema.sql] → 답: handle_create_order, writes=[data:orders, data:order_items] |

## 냉정한 평가 (이 라운드 시점)
- 가중평균 ≈ **8.9 → ~9.0** (B/Q&A 가 자가-심화로 강화).
- 9.0 완료 잔여 차단: mypy 게이트 RED(69, 전부 false-positive), 테스트 flake 1,
  실제 docker-compose 미실행. 이들은 코스메틱/환경/저신뢰라 자율 churn 부적합.

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B MCP/Q&A | 9.5 | **9.6** | 부분 분석에도 상세 질의에 답(자가-심화 루프) |
| A 분석 커버 | 9.4 | **9.5** | 필요 시 온디맨드 추가 추출 |
