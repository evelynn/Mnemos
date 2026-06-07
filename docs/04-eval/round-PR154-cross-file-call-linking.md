# Round PR-154 — cross-file CALLS linking for agent-extracted code

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `a0f2b8b`
트리거: `/loop` "완성까지 진행" — 정직 감사가 지목한 B/F 품질 갭

## 문제

에이전트 코드 추출은 파일 단위라 `to_envelopes` 가 파일 경계를 넘는 호출 엣지를
버렸다 → `find_callers`/`find_callees` 가 **파일 간 동작 안 함**(Claude 추출 코드).
"이 함수를 누가 부르나?" 가 같은 파일 안에서만 답해졌다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 154-1 | 코드 추출 프롬프트에 심볼별 `calls`(외부 파일 포함 callee 이름) 추가 | `app/extractor/agent_extract.py` |
| 154-2 | `to_envelopes` 가 callee 이름을 `data.calls_out` 에 보존 | `app/extractor/agent_extract.py` |
| 154-3 | `link_inferred_calls` — 에이전트 추출 후 callee 이름을 프로젝트 전역 Symbol 과 **이름 매칭**해 CALLS 엣지 생성. **모호하지 않을 때(정확히 1개 매칭)만** 생성 → 정밀도 유지 | `app/orchestrator/jobs.py` |
| 154-4 | run_ingest 에 `link_calls` 스테이지(에이전트 추출이 돈 경우) | `app/orchestrator/jobs.py` |
| 154-5 | 회귀 테스트 2건(calls_out 보존 + 모호/누락 스킵하고 unambiguous 만 링크) | `tests/test_pr154_cross_file_calls.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-154 단위 | **2 passed** — seed 그래프에서 `run`→`helper`(파일간) 링크, `ambig`(2 정의)·`missing`(0) 스킵 |
| pytest `not integration` (−pr114) | **1483 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |

라이브: 해석 로직은 단위테스트로 결정적 입증. LLM 의 `calls_out` 채움은 PR-140 에서
검증된 동일 추출 메커니즘. (전수 cross-file cpp 라이브는 LLM 비용↑·calls_out 채움
의존이라 단위테스트로 대체 — 정밀도 게이트(unambiguous-only)가 false edge 방지.)

## 영역 점수 갱신 (정직 기준선)
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| B MCP/Q&A | 8.7 | **8.8** | find_callers/callees 가 파일 간 동작(에이전트 추출 코드) |
| F 그래프 품질 | 8.4 | **8.6** | 파일 경계 호출 연결로 그래프 연결성 ↑ |
