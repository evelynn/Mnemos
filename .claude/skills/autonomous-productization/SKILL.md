---
name: autonomous-productization
description: Mnemos(Python/FastAPI)용 자율 상품화 하네스. 사용자의 추가 지시 없이도 가중평균 점수가 상품화 목표(기본 9.8/10)에 도달할 때까지 자가-진단 → 우선순위 fix → 게이트 검증 → 커밋·푸시를 반복한다. *진짜 결함과 기능 향상만* 작업하고 코스메틱 변경은 금지한다. ruff/mypy/pytest + docker-free boot 을 자동화 게이트로 사용한다.
---

# Autonomous Productization Harness (Mnemos / Python)

> 원본은 TS/Next.js 프로젝트(npm/vitest/tsc)용이었다. 본 버전은 Mnemos
> (Python 3.12 · FastAPI · SQLAlchemy · pytest) 에 맞춰 **게이트·영역·측정
> 명령·라운드 코드**를 재정의한 업그레이드본이다. 한 번 invoke 하면 종료
> 조건(§7) 도달까지 멈추지 않는다.

---

## 1. 미션 (변하지 않는 한 줄)

> **목표 점수(가중평균)** 와 **모든 영역 minimum** 에 도달할 때까지, 자율적으로
> 영역별 점수 갭을 측정·우선순위화·fix·검증·커밋·푸시 한다. 사용자에게 묻지 않는다.

기본 목표:
- 가중평균 ≥ **9.0/10** (Mnemos 자체 score-evidence 기준 90.7 → 점진 상향)
- 모든 영역 ≥ **8.5/10**
- 보안(운영 안전망) 영역 ≥ **9.5/10**
- 자동화 게이트 모두 GREEN (ruff 0 · pytest pass · docker-free boot OK)

선택 인자: `target_avg`, `target_min`, `target_security`, `max_rounds`, `dry_run`.

---

## 2. 자동화 게이트 (Mnemos 전용 — 매 라운드 필수 GREEN)

커밋 전 다음이 모두 통과해야 한다. `cd` 금지, 절대경로 venv 사용.

```bash
V=/home/user/Mnemos/server/.venv/bin
# 1. 린트 — 0 에러 필수
$V/ruff check /home/user/Mnemos/server/app
# 2. 비통합 테스트 — 신규 회귀 0
$V/python -m pytest -p no:cacheprovider -m "not integration" \
  --deselect /home/user/Mnemos/server/tests/test_pr114_real_analyzer.py -q
# 3. 타입 — 신규 에러 0 (기존 debt 는 줄이되 round 차단 금지)
$V/mypy /home/user/Mnemos/server/app 2>&1 | tail -1
# 4. docker-free boot smoke — 헤드리스 부팅 + /health/ready 200
$V/python -m app.serve_local --reset --port 8099 & sleep 4
curl -fsS http://127.0.0.1:8099/api/v1/health/ready ; pkill -f "[s]erve_local"
```

**게이트 해석**
- ruff·pytest·boot 는 **반드시 GREEN**. 실패 시 root-cause 수정, 우회(`--no-verify`) 금지.
- mypy 는 기존 debt(현재 71건, 전부 false-positive/주석노이즈로 확인됨)가 있으므로
  "**신규 에러 0 + 총 에러 단조 감소**" 를 게이트로 한다. round 가 mypy 총량을
  늘리면 revert.
- pytest 의 알려진 환경 실패(분석기 툴체인 5건 = node_modules 미설치)는 deselect 또는
  카운트에서 분리. 코드 회귀와 환경 실패를 혼동하지 말 것.

5회 시도해도 GREEN 불가 → 단계 4 변경 revert 후 더 작게 재시도.

---

## 3. 영역 정의 + 가중치 (Mnemos KPI)

Mnemos `docs/operator-guide/score-evidence.md` 의 차원을 KPI 로 채택한다.

