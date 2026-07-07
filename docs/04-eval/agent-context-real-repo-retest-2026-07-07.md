# Mnemos AI Context Pack 실제 레포 재검증 — PR-191 커버리지 회복 후

> 재검증일: 2026-07-07 (선행 문서: `agent-context-real-repo-test-2026-07-07.md`)
> 대상 레포: `https://github.com/DeusData/codebase-memory-mcp`
> 로컬 경로: `E:\AI_work\analysis-targets\codebase-memory-mcp`
> 대상 SHA: `3d8d2c1935a75ed6095dcea3539cc88dd340e134`
> Mnemos 프로젝트 ID: `765d7f4c-f902-4d3a-b3c7-311641e8ccfd`
> 재분석 run ID: `1aaa1048-fd69-41a1-a50d-0fd461e49b23`

## 1. 결론

선행 평가가 지목한 4개 작업(P0 C/C++ analyzer, coverage report, 경로 정규화,
검색 강화)을 전부 구현하고, 같은 레포를 실제 파이프라인(`run_ingest`)으로
재분석한 뒤 **실제 MCP stdio 세션**으로 25개 검증 항목을 돌렸다. 결과:

- **25/25 PASS.** 이전에 "graph-ui/wrapper 부분 분석"이던 것이 이제 C 엔진
  본체를 포함한 전체 그래프다.
- 심볼 223 → **11,391** (51배), CALLS 엣지 1,266 → **57,491** (45배),
  DataEntity 0 → **13** (C 코드 안의 실제 SQLite 테이블).
- `symbols:cpp`는 더 이상 `no_analyzer`가 아니다 — 11,168개 심볼을 결정적
  (`asserted`) 추출했다.
- 그래프는 남은 한계(ruby 미분석, stub 요약)를 **스스로 보고한다**
  (`coverage_report.status = "partial"`).

## 2. 구현한 것 (선행 문서 §6의 Task 1–4)

### Task 1. `ggoss-cpp` — 결정적 C/C++ analyzer (P0)

새 파일: `analyzers/ggoss-cpp/src/ggoss_cpp.py` (순수 stdlib, 의존성 0)

- verbs: `probe / inventory / symbols / calls / contracts / data_access / schema`
  — 기존 analyzer 계약(`docs/analyzer-contract.md`) 그대로.
- 추출 방식: 주석/문자열/전처리기를 오프셋 보존 블랭킹 → brace-depth 스캐너로
  파일 스코프 판정(`extern "C"`/`namespace` 브레이스는 투명 처리) →
  function/struct/enum/union/class/함수형 macro 심볼 + CALLS 엣지.
  의도적으로 semantic 파서가 아니다(선행 문서의 MVP 제약 준수).
- **CALLS 해소 순서: 같은 파일(C static 링키지) → 프로젝트 전역 유일 이름 →
  `c:extern:<name>`.** 해소된 엣지는 `asserted`, extern은 `inferred`.
- `data_access`: 함수 본문 안 SQL 문자열 리터럴(인접 리터럴 연결 포함)에서
  READS/WRITES + DataEntity 추출 (ggoss-py와 같은 regex, 항상 `inferred`).
- `contracts`: **의도적으로 빈 출력.** C HTTP 라우팅은 문자열 비교 기반이라
  결정적 추출이 불가능하고, 추측 route는 verified/inferred 구분을 오염시킨다.
- `vendored/vendor/third_party` 트리는 기본 제외 (이번 레포에서 877개 파일 —
  전부 tree-sitter/sqlite 등 서드파티).
- 경로: 심볼 id와 `location.file` 모두 **프로젝트 상대 POSIX 경로**
  (`c:src/main.c:main@652`) — 선행 문서 P2의 경로 혼재 문제를 원천 차단.
- 등록: `analyzers/registry.py`(`cpp → ggoss-cpp`),
  `analyzers/runner.py`(`_INREPO_ANALYZERS`, docker-free 실행).
- 성능: 527파일 기준 symbols 10.5s / calls 8.4s / data_access 5.3s (계약 예산
  100k LOC ≤ 10min 대비 여유).

추가로 `javascript → ggoss-ts`를 등록했다(ggoss-ts는 이미 `.js/.jsx/.mjs/.cjs`
를 스캔). typescript와 javascript가 둘 다 등록된 프로젝트는 중복 실행 대신
`covered_by:typescript` 사유로 스테이지를 스킵 기록한다(`orchestrator/jobs.py`).

### Task 2. `coverage_report` (P1)

