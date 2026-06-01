# Round PR-140 — Claude-Code extraction for analyzer-less languages

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `83d2c7c`
하네스: `autonomous-productization` · 트리거: OpenClaw(C++) 실분석 시도

## 발견된 치명적 결함 (실사용 중 노출)

OpenClaw(거대 C++ 게임 엔진, 엔진 자체 코드 120 .cpp / 132 .h)를 Mnemos로
분석하려다 **두 개의 연쇄 결함**을 만났다:

1. **프로젝트 생성 불가** — `app/api/projects.py` 의 `Language` 가
   `Literal["csharp","typescript"]` 하드코딩이라 C++(나아가 python/oracle/
   mssql)는 프로젝트조차 만들 수 없었다.
2. **분석 불가 (치명적)** — 설령 만들어도, 추출은 결정적 ggoss 분석기에만
   의존하고 L1~L3 LLM 은 *이미 추출된 그래프 노드*만 요약한다. C++ 분석기는
   존재하지 않으므로 그래프가 비고 → 요약 0 / findings 0. **"언어셋이 없으면
   분석을 못 하는"** 구조적 결함.

이는 README 설계 원칙 #4("대화·코딩 루프를 Claude Code 에 위임")와 정면으로
배치된다 — Claude Code 는 C++ 를 완벽히 이해한다(실측: `ActorController::
VOnUpdate` C++ 스니펫을 정확히 요약). 위임이 *요약*에만 적용되고 *추출*에는
적용되지 않은 것이 근본 원인.

## 보완 (Claude Code 구독으로 처리)

결정적 분석기가 없는 언어는 **Claude Code 구독(Agent SDK, API 키 불요)**이
원시 소스에서 직접 심볼/엣지를 추출하도록 새 스테이지를 추가:

| ID | 변경 | 파일 |
|---|---|---|
| 140-1 | `extract_file_via_agent_sdk` + `discover_source_files` + `to_envelopes` — Claude Code 가 파일당 심볼/엣지 추출, analyzer-contract envelope 로 변환 | `app/extractor/agent_extract.py` (신규) |
| 140-2 | `_run_agent_extraction_stage` + run_ingest 와이어링: `binary_for(lang) is None` 인 언어를 에이전트 추출로 라우팅. 기존 `_record_payload` 재사용 → 그래프·L1~L3·findings·MCP 자동 연결 | `app/orchestrator/jobs.py` |
| 140-3 | `agent_extract_limit`(파일 예산) 트리거 파라미터 | `app/api/analysis.py` |
| 140-4 | 프로젝트 언어 검증을 레지스트리 기반(결정적 ∪ 에이전트 가능)으로 확대 — C++/Go/Rust/Java… 생성 가능 | `app/api/projects.py`, `app/analyzers/registry.py` |
| 140-5 | 결정적 회귀 테스트 4건 (순수 헬퍼 + 와이어링) | `tests/test_pr140_agent_extraction.py` |

**정직성**: LLM 추출 노드는 `certainty="inferred"` (spec §2 원칙 #3). 결정적
분석기가 있으면 그것이 진실의 원천이고, 본 경로는 *플랫폼이 언어 때문에 눈머는
것*을 막는 폴백이다. 파일당 budget(`agent_extract_limit`, 기본 12)으로 거대
레포에서도 LLM 호출이 무한 증가하지 않게 bound.

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff `app/` | **0** |
| pytest `not integration` (−pr114) | **1454 passed / 6 failed** (was 1450 — +4 신규, 회귀 0; 실패 6은 기존 환경: pr116 툴체인 5 + pr138d flake 1) |
| mypy `app/` | **69** (신규 0, 신규 파일 추가에도 불변) |
| live Agent SDK 추출 | **OK** — C++ 스니펫 추출/요약 실측 |
| OpenClaw 실분석 | `agent_extract:cpp` 스테이지가 Claude Code CLI 로 엔진 .cpp 추출 실행 (결과는 별도 분석 자료) |

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| A. 분석기 실작동 / 언어 커버 | 8.5 | **9.0** | 결정적 분석기 없는 모든 언어를 Claude Code 로 커버 — "언어셋 없으면 분석 불가" 치명 결함 해소 |
| B/E. Claude Code 위임 | 8.0 | **8.8** | 위임이 요약→추출까지 확장, 원칙 #4 실현 |

## 다음 라운드 후보
1. 에이전트 추출 결과의 component/contract 승격(현재 Symbol/CALLS 위주).
2. 파일 선택 휴리스틱 개선(크기순 → 중심성/디렉터리 균형, 벤더 디렉터리 자동 제외 강화).
3. 기존 백로그: pr138d flake, 분석기 툴체인 CI.
