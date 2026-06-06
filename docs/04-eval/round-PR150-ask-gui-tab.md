# Round PR-150 — Ask (Q&A) GUI tab + UX review

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `d901814`
트리거: `/loop` — "유려한 UI/UX·사용하기 편리한 기능들이 충분한지 검토"

## UX 감사로 드러난 갭

GUI nav(12탭: Dashboard·Projects·Analysis·Data·Graph·Plans·Diffs·Findings·
Report·Audit·Profile·Settings)를 점검한 결과: 이번 세션에 추가한 **가장 강력한
편의 기능 — `/ask`(자가-심화 Q&A)·`/trace_flow`(횡단 프로세스) — 이 REST 전용으로
GUI 어디에도 노출되지 않음.** 운영자가 발견·사용할 수 없는 상태.

## 보완 — "Ask" 탭 신설

| ID | 변경 | 파일 |
|---|---|---|
| 150-1 | `ask.html` — 프로젝트 피커(드롭다운, UUID 직접입력 불요) + 질문 입력 + "Deepen options"(소스루트·심화 토글) + 답변 카드(매칭 심볼·시그니처·요약·**Writes/Reads**·`Deepened` 배지·기타 매칭) | `app/dashboard/templates/ask.html` (신규) |
| 150-2 | nav 에 "Ask" 추가 + 유효 탭 등록 | `_layout.html`, `app/dashboard/router.py` |
| 150-3 | Ask 탭 CSS(카드·배지·스피너, `prefers-reduced-motion` 존중) | `app/dashboard/static/app.css` |
| 150-4 | 회귀 테스트 3건 + UI/UX 감사(pr129)에 `/ask`·`ask.html` 등록 | `tests/test_pr150_ask_tab.py`, `test_pr129_ui_ux_audit.py` |

UX 품질 준수: 기존 규약 그대로 — `MnemosUI` 헬퍼(자동 CSRF 주입 fetch·toast·
showError·i18n `data-i18n`·project picker), aria-live 로딩, sr-only 제목,
escapeHtml 전수 적용(XSS 감사 통과).

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-150 단위 + UI/UX 감사(pr129) | **83 passed** |
| pytest `not integration` (−pr114) | **1476 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live GUI | ✅ `GET /ask` 200(17.8KB, "Ask the system"·askQuestion·mountProjectPicker), nav 에 Ask 노출 |

## 냉정한 평가 (UX 관점)
- **개선**: 핵심 Q&A 기능이 이제 GUI 에서 한 번에 사용 가능(질문→답변+데이터접근+심화 배지).
- **남은 UX 후속**: trace_flow 트리거 GUI(현재 Ask 로 질의는 가능하나 흐름 시각화 트리거 버튼 없음 — report 탭은 표시만), 모바일 좁은 폭 nav, 키보드 단축키.

## 영역 점수 갱신
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| H UX/편의 | 9.1 | **9.3** | 강력 기능(Q&A)의 GUI 노출 — 발견성·사용편의 ↑ |
