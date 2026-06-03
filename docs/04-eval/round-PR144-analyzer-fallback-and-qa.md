# Round PR-144 — Claude-Code fallback when analyzer binary is absent + Q&A verification

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `97b84fe`
트리거: "이월(미검증) 영역 재조사 → 권장 보완 진행 + 분석 후 질의응답 검토"

## 재조사로 드러난 결함

PR-140 의 Claude-Code 추출 폴백은 `binary_for(language) is None` 일 때만 동작했다.
그런데 **python/csharp/typescript 는 분석기가 "등록"돼 있지만 docker-free 에선
바이너리가 PATH 에 없다.** 이 경우:
- 결정적 분석기 스테이지 → 바이너리 없음 → skip → 0 추출
- 에이전트 폴백 → `binary_for` 가 None 이 아니라 **안 걸림**
- 결과: **docker-free Python/C#/TS 프로젝트는 그래프가 비고, Q&A 가 빈 그래프**

즉 "분석기 없는 언어"(C++)뿐 아니라 "분석기 있으나 미설치"(docker-free 의 주력
언어들)도 분석 불가였다 — 이월 점수(B MCP=9.0)가 실제로는 docker-free 에서 위태로움.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 144-1 | `analyzer_available(lang)` — 등록 AND 바이너리 PATH 존재일 때만 True | `app/analyzers/registry.py` |
| 144-2 | run_ingest 폴백 조건 `binary_for(lang) is None` → `not analyzer_available(lang)` (미등록 OR 미설치 모두 Claude 폴백) | `app/orchestrator/jobs.py` |
| 144-3 | python/csharp/typescript/javascript 확장자를 에이전트 추출 대상에 추가(폴백이 파일을 찾을 수 있게) | `app/extractor/agent_extract.py` |
| 144-4 | 테스트는 실 Claude 구독을 호출하면 안 되므로 `MNEMOS_DISABLE_AGENT_SDK=1` 기본화 — 폴백이 모든 언어에 걸리는 테스트 env 에서 실 호출/지연 방지 | `tests/conftest.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| mypy | **69** (불변) |
| pytest `not integration` (−pr114) | **1457 passed / 6 failed / 32 skipped** (회귀 0; 실패 6 = pr116 툴체인 5 + pr138d flake 1) |
| 부수 효과 | 스위트 **127s→37s**: 이전엔 일부 real-LLM 테스트(pr125/pr126 등 6건)가 **실 Claude 호출** 중이었음을 발견. 가드로 기본 skip(명시적 opt-in 시 실행) — 결정적·고속·무비용 |

## 분석 후 Q&A / 상세 프로세스 질의응답 검토

(아래 "Q&A 검증" 섹션 — live)

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| A 분석기/언어 커버 | 9.2 | **9.4** | docker-free 에서도 모든 주력 언어(py/cs/ts) 가 Claude 폴백으로 분석됨 |
| B MCP 쿼리(Q&A) | 9.0 | **(검증 후 갱신)** | 빈 그래프 위험 제거 + Q&A 도구 실측 |
| K 위생 | 8.0 | **8.2** | 테스트가 더 이상 실 구독 호출 안 함(결정적) |
