# Round PR-153 — docker-free deterministic Python analysis (in-repo ggoss-py)

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `bcfaf5e`
트리거: `/loop` — "기본 구동은 docker가 아니라 기본 구성으로 완성"; 정직 감사가 지목한
A(분석기 실작동) 향상

## 문제 (냉정한 평가)

정직 감사(score-audit-honest.md): docker-free 에서 결정적 분석기가 *전혀* 안 돌고
모든 추출이 Claude `inferred` 폴백이었다 — A 영역의 핵심 약점. 그러나 ggoss-py 는
**순수 stdlib(`ast`)** 라 docker 없이도 source 에서 바로 실행 가능한데, 러너는
`shutil.which("ggoss-py")` 가 None 이면 그냥 폴백해 버렸다.

## 보완

기본(비-docker) 구성에서 in-repo ggoss-py 를 직접 실행 → Python 이 `inferred` 가
아닌 **결정적(`asserted`)** 추출(심볼·계약·호출·데이터접근)을 받는다.

| ID | 변경 | 파일 |
|---|---|---|
| 153-1 | `inrepo_script(binary)` — 플래그(`MNEMOS_INREPO_ANALYZERS`) ON + 스크립트 존재 시 in-repo Python 엔트리포인트 반환 | `app/analyzers/runner.py` |
| 153-2 | `run()` 이 binary 미설치 시 `python <script> <verb> <path>` 로 폴백 | `app/analyzers/runner.py` |
| 153-3 | `analyzer_available()` 가 in-repo 스크립트도 가용으로 인정 | `app/analyzers/registry.py` |
| 153-4 | `serve_local` 이 `MNEMOS_INREPO_ANALYZERS=1` 기본 설정 | `app/serve_local.py` |
| 153-5 | `.env.example` 문서화 + 회귀 테스트 3건 | `.env.example`, `tests/test_pr153_inrepo_analyzer.py` |

**안전 게이팅**: 플래그 OFF(테스트 기본)면 동작 불변 → 스위트 안정. 프로덕션은
PATH 바이너리(docker 이미지) 우선이라 무영향. serve_local(기본 구성)만 ON.

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff | **0** |
| PR-153 단위 | **3 passed** (플래그 게이팅·가용성·serve_local) |
| pytest `not integration` (−pr114) | **1481 passed / 6 failed / 32 skipped** (회귀 0) |
| mypy | **69** (불변) |
| live | serve_local 가 Python 프로젝트 분석 시 **`spawning analyzer: ggoss-py symbols/contracts/calls/data_access`** 실행 → `handle_create_order` 심볼 `certainty=asserted`(결정적, inferred 아님) + orders 테이블/엣지 발견 |

### 회귀 중 잡은 자기-결함
신규 env var `MNEMOS_INREPO_ANALYZERS` 가 `.env.example` 미문서화 →
`test_pr131_env_example_completeness` 실패. 게이트가 정확히 잡음 → 문서 추가로 해소
(deselect 아님).

## 영역 점수 갱신 (정직 기준선 위에서)
| 영역 | before | after | 근거 |
|---|---:|---:|---|
| A 분석기 실작동 | 8.5 | **8.8** | docker-free 에서 결정적 Python 추출(asserted) 실가동 — inferred 의존 탈피 |
| C 운영검증(배포) | 8.3 | **8.5** | 기본 구성이 실 분석기로 더 완성 |