`build_project_index`(→ MCP `get_project_index`, HTTP artifact, AGENTS.md)에
신설:

- `status`: `complete | partial | insufficient`
- `requested_languages`, `language_stages`(스테이지별 status/skip 사유),
  `skipped`, `critical_gaps`(스킵됐고 agent 추출로도 커버 안 된 언어),
  `graph_coverage.symbols_by_top_dir`(경로 버킷), `summary_quality`
  (stub-only 경고), `recommendation`.
- `covered_by:*` 스킵은 gap이 아니다. agent_extract가 성공한 언어도 gap이
  아니다.
- AGENTS.md에도 "## Analysis Coverage" 섹션 + "partial이면 해당 언어에 대해
  추측하지 말고 말하라"는 계약 조항을 추가했다.

### Task 3. 경로 정규화 (P2)

- `mcp/queries.py`: `project_root_prefix()`(프로젝트 절대경로들의 공통 접두사
  추론, 캐시) + `relative_source_path()`.
- artifact 계층(`agent_context.py`)은 빌드 결과를 걸어 모든
  `location.file`에 `location.relative_file`을 추가한다. 절대경로는 보존.
- Windows `E:\...` / POSIX `E:/...` / 이미 상대인 경로(ggoss-cpp) 혼재에서
  전부 같은 상대경로로 수렴함을 테스트로 고정.

### Task 4. 검색 강화 (P2)

- `search_symbols`에 `scope=all|product|tests`, `path_prefix` 필터 추가.
- 결과에 `source_role`(product/test/vendored/generated/support)과
  `location{file, relative_file, line}` 추가 — 검색 결과에서 바로 좁은
  `read_file`로 갈 수 있다.
- MCP tool schema/설명 갱신(`mcp/server.py`).

### 기타 (선행 문서 P1 후속)

- **Contract exposer 정직성**: EXPOSES 엣지가 없는 contract는 project index에서
  `has_exposer: false` + `warning: "client_inferred_no_server_exposer"`,
  task pack에서 `server_exposer_missing: true`로 표시된다. 이번 레포의
  `POST /rpc` 등 8개 contract 전부가 TS fetch 리터럴에서만 추론된 것임이
  이제 명시된다 (C 서버 쪽 handler는 문자열 라우팅이라 결정적 연결 불가).

## 3. 재분석 결과 (전/후)

| 항목 | 이전 run (e6b0d03e) | 이번 run (1aaa1048) |
|---|---|---|
| symbols | 223 | **11,391** |
| CALLS edges | 1,266 | **57,491** |
| contracts | 8 | 8 |
| data_entities | 0 | **13** |
| errors | 0 | 0 |
| `symbols:cpp` | `no_analyzer` 스킵 | **completed, 11,168 records** |
| `calls:cpp` | 스킵 | **completed, 56,116 records** |
| `data_access:cpp` | 스킵 | **completed, 169 records** |
| `symbols:javascript` | `no_analyzer` 스킵 | `covered_by:typescript` 스킵 (정직 기록) |

그래프 경로 버킷(심볼 기준): `tests` 7,153 / `src` 2,267 / `internal` 1,618 /
`tools` 130 — C 엔진 본체(`src/`, `internal/cbm/`)가 이제 그래프에 있다.

## 4. MCP 실세션 검증 (25/25 PASS)

`python -m app.mcp.server --project …` stdio 세션으로 실행
(스크립트 요지: initialize → tools/list → 실제 tool 호출 → 응답 검증):

- **coverage**: `coverage_report.status == "partial"`, gap은 정확히 `{ruby}`
  (cpp는 더 이상 gap 아님, javascript는 covered_by), stub 요약 경고 존재.
- **hot symbols**: `cbm_free_result`, `cbm_store_close`, `cbm_node_text`,
  `cbm_language_for_extension` 등 C 코어 함수가 상위 — 이전에는 UI 훅이
  상위였다. 전부 `relative_file` 보유.
- **검색**: `"MCP server request tool call"` → 제품 코드 1위(테스트 헬퍼
  아님). `"install binary checksum"` → `extract_and_install_binary`,
  `verifyChecksum` 상위 (선행 문서 Task 4의 수용 기준 그대로).
  `scope=product&path_prefix=src` → `src/ui/http_server.c:cbm_http_server` 등
  C 서버 코드만.
- **task pack (C 심볼)**: `gb_intern` pack — callers 4 / callees 3 /
  impact / relative_file / raw_source_included=false / 44KB (경계 내).
