# Mnemos AI Context Pack 실제 레포 검증 및 Claude Code 작업 지시

> 평가일: 2026-07-07  
> 대상 레포: `https://github.com/DeusData/codebase-memory-mcp`  
> 로컬 경로: `E:\AI_work\analysis-targets\codebase-memory-mcp`  
> 대상 SHA: `3d8d2c1935a75ed6095dcea3539cc88dd340e134`  
> Mnemos 프로젝트 ID: `765d7f4c-f902-4d3a-b3c7-311641e8ccfd`  
> Analysis run ID: `e6b0d03e-0fd9-431f-95ba-9e3b992433dd`

## 1. 결론

Mnemos의 "AI가 재조회하는 분석 지식베이스 + 작업별 소형 context pack" 방향은 실제 MCP/API 경로에서 동작한다. 다만 이번 대상 레포는 C/C++가 본체인 프로젝트라서, 현재 Mnemos 로컬 환경의 분석 결과는 전체 솔루션 분석이 아니라 `graph-ui`, package wrapper, Python helper 중심의 부분 분석이다.

Claude Code에게 시킬 다음 핵심 작업은 명확하다.

1. C/C++ 결정적 analyzer를 붙인다.
2. analyzer coverage/reporting을 project index에 명시한다.
3. context pack과 MCP 검색을 "AI 작업 도구"로 쓰기 위한 랭킹/출력 안전성을 계속 강화한다.

## 2. 이번에 수정한 점

### 2.1 AI용 artifact 신설

새 파일:

- `server/app/artifacts/agent_context.py`
- `server/tests/test_pr190_agent_context_artifacts.py`

추가된 스키마:

- `mnemos.agent.project_index.v1`
- `mnemos.agent.task_context_pack.v1`

핵심 함수:

- `build_project_index(...)`
- `build_task_context_pack(...)`

의도:

- 대형 레포 전체를 LLM prompt에 넣지 않는다.
- Mnemos graph DB를 source of truth로 둔다.
- Claude Code/Codex는 `project_index -> search -> task_context_pack -> targeted MCP query -> narrow read_file` 흐름으로 작업한다.
- raw source는 context pack에 넣지 않는다.

### 2.2 HTTP artifact API 추가

수정 파일:

- `server/app/api/artifacts.py`
- `server/app/artifacts/__init__.py`

추가 endpoint:

- `GET /api/v1/projects/{project_id}/artifacts/project-index.json`
- `GET /api/v1/projects/{project_id}/artifacts/task-context-pack.json?target_id=...`

기존 artifact list에도 다음 항목을 추가했다.

- `mnemos-project-index.json`
- `mnemos-task-context-pack.json`

### 2.3 MCP tool 추가

수정 파일:

- `server/app/mcp/server.py`
- `server/tests/test_pr105_mcp_tool_descriptions.py`

추가 tool:

- `get_project_index`
- `get_task_context_pack`

또한 local SQLite MCP 경로에서 UUID/JSON 비교가 실패해 `project_not_found`가 나는 실제 버그를 수정했다. `MNEMOS_LOCAL_MODE=1` 또는 SQLite `DATABASE_URL`이면 MCP 서버 import 초기에 `sqlite_polyglot.install_polyglot()`을 실행한다.

### 2.4 AGENTS.md를 사람용 보고서에서 agent contract로 전환

수정 파일:

- `server/app/artifacts/agents_md.py`

변경 내용:

- "프로젝트 설명 문서"가 아니라 "Claude Code/Codex가 Mnemos를 어떻게 써야 하는지"를 적는 agent contract로 바꿨다.
- `get_project_index`, `get_task_context_pack`, `impact_analysis`, `get_data_access`, `get_contract` 사용 순서를 명시했다.
- inferred/verified 구분을 보존하도록 지시한다.

### 2.5 기존 MCP 분석 기능을 context pack 내부에서 선사용

수정 파일:

- `server/app/artifacts/agent_context.py`

`task_context_pack`에 `precomputed_mcp_context`를 추가했다.

Symbol target에서는 내부적으로 다음 MCP query helper를 먼저 사용한다.

- `get_symbol`
- transitive `find_callers`
- transitive `find_callees`
- `get_data_access`
- intent 기반 `search_symbols`

Contract target에서는 다음을 선사용한다.

- `get_contract`
- `find_runtime_path`

DataEntity target에서는 다음을 선사용한다.

- `get_data_entity`

이로써 pack 하나만 받아도 AI가 바로 작업 계획을 세울 수 있고, 더 필요하면 `next_mcp_queries`를 따라 재조회할 수 있다.

