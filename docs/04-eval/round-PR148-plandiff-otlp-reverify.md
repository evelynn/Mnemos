# Round PR-148 — Plan/Diff + OTLP re-verification (carried → verified)

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `64ae582`
트리거: "이월 영역(G Plan/Diff, I OTLP) 재검증"

anti-drift #1(검증 전 미신뢰)에 따라, score-evidence 에서 이월돼 있던 G/I 점수를
docker-free 라이브 실행으로 실측 전환.

## G. Plan/Diff 워크플로 — 라이브 실측

데모 프로젝트의 finding 에서 전체 라이프사이클을 HTTP 로 구동:

| 단계 | 호출 | 결과 |
|---|---|---|
| Plan 생성 | `POST /findings/{id}/plan` | ✅ plan (pending_approval) |
| Gate A | `POST /plans/{id}/decide {"status":"approve"}` | ✅ approved |
| Diff 제출 | `POST /diff_submissions {plan_id,task_id,diff}` | **처음 500 → fix 후 200** |
| Gate B | `POST /diff_submissions/{id}/approve` | ✅ approved_no_mr (GitLab 미설정 시 정상) |
| break-glass | `POST /diff_submissions/{id}/break_glass_grant {rationale≥200자}` | ✅ granted (200자 미만 → 422, 의도된 마찰) |

### 발견·수정한 실버그 (G)
`POST /diff_submissions` 가 **500** — 핸들러가 `report.as_jsonable()`(전체
`{verdict,passes,findings}` **dict**)를 `auto_review_findings` 에 저장했으나, 모델·
GUI·`DiffOut` 은 **findings 리스트**를 기대 → `DiffOut.auto_review_findings: list`
검증 실패. **HTTP 경로가 end-to-end 로 테스트된 적 없어** 놓친 "tests reality" 갭
(test_pr120 은 `DiffSubmission` 행을 직접 구성).
**Fix**: `[f.as_jsonable() for f in report.findings]`(리스트)만 저장. (`app/api/diffs.py`)

## I. OTLP runtime — 라이브 실측

| 항목 | 결과 |
|---|---|
| 토큰 없음 | **401** (fail-closed, `MNEMOS_OTLP_TOKEN`) |
| 토큰 + span | **200** `{"accepted":1,"buffered":0}` (org 헤더 없으면 버퍼 스킵) |
| 토큰 + org 헤더 + span | **200** `{"accepted":1,"buffered":1}` → `runtime_observations` 에 `('orders-api','/orders/{id}','EXPOSES',1)` 적재 |
| scrub | `db.statement` 의 PII 스크럽 경로 작동 |

→ 수신·인증(fail-closed)·스크럽·버퍼·`runtime_observations` 적재(reconcile 대상)
전 경로 실측. (live OTel SDK 송신자 연결만 미실행 — 본 환경 밖)

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-148 단위 + pr120 + pr121 | **21 passed** |
| pytest `not integration` (−pr114) | **1467 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |

## 영역 점수 갱신 (이월 → 실측)
| 영역 | 이월 | 실측 | 근거 |
|---|---:|---:|---|
| G Plan/Diff | 8.0 | **8.5** | 전체 라이프사이클 HTTP 실측 + diff_submit 500 실버그 fix |
| I OTLP | 7.5 | **8.0** | 수신·인증·스크럽·버퍼·적재 실측 (live 송신자만 미연결) |
