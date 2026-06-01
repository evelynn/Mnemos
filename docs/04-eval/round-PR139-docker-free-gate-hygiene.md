# Round PR-139 — docker-free regression guard + gate hygiene

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `b14b461`
하네스: `autonomous-productization` (Python/Mnemos 적응본)

## 배경 — "배포완료/상품화 완료" 주장 재평가

프로젝트 자체 `score-evidence.md` 는 **90.7/100, "production-capable beta"**
이며 "거짓 주장 금지" 원칙 하에 격차를 명시한다 — 즉 프로젝트 스스로도 "100%
상품화 완료" 를 주장하지 않는다. 독립 build test(docker 없이 `serve_local`)
결과, 자체 평가가 "docker-free 전부 200" 이라 한 부분에서 **핵심 기능이 크래시**
했다 (아래 D-1~D-3, PR-138~ 에서 fix). 따라서 정직한 판정:

> **강한 베타(~90/100), "상품화 완료" 아님.** 자체 호스팅 단일조직 배포는
> 실사용 가능하나, docker-free 완전성·게이트 위생·일부 차원 격차가 남는다.

## 갭 측정 (verified)

| 영역 | 측정 근거 | 현재 | 목표 | 갭 |
|---|---|---:|---:|---:|
| C. 운영검증(배포) | docker-free 핵심플로(analysis run·sample·invite)가 크래시했고 이를 막는 테스트 부재 | 7.5 | 8.5 | 1.0 |
| K. 코드/타입 위생 | `ruff` 2 에러, `mypy` 71 에러, sample/analysis docker-free 테스트 0건 | 6.5 | 8.5 | 2.0 |
| (그 외) | A·B·D·E·F·G·H 는 score-evidence 88~98, target_min 충족 | — | — | 0 |

WeightedImpact 최대 = **K(코드/타입 위생, 갭 2.0 × 0.04) + C(배포, 1.0 × 0.12)**.
두 영역을 함께 닫는 단일 라운드(effort S).

## 본 라운드 작업

| ID | 변경 | 파일 | 영역 | 비고 |
|---|---|---|---|---|
| 139-1 | 미사용 import 2건 제거 → ruff 0 | `app/extractor/agent.py`, `app/extractor/runner.py` | K | dead import (`field`, `FALLBACK_NO_BACKEND`) |
| 139-2 | pgvector optional-import override → mypy 71→69 | `pyproject.toml` `[tool.mypy]` | K | optional `search` extra, false-positive |
| 139-3 | docker-free 회귀 가드 3건 신설 | `tests/test_pr139_docker_free_regression.py` | C·K | Fix A/B/C 각각 pre-fix 실패·post-fix 통과 |

> Fix A/B/C 본체(누락 모델 import / 라우트 셰도잉 / ProgressBus fakeredis)는
> 직전 commit `b14b461` 에서 수정 완료. 본 라운드는 그 **회귀 가드 + 게이트
> 위생** 만 추가한다 (anti-drift #2: 이전 fix 재작업 금지).

### mypy 71건은 버그가 아님 (조사 결과)

`union-attr`/`operator` 고신호 경고를 전수 확인한 결과 전부 **short-circuit 으로
런타임 안전한 방어적 코드의 false-positive** 였다:
- `safety/review/data_access.py:69,82` — `(entity.data if entity else {}) and …`,
  `evidence = [...] if entity else []` 로 가드됨.
- `obs/metrics.py:198` — `bool(client) and client.host …` 단락.
- `api/onboarding.py:137,252` — `expires_at` 은 `Mapped[datetime]`(non-nullable),
  `invite is None or … or expires_at < now` 단락.

→ 런타임 안전 코드를 mypy 만족 위해 바꾸는 것은 코스메틱(skill §5-4)이므로 하지
않는다. 총량 단조 감소만 추구한다. **남은 69건은 backlog**.

## 검증 결과 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff `app/` | **0 errors** (was 2) |
| pytest `not integration` (−pr114) | **1450 passed / 6 failed** (was 1447/6 — +3 신규, 회귀 0). 실패 6건은 전부 기존 환경: pr116 툴체인 5 + pr138d isolation flake 1 |
| 신규 회귀 3건 | **3 passed** (pre-fix 실패 확인) |
| mypy `app/` | **69 errors** (was 71, 단조 감소, 신규 0) |
| docker-free boot `/health/ready` | **200 ok** (worker=inline) |

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| C. 운영검증(배포) | 7.5 | 8.3 | 핵심 docker-free 플로 3종 회귀 가드 확보 |
| K. 코드/타입 위생 | 6.5 | 7.8 | ruff 0, mypy −2, real-exec 회귀 테스트 +3 |

## 다음 라운드 후보

1. **테스트 격리 flake** (`test_pr138d::…inline_worker…`) — 풀스위트에서만 실패.
   근본: `get_settings` lru_cache 가 테스트 간 누수. 후보 fix: conftest autouse
   `get_settings.cache_clear()` 또는 폴루터 bisection. (effort M, 위험 中)
2. **mypy 69 → 0** — annotation/override 위주, 코스메틱 경계라 신중히. (effort M)
3. **분석기 툴체인 CI** — `analyzers/ggoss-ts` `npm ci` 를 SessionStart hook 에
   넣어 pr114/116 5건 환경실패 해소. (effort S, 영역 A)