### 2.6 raw source 및 큰 payload 누출 방지

수정 파일:

- `server/app/artifacts/agent_context.py`
- `server/app/mcp/queries.py`
- `server/tests/test_pr190_agent_context_artifacts.py`

실제 레포 분석 중 TS analyzer가 `signature`에 함수 본문 일부를 넣는 경우가 발견됐다. 그래서 출력 계층에서 다음을 방어한다.

- `signature`는 첫 줄만 노출한다.
- `excerpt`도 첫 줄만 노출한다.
- `content`, `raw`, `snippet`, `body`, `payload`, `source_text`, `file_text` 등 raw payload 키는 생략한다.
- 큰 문자열은 `{omitted: "large_string", chars: N}`로 대체한다.
- nested data도 depth/list/key 수를 제한한다.

### 2.7 검색 랭킹 보정

수정 파일:

- `server/app/mcp/queries.py`
- `server/app/artifacts/agent_context.py`

실제 MCP 검색에서 `tests/windows/mcp_stdio.py`가 제품 코드보다 먼저 나오는 문제가 있었다.

수정:

- `tests`, `test`, `__tests__`, `vendored`, `vendor`, `third_party`, `node_modules`, `build`, `dist`, `coverage` 경로에 검색 score penalty 적용
- `tools`, `scripts`, `fixtures`, `examples`, `docs` 경로에 약한 penalty 적용
- project index hot symbols도 path rank를 고려해 제품 코드가 먼저 나오도록 정렬

## 3. 실제 외부 레포 분석 결과

### 3.1 레포 구성

대상 레포 파일 수:

- 전체: 1,775 files
- `.c`: 763
- `.h`: 640
- `.tsx`: 35
- `.ts`: 13
- `.py`: 17
- `.js`: 4
- `.rb`: 1

주요 디렉터리 파일 수:

- `internal`: 1,209
- `src`: 128
- `vendored`: 89
- `tests`: 168
- `graph-ui`: 55
- `pkg`: 23
- `tools`: 23

즉 이 레포의 본체는 C/C++이다.

### 3.2 Analysis run 결과

Run:

- ID: `e6b0d03e-0fd9-431f-95ba-9e3b992433dd`
- status: `completed`
- started: `2026-07-07T09:47:22.884013`
- completed: `2026-07-07T09:47:56.744398`

Stats:

```json
{
  "symbols": 223,
  "edges": 1266,
  "contracts": 8,
  "data_entities": 0,
  "errors": 0,
  "findings": 0,
  "l1_summaries": 60,
  "l2_summaries": 16,
  "l3_summaries": 2
}
```

Current graph inventory:

```json
{
  "nodes": {
    "Contract": 8,
    "Symbol": 223
  },
  "edges": {
    "CALLS": 1266
  },
  "summaries": 78
}
```

Certainty:

```json
{
  "nodes": {
    "asserted": 223,
    "inferred": 8
  },
  "edges": {
    "asserted": 203,
    "inferred": 1063
  }
}
```

Created by:

- `ggoss-ts`: 140 nodes
- `ggoss-py`: 91 nodes

Summary backend:

- all summaries are `model_used="stub"`, `fallback_reason="no_backend"`

### 3.3 Stage 결과

성공적으로 추출된 단계:

- `symbols:typescript`: 146 symbols
- `contracts:typescript`: 9 contract/edge records
- `calls:typescript`: 887 accumulated edges
- `symbols:python`: accumulated 237 symbols before final distinct count
- `calls:python`: accumulated 1838 edges before final distinct count
- `l1_summaries`: 60
- `l2_summaries`: 16
- `l3_summaries`: 2

스킵된 단계:

- `symbols:cpp`: `no_analyzer`
- `contracts:cpp`: `no_analyzer`
- `calls:cpp`: `no_analyzer`
- `data_access:cpp`: `no_analyzer`
- `symbols:javascript`: `no_analyzer`
- `contracts:javascript`: `no_analyzer`
- `calls:javascript`: `no_analyzer`
- `data_access:javascript`: `no_analyzer`
- `symbols:ruby`: `no_analyzer`
- `contracts:ruby`: `no_analyzer`
- `calls:ruby`: `no_analyzer`
- `data_access:ruby`: `no_analyzer`
- `agent_extract:cpp`: `agent_sdk_unavailable`
- `agent_extract:javascript`: `agent_sdk_unavailable`
- `agent_extract:ruby`: `agent_sdk_unavailable`

중요: C/C++가 본체인 레포인데 C/C++가 전부 빠졌다. 따라서 이번 결과를 "codebase-memory-mcp 전체 분석"으로 말하면 안 된다.