| 영역 | 가중 | 측정 지표 (verified) |
|---|---:|---|
| A. 분석기 실작동 | 15% | 6종 빌드/probe, 4종 정확도 floor (`scripts/accuracy/measure.py`) |
| B. MCP 쿼리 실데이터 | 15% | `app/mcp/*` 핸들러가 real SQLAlchemy 로 응답, search/get/callers |
| C. 운영 검증(배포) | 12% | docker-free boot + 핵심 플로우(analysis run 완주·sample 마스킹·invite) |
| D. 운영 안전망/보안 | 12% | startup-verify, 보안헤더 6/6, CSRF, OIDC nonce, 데이터접근 게이트 |
| E. L1~L3 LLM | 8% | stub/real-anthropic/Agent-SDK 경로, fallback_reason 영속 |
| F. 그래프 데이터 품질 | 8% | dogfood 추출→적재→검증, certainty 분류 |
| G. Plan/Diff 워크플로 | 8% | Plan→Gate A→Diff→break-glass→MR lifecycle |
| H. UX/진입마찰 | 8% | seed-demo, getting-started, /docs, GUI 탭 26개 |
| I. OTLP runtime | 5% | receive_traces, scrub, assemble_trace_tree |
| J. 데이터 lookup 안전 | 5% | sample 마스킹(PII), query_data rate-limit/audit |
| K. 코드/타입 위생 | 4% | ruff 0, mypy 단조감소, 테스트 reality(grep 아닌 실행) |

`가중평균 = Σ(점수 × 가중)`. sub-feature 평균으로 영역 점수 산정(§6 rubric).

---

## 4. 자율 루프 (한 사이클 = 한 라운드)

### 단계 1 — 현재 상태 측정 (추측 금지, 코드/실행 근거만)

```bash
V=/home/user/Mnemos/server/.venv/bin
# 규모
find /home/user/Mnemos/server/app -name "*.py" | xargs wc -l | tail -1
grep -rn "def test_" /home/user/Mnemos/server/tests | wc -l
# 보안/안전 (D)
grep -rn "require_project_org\|CurrentUser\|Depends(require" /home/user/Mnemos/server/app | wc -l
grep -rn "audit_record\|await audit" /home/user/Mnemos/server/app | wc -l
# 위생 (K)
$V/ruff check /home/user/Mnemos/server/app 2>&1 | tail -1
$V/mypy /home/user/Mnemos/server/app 2>&1 | tail -1
# reality: grep-only vs real-exec 테스트 비율
grep -rln "ASGITransport\|TestClient\|await client\|asyncio.run" /home/user/Mnemos/server/tests | wc -l
```

각 영역을 §6 rubric 으로 0~10. 점수마다 **file:line 또는 셈 결과** 인용 필수.

### 단계 2 — 갭 분석
```
Gap(area)=max(0, target_min−current);  WeightedImpact=Gap×weight
```
최대 WeightedImpact 영역을 타깃. tie 면 effort 작은 쪽.

### 단계 3 — 라운드 계획 문서
`docs/04-eval/round-PR<NNN>-<slug>.md` 신규. `<NNN>` = `git log --oneline | grep -oE 'PR-[0-9]+' | head -1` 의 다음 번호.
표: 갭 측정 / 본 라운드 작업(ID·파일·영역·예상Δ) / 검증(종료후 채움).

### 단계 4 — 코드 변경 (허용/금지)

**허용**: ① 버그 fix(현 동작 ≠ spec/주석/유저기대) ② 영역 갭을 직접 닫는 누락기능 보강
③ ①②에 부속된 테스트·타입·문서.

**금지**: 작동코드 리네이밍·스타일통일·갭무관 추상화·자명한 주석·비보안 의존성업데이트·
"더 깔끔"으로 정당화되는 변경·"유저가 원할것" 추측.

**원칙**: 한 사이클 신규파일 ≤5·수정 ≤15. 데이터-의미 코드는 단위테스트 우선.
API 응답 변경은 항상 클라이언트(템플릿/MCP) 동기화. Edit 우선, 기존파일 Write 덮어쓰기 금지.

### 단계 5 — 게이트 (§2) 전부 GREEN.

### 단계 6 — 라운드 문서 "검증 결과" 실측 채움 + 영역 갱신표.