- **entrypoint**: `c:src/main.c:main@652` 검색 가능, callees 34개가 C 모듈로
  해소(`cbm_alloc_init`, `cbm_profile_init`, `cbm_cli_set_version` …).
- **data**: C 코드에서 추출된 SQLite 테이블 DataEntity (`data:nodes`,
  `data:nodes_fts` 등) 조회 가능.

### Ground-truth 스팟 체크

그래프가 보고한 `gb_intern`의 호출자 4개
(`cbm_gbuf_insert_edge`, `cbm_gbuf_upsert_node`, `merge_copy_new_node`,
`merge_update_existing`)를 원본 소스 grep과 대조 — 실제 9개 호출 지점을
감싸는 함수 집합과 **정확히 일치** (이 표본에서 정밀도/재현율 100%).

## 5. 테스트 / 게이트

- 신규: `tests/test_pr191_cpp_analyzer.py` (6 tests — 심볼 종류, 프로토타입
  제외, static 우선 해소, extern 정직성, SQL 리터럴, vendored 제외, registry).
- 확장: `tests/test_pr190_agent_context_artifacts.py` (+4 — coverage gap,
  relative_file 혼재 경로, contract exposer 플래그, scope/source_role).
- 전제 갱신(“cpp에는 analyzer가 없다”가 참이 아니게 됨): `test_pr140`·
  `test_pr153`(fallback 예시 언어를 ruby로), `test_pr35`(registry 8개 언어),
  `test_pr100`(compose 7번째 analyzer 이미지), `test_pr80`/`test_pr95`
  (search_symbols가 길어져 소스-슬랩 윈도 확대), `test_pr117`(_FakeSession
  컬럼 셀렉트 지원).
- 관련 스위트 76 passed, ruff clean.
- 전체 스위트: 1,624 passed / 27 failed / 15 errors. 실패·에러를 clean HEAD
  워크트리에서 재실행해 전수 분류 — 내 변경 기인 5건(pr140/pr35-registry/
  pr80/pr95/pr100)은 전부 위 전제 갱신으로 해소했고, **나머지는 HEAD에서도
  동일하게 실패하는 기존 결함**(Windows 서브프로세스/유니코드 아티팩트,
  낡은 WCAG 기준, 라이브 서버가 필요한 integration 테스트)이다. 신규 회귀 0.

## 6. 남은 정직한 한계 (다음 작업 후보)

1. **ruby 미분석** — analyzer 없음 + local mode에서 agent SDK 비활성.
   coverage_report가 critical gap으로 보고한다 (파일 1개라 실익 낮음).
2. **요약이 stub** — LLM 백엔드 없는 환경. `summary_quality.warning`으로
   표시된다. LLM 붙이면 L1–L3이 실서사가 된다.
3. **C contracts 빈 출력** — `POST /rpc`의 서버측 handler(C의 문자열 라우팅)는
   결정적으로 연결 불가. `server_exposer_missing=true`로 정직하게 표시.
   (원하면 별도 heuristic PR로, 반드시 `inferred`로만.)
4. **ggoss-cpp 한계(문서화됨)**: 전처리기 미평가(#ifdef 양쪽 다 보임),
   함수 포인터/멤버 호출 미해소, K&R 스타일 미지원, 매크로 생성 함수 불가시.
5. **tests 심볼이 7,153개로 최다 버킷** — 검색 penalty와 scope=product로
   완화되지만, hot symbol 목적별 리스트(entrypoints/api_clients/...)는
   여전히 P2 후보다.

## 7. 판정

선행 문서의 요구 — "이 네 가지가 끝나야 Mnemos를 '대규모 솔루션을 AI가
지속적으로 조회하는 분석 지식베이스'라고 주장할 수 있다" — 를 기준으로:

- Task 1 (C/C++ analyzer): **완료, 수용 기준 전부 충족**
- Task 2 (coverage_report): **완료** (partial/critical gap 정확)
- Task 3 (relative_file): **완료** (혼재 경로 수렴 테스트 고정)
- Task 4 (검색 강화): **완료** (두 수용 검색 쿼리 모두 통과)

C/C++ 본체 레포에 대해 project_index → search(scope/path_prefix) →
task_context_pack → impact/callers 흐름이 실제 MCP 세션에서 동작하고, 그래프가
자기 커버리지 한계를 스스로 보고한다. 이번 대상 레포 기준으로 "AI가 재조회하는
분석 지식베이스" 주장이 성립한다.