### 3.4 실제 graph가 잡은 주요 영역

Symbol path buckets:

- `graph-ui/src/components`: 92
- `graph-ui/src/lib`: 18
- `tests/windows/mcp_stdio.py`: 17
- `scripts/gen-py-stdlib.py`: 15
- `pkg/pypi/src`: 11
- `scripts/extract_nomic_vectors.py`: 10
- `pkg/npm/install.js`: 7
- `graph-ui/src/hooks`: 6
- `graph-ui/src/api`: 2

분석된 제품성 높은 영역:

- `graph-ui/src/api/rpc.ts`
- `graph-ui/src/components/*`
- `graph-ui/src/lib/*`
- `graph-ui/src/hooks/*`
- `pkg/npm/install.js`
- `pkg/pypi/src/codebase_memory_mcp/_cli.py`

거의 분석되지 않은 핵심 영역:

- `src/*.c`
- `src/*.h`
- `internal/**/*.c`
- `internal/**/*.h`
- `vendored/**/*.c`
- `vendored/**/*.h`

## 4. AI 도구로서 실제 사용성 확인

### 4.1 Project index

`GET /api/v1/projects/{project_id}/artifacts/project-index.json?top_k=10`

결과:

- schema: `mnemos.agent.project_index.v1`
- index size: 약 10KB
- hot symbols는 보정 후 `graph-ui/src`와 `pkg/pypi/src` 중심으로 정렬됨
- 예: `useUiMessages`, `callTool`, `_bin_path`, `_validate_url_scheme`, `computeCameraTarget`

### 4.2 MCP search

질의: `MCP server request tool call`

보정 전:

- `tests/windows/mcp_stdio.py:McpServer.call_tool`이 1위

보정 후:

```json
[
  {
    "symbol_id": "ts:rpc.ts:callTool@15:1",
    "name": "callTool",
    "component_id": "svc.codebase-memory-mcp",
    "kind": "Symbol",
    "certainty": "asserted",
    "score": 17.5,
    "excerpt": "export async function callTool<T = unknown>("
  }
]
```

이제 제품 코드가 먼저 나온다.

### 4.3 Task context pack

Target:

- `ts:rpc.ts:callTool@15:1`

Pack 결과:

- schema: `mnemos.agent.task_context_pack.v1`
- pack size: 약 14.8KB
- `raw_source_included`: `false`
- target file: `graph-ui/src/api/rpc.ts`
- target line: 15
- callers: 3
- callees: 4
- evidence refs: 8
- next MCP queries:
  - `get_symbol`
  - `find_callers`
  - `find_callees`
  - `get_data_access`
  - `impact_analysis`

Impact:

```json
{
  "directly_affected": [
    "ts:NodeDetailPanel.tsx:loadCode@71:20",
    "ts:useProjects.ts:fetchProjects@22:37",
    "ts:useProjects.ts:infos@31:18"
  ],
  "transitively_affected": [],
  "affected_tests": [],
  "affected_data_entities": [],
  "opaque_components_touched": [],
  "runtime_exercised": false
}
```

Contract:

```json
{
  "contract": {
    "id": "http.POST./rpc",
    "kind": "http_endpoint",
    "name": "POST /rpc",
    "spec": {
      "method": "POST",
      "path": "/rpc"
    },
    "certainty": "inferred"
  },
  "exposers": [],
  "callers": [
    "ts:rpc.ts:<caller>@19:21"
  ],
  "runtime_stats": null
}
```

평가:

- AI가 `graph-ui`/wrapper 작업을 하기 위한 context pack으로는 사용 가능하다.
- 전체 C 엔진 변경 작업에는 사용할 수 없다. 핵심 소스가 graph에 없다.

## 5. 부족한 점

### P0. C/C++ 분석 부재

이번 레포는 C/C++ 파일이 압도적으로 많다.

- `.c`: 763
- `.h`: 640
- `src`: 128 files
- `internal`: 1,209 files

하지만 C/C++ stage는 전부 `no_analyzer`로 스킵됐다.

현 상태로는 다음 질문에 답할 수 없다.

- MCP 서버의 실제 C entrypoint는 어디인가?
- CLI command dispatch는 어떤 C 함수들이 담당하는가?
- index build pipeline의 C call graph는 무엇인가?
- tree-sitter/language registry는 어디서 초기화되는가?
- mmap/sqlite/LZ4/compression 경로의 영향 범위는 무엇인가?
- C 레벨 dead code / dynamic call / data flow는 무엇인가?