### 단계 7 — 커밋
```bash
git -C /home/user/Mnemos add -A
git -C /home/user/Mnemos commit -m "round-PR<NNN>: <요약> — 가중평균 <before>→<after>

<영역 변화>

게이트: ruff 0, pytest <P>/<T> pass, mypy <N>(−Δ), boot OK. 누적 <K>건."
```
> 커밋 메시지에 모델 식별자/마케팅명 포함 금지. PR 본문/코드에도 금지.

### 단계 8 — 푸시
```bash
git -C /home/user/Mnemos push -u origin <designated-branch>
```
실패 시 2s/4s/8s/16s backoff 4회. → **단계 1 로 복귀**.

---

## 5. Anti-drift 규칙

1. **agent/문서 보고는 검증 전 미신뢰** — `wc -l`/`grep`/실행으로 직접 확인.
   (실제: score-evidence 가 docker-free "전부 200" 이라 했으나 build test 결과
   analysis run 은 ProgressBus 의 real-Redis dial 로 크래시했음. PR-138~ fix.)
2. **이전 라운드 fix 재작업 금지** — `git log --oneline -30`.
3. **target_min 충족 영역은 미세향상보다 갭 큰 영역 우선.**
4. **mypy/ruff 경고가 곧 버그는 아니다** — short-circuit 으로 안전한 union-attr
   false-positive 다수. *런타임 안전 코드를 mypy 만족 위해 바꾸는 것은 코스메틱*.
   실버그(None-deref 가 실제 도달 가능)만 fix, 나머지는 annotation/override 로 처리.
5. **테스트는 grep 매칭 말고 실행**(ASGITransport/asyncio.run). pre/post-fix 로
   실패↔통과가 갈리는 regression-guard 를 선호.

---

## 6. 점수 rubric (sub-feature 0~10)

| 상태 | 점수 |
|---|---:|
| 부재 | 0 / 일부·작동불가 | 2 / 작동하나 발견難 | 4 / 기본가능·edge미흡 | 6 |
| 핵심+edge·일관 | 8 / 모범사례+테스트+문서 | 9 / 업계상위·보강불필요 | 10 |

영역점수 = sub 평균. target 9+ 는 모든 sub 가 테스트·문서·edge 까지.

---

## 7. 종료 조건

하나라도 충족 시 정지 + 보고:
1. 목표 도달(가중평균≥target_avg AND 모든영역≥target_min AND 보안≥target_security)
2. 연속 3 라운드 평균 Δ ≤ 0.02 (수렴)
3. 게이트 5회 시도 GREEN 불가(외부환경)
4. 사용자 명시 중단

정지 메시지: 사유 / 현재 가중평균 / 미달영역 / 다음 권장 plan / 최근 커밋 list.

---

## 8. 실패 모드 & 복구

| 증상 | 원인 | 복구 |
|---|---|---|
| pytest 다수 실패 | source 회귀 | 변경 revert, 더 작게 |
| pytest 1-2 mismatch | 테스트식 좁음 | 식 완화, source 유지 |
| 분석기 테스트 실패 | node_modules/dotnet 미설치 | **환경문제** — deselect, 회귀 아님 |
| mypy 총량 증가 | 신규 annotation gap | 추가 annotation/`# type: ignore[code]` |
| boot 실패 | 모델 import 누락→테이블 미생성 | `ensure_sqlite_schema` import 목록 점검 |
| push 실패(네트워크) | sandbox 일시차단 | exponential backoff 4회 |

---

## 9. 라운드 코드

Mnemos 는 `PR-<NNN>` 단조증가(현재 최신 PR-138h). 다음 라운드 = 최신 +1.
`git log --oneline | grep -oE 'PR-[0-9]+' | sort -t- -k2 -n | tail -1` 로 확인.

---

## 10. 첫 invoke 시퀀스

```
1. git log --oneline | grep -oE 'PR-[0-9]+' | tail -1     # 최신 라운드
2. §4-단계1 측정 → 현재 가중평균
3. WeightedImpact 최대 영역 식별
4. plan 문서 → 코드 → 게이트(§2) → 커밋 → 푸시
5. §7 종료조건 확인 → 미충족 시 1 반복
```
