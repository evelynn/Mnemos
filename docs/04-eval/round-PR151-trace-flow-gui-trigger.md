# Round PR-151 — trace_flow GUI trigger (Ask tab) + 9.8 cold read

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `6b81311`
트리거: `/loop` (목표 9.8) — "유려한 UI/UX·편의 기능 충분한지"; PR-150 이 남긴 UX 후속

## 보완 — GUI 에서 프로세스 추적 트리거

PR-146 은 영속된 흐름을 report 탭에 *표시*만, PR-147 은 `/trace_flow/auto` 를
REST 로만 제공했다. 이제 **Ask 탭에서 진입점만으로 프로세스 추적을 시작**:

| ID | 변경 | 파일 |
|---|---|---|
| 151-1 | Ask 탭에 "Trace as a process (FE→BE→DB)" 버튼 — `entry`(질문)+`source_root` 으로 `POST /trace_flow/auto` 호출 | `app/dashboard/templates/ask.html` |
| 151-2 | 흐름 렌더: tier별 단계 + 신호 + **플래그 값별 의미** + data_touched + 자동수집 파일 배지 | `ask.html` |
| 151-3 | CSS(actions row·flow steps/flags) | `app/dashboard/static/app.css` |
| 151-4 | 회귀 테스트(trace 트리거 와이어링) | `tests/test_pr150_ask_tab.py` |

엔드포인트(`/trace_flow/auto`) 자체는 PR-147 에서 라이브 검증(진입점만으로 3-tier
자동수집 → 7 steps). 본 라운드는 그 **GUI 트리거+렌더** 를 추가.

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| Ask 탭 + UI/UX 감사 | **84 passed** |
| pytest `not integration` (−pr114) | **1477 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live GUI | ✅ `/ask` 에 "Trace as a process" 버튼·traceProcess·flow-steps 렌더 |

## 냉정한 평가 — 9.8 은 이 환경에서 정직하게 도달 불가
가중평균 ≈ **9.05~9.1**. 9.8(consumer-SaaS/글로벌출시)은 **모든 영역 ~9.5+**
요구. 구조적으로 막힌 영역:
- **K 위생 8.2**: mypy 69(전수조사 false-positive). green 만들려면 대량 annotation
  → 하네스가 금지하는 코스메틱 churn. (annotation-only 로 진행은 가능하나 가치<위험)
- **I OTLP 8.0**: live OTel 송신자 미연결(환경 밖).
- **C 배포 8.6**: 실제 docker-compose 미실행(환경 밖).
- 테스트 flake 1(풀스위트 한정), `ScheduleWakeup` 미제공으로 자동 루프 불가.

→ **요구사항 본질(분석·횡단프로세스·상세질의응답·자가심화·GUI 노출)은 충족**.
9.8 의 잔여 격차는 *기능*이 아니라 *환경/툴링/코스메틱*. 정직하게 9.8 도달은
docker·OTel 환경 또는 코스메틱 허용 시에만 가능.

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| H UX/편의 | 9.3 | **9.4** | GUI 에서 Q&A + 프로세스 추적 둘 다 트리거 가능 |