Claude Code 작업:

- `ggoss-cpp` 또는 tree-sitter 기반 C/C++ analyzer를 추가하라.
- 최소 record types:
  - `symbol`
  - `edge` with `CALLS`
  - optionally `contract`
  - optionally `data_entity` for SQLite/schema-like artifacts
- 처음부터 완전 semantic resolver를 만들지 말고, 1차는 function/struct/enum/macro/function-call 정도를 deterministic extraction한다.
- `src/`와 `internal/`을 우선 분석하고 `vendored/`는 기본 제외 또는 낮은 priority로 둔다.

### P1. Analyzer coverage가 artifact에 명시되지 않음

현재 project index는 node/edge counts는 보여주지만, "어떤 언어/경로가 분석되지 않았는지"를 명확히 말하지 않는다.

이번 분석에서 가장 중요한 사실은 `cpp no_analyzer`인데, AI가 project index만 보면 이 한계를 강하게 인지하기 어렵다.

Claude Code 작업:

- `build_project_index`에 `coverage_report` 섹션 추가
- 포함할 정보:
  - requested project languages
  - stages by language
  - skipped stages with reason
  - source file extension counts if available
  - graph coverage by path bucket
  - warning list
- 예:

```json
{
  "coverage_report": {
    "status": "partial",
    "critical_gaps": [
      {
        "language": "cpp",
        "reason": "no_analyzer",
        "impact": "dominant repository language not represented in graph"
      }
    ]
  }
}
```

### P1. JS/Ruby fallback도 local mode에서는 사실상 동작하지 않음

`javascript`, `ruby`는 deterministic analyzer가 없고 agent extraction도 `agent_sdk_unavailable`로 스킵됐다.

Claude Code 작업:

- local mode에서 agent extraction 불가 시 명확한 artifact warning을 만든다.
- JS는 TS analyzer로 같이 처리할 수 있는지 확인한다. 현재 `ggoss-ts`가 `.js`를 충분히 볼 수 있다면 registry를 정리한다.

### P1. LLM summary가 stub임

모든 summary가 다음 형태다.

- `model_used="stub"`
- `fallback_reason="no_backend"`

이는 좋게 말하면 정직한 fallback이지만, AI가 고수준 시스템 이해를 얻기엔 부족하다.

Claude Code 작업:

- project index와 task pack에서 `summary_quality`를 명시한다.
- `stub/no_backend`이면 AI에게 "summary is structural placeholder only"라고 경고한다.
- LLM이 없는 환경에서도 deterministic summary를 조금 더 유용하게 만들 수 있는지 검토한다.

### P1. Contract inference가 client-side fetch 중심임

`POST /rpc`, `/api/index`, `/api/logs` 등은 TS fetch literal에서 추론됐다.

문제:

- exposer가 없음
- server implementation은 C 쪽에 있을 가능성이 높지만 C graph가 없어서 연결되지 않음

Claude Code 작업:

- C/C++ analyzer에서 HTTP route/server handler 패턴을 뽑을 수 있으면 contract EXPOSES edge를 생성한다.
- 아니면 최소한 `contract_source=client_fetch_literal`과 `server_exposer_missing=true`를 artifact에 명시한다.

### P2. Hot symbol ranking은 개선됐지만 아직 목적별 ranking이 필요함

현재 개선 후 `tests/tools/vendored` penalty는 들어갔다. 그러나 hot symbol은 여전히 "incoming_calls" 중심이다.

Claude Code 작업:

- project index에 한 종류의 hot list만 두지 말고 목적별 list를 둔다.
  - `entrypoints`
  - `api_clients`
  - `ui_components`
  - `package_installers`
  - `high_indegree_symbols`
  - `recent_findings_subjects`
- 각 list는 path rank와 node kind를 같이 사용한다.

### P2. Path/source metadata 정규화 필요

TS analyzer는 `/` 경로, Python analyzer는 Windows `\` 절대경로가 섞인다.

Claude Code 작업:

- artifact 출력에서 `location.file`은 가능하면 project-relative path로 추가 노출한다.
- 기존 absolute path는 유지하되 `relative_file` 필드를 추가한다.

예:

```json
{
  "location": {
    "file": "E:/AI_work/analysis-targets/codebase-memory-mcp/graph-ui/src/api/rpc.ts",
    "relative_file": "graph-ui/src/api/rpc.ts",
    "line": 15
  }
}
```

## 6. Claude Code에게 줄 구현 지시

아래 순서로 작업하라.

### Task 1. C/C++ analyzer MVP

목표:

- `codebase-memory-mcp`의 `src/`와 `internal/`에서 C function/struct/enum/call graph를 graph에 넣는다.

제약:

- 처음부터 완벽한 C semantic analysis를 하지 않는다.
- deterministic extraction 우선.
- vendored는 기본 제외하거나 낮은 priority.
- output은 analyzer contract JSONL을 따른다.

검증 기준:

- 같은 레포 재분석 후 `cpp` stage가 `no_analyzer`가 아니어야 한다.
- `src/main.c`, `src/cli/cli.c`, `src/ui/http_server.c`, `src/graph_buffer/graph_buffer.c` 같은 파일의 함수가 Symbol로 잡혀야 한다.
- C CALLS edge가 생성되어야 한다.
- `project-index.json` hot symbols에 C core 함수가 나타나야 한다.

### Task 2. Coverage report artifact

목표:

- AI가 "이 분석을 믿어도 되는지" 판단할 수 있게 한다.

구현 위치:

- `server/app/artifacts/agent_context.py`

출력:

- `coverage_report.status`: `complete | partial | insufficient`
- `coverage_report.language_stages`
- `coverage_report.skipped`
- `coverage_report.critical_gaps`
- `coverage_report.recommendation`

검증 기준:

- 현재 run에서는 `partial`이어야 한다.
- `cpp no_analyzer`가 critical gap으로 표시되어야 한다.

### Task 3. Context pack location normalization

목표:

- AI가 `read_file`을 좁게 호출할 수 있게 project-relative path를 안정적으로 제공한다.

구현 위치:

- `server/app/artifacts/agent_context.py`
- optionally `server/app/mcp/queries.py`

검증 기준:

- `target_node.location.relative_file`이 존재한다.
- Windows absolute path와 POSIX-style path가 섞여도 relative path가 안정적이다.

### Task 4. MCP search/product ranking hardening

목표:

- `tests`, `tools`, `vendored` helper가 제품 코드보다 먼저 나오는 문제를 계속 줄인다.

이미 들어간 것:

- path penalty
- signature/excerpt first-line truncation

추가할 것:

- optional `scope=product|tests|all`
- optional `path_prefix`
- result에 `path_rank_reason` 또는 `source_role` 추가

검증 기준:

- `search_symbols("MCP server request tool call")`는 `graph-ui/src/api/rpc.ts:callTool`을 테스트 helper보다 먼저 반환한다.
- `search_symbols("install binary checksum")`는 `pkg/npm/install.js:verifyChecksum`과 `pkg/pypi/_cli.py:_verify_checksum`을 상위에 둔다.

## 7. 현재 검증 커맨드

관련 테스트:

```powershell
cd E:\AI_work\Mnemos\server
..\.venv\Scripts\python.exe -m pytest `
  tests/test_pr190_agent_context_artifacts.py `
  tests/test_pr105_mcp_tool_descriptions.py `
  tests/test_pr132_openapi_integrity.py `
  tests/test_pr117_mcp_dogfood.py `
  tests/test_pr118_real_mcp_session.py `
  -q
```

현재 결과:

```text
39 passed
```

Lint:

```powershell
cd E:\AI_work\Mnemos\server
..\.venv\Scripts\python.exe -m ruff check `
  app\artifacts\agent_context.py `
  app\mcp\queries.py `
  app\artifacts\agents_md.py `
  app\artifacts\__init__.py `
  app\api\artifacts.py `
  app\mcp\server.py `
  tests\test_pr190_agent_context_artifacts.py `
  tests\test_pr105_mcp_tool_descriptions.py
```

현재 결과:

```text
All checks passed!
```

## 8. 최종 판정

Mnemos를 AI의 도구로 쓰는 구조는 맞다. 실제 MCP stdio와 HTTP API에서 동작했고, `get_project_index -> search_symbols -> get_task_context_pack -> next_mcp_queries` 흐름도 확인됐다.

하지만 현재 상태로는 C/C++ 대형 엔터프라이즈 솔루션에 대해 "충분히 분석했다"고 말할 수 없다. 이번 테스트 레포의 핵심은 C/C++인데, Mnemos가 실제로 분석한 것은 TypeScript UI와 Python/package wrapper 중심이다.

따라서 다음 Claude Code 작업은 기능 추가가 아니라 분석 커버리지 회복이다.

우선순위:

1. C/C++ analyzer MVP
2. coverage_report artifact
3. project-relative location normalization
4. search/hot-symbol ranking hardening

이 네 가지가 끝나야 Mnemos를 "대규모 솔루션을 AI가 지속적으로 조회하는 분석 지식베이스"라고 주장할 수 있다.
