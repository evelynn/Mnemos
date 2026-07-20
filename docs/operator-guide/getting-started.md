# Mnemos — 운영자 시작 가이드 (PR-112)

이 문서는 새 운영자가 Mnemos의 source-index/MCP 가치를 확인하기 위한 단일
진입점입니다. 소요 시간은 대상 저장소 크기와 언어에 따라 달라집니다.

## 0. 한눈에

Mnemos는 **AI가 큰 소스를 직접 전부 읽지 않고도 분석할 수 있도록, 결정적
소스 인덱스와 근거가 붙은 작은 재조회 결과를 제공하는 보조 도구**입니다.
기본 분석은 LLM을 호출하지 않으며, AI는 그래프/MCP를 길잡이로 삼아 필요한
파일 범위만 마지막에 확인합니다.

- 기본 worker: Python, TypeScript/JavaScript, C/C++, Java, Kotlin, Web,
  tree-sitter 기반 소스 분석기
- C#, MSSQL/Oracle, .NET 바이너리 분석기는 저장소에 있으나 기본 Compose
  worker 실행 경로에는 아직 포함되지 않음
- 결과는 bitemporal 지식 그래프 (Node/Edge/Contract/DataEntity)
- Claude Code 같은 에이전트가 MCP 로 조회
- 모든 운영 기능은 GUI 에서 가능 (CLI 는 부팅 자가진단 + 데모만)

## 0.5. Docker 없이 1분 실행 (PR-135)

Docker 데몬·compose 없이 노트북에서 바로 띄울 수 있습니다. SQLite +
in-process fakeredis + inline 잡 + 로컬 analyzer 바이너리로 **단일
프로세스, 외부 서비스 0**:

```bash
cd server
pip install -e ".[local]"  # + aiosqlite, fakeredis
python -m app.serve_local --seed-demo
# Mnemos local mode (no Docker)
#   database : sqlite+aiosqlite:///./mnemos-local.db
#   redis    : in-process fakeredis
#   jobs     : inline (asyncio background tasks)
#   ============================================================
#     Login: demo-admin / Demo-xxxxxxxxxxxx1234
#   ============================================================
#   → http://127.0.0.1:16401
```

옵션: `--port 9000`, `--db /path/to.db`, `--reset` (깨끗한 슬레이트).
프로덕션 토폴로지(Postgres+Redis+worker)는 그대로 docker-compose
경로를 쓰면 됩니다 — local mode 분기는 전부 `is_local_mode()` 로
게이트되어 프로덕션에 영향 0.

로컬 모드는 in-repo 분석기를 사용합니다. Python/C/C++/Java/Kotlin/Web은
Python 인터프리터로 실행되고, TypeScript/JS는 `node`와
`analyzers/ggoss-ts/node_modules`가 필요합니다. tree-sitter 언어에는
`analyzers-treesitter` extra가 필요합니다. C#/.NET은 로컬 모드가 자동으로
준비하지 않습니다. 분석기 가용성은 실행 전 환경에서 확인해야 합니다.

## 1. 5분: 데모로 즉시 체험 (docker-compose 경로)

GitLab 프로젝트 등록 없이 모든 GUI 가 실제 데이터로 채워진 모습을
보고 싶다면:

```bash
docker compose up -d
docker compose exec platform python -m app.cli seed-demo
# 출력:
#   project_id: ...
#   nodes: 18  edges: 16  findings: 6  runs: 3
#   ============================================================
#     Login: demo-admin / demo-xxxxxxxxxxx
#     ↑ printed ONCE — save it before clearing the terminal.
#   ============================================================
```

위 숫자는 `seed-demo`가 넣는 **합성 fixture의 예시**일 뿐입니다. 실제 저장소
용량, 분석기 정확도, 처리량, 또는 운영 준비도를 나타내지 않으며 fixture 변경에
따라 달라질 수 있습니다.

그 다음 `http://localhost:16401` 접속 → 위 자격증명으로 로그인.

대시보드 첫 화면에 보일 것:
- **Triage now** 카드에 P1 findings 1개 (`duplicate_endpoint`)
- 좌측 stat 카드: Projects 1, Runs 3, Open findings 5
- "Latest analysis runs" 에 completed/failed/queued 3종
- /findings 탭: 6 항목 (P1~P4, open/acknowledged/resolved 혼합)
- /graph 탭: 18 노드 4종 (Component/Symbol/Contract/DataEntity)
- /health 탭: 5개 서브시스템 상태

데모 모드 종료/재시작 시 같은 명령으로 wipe + reseed. 운영 환경
(`MNEMOS_ENV=production`) 에서는 `--force` 없이는 거부됨.

## 2. 실제 GitLab 프로젝트 1개 분석

### 2.1 사전 준비 (한 번만)

```bash
# (a) 안전한 SECRET_KEY 생성 — placeholder 사용 시 PR-97 가드가 부팅 거부
echo "SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" >> .env

# (b) Fernet 키 생성 — KMS 가 secrets 를 암호화하는 데 씀
echo "FERNET_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env

# (c) 분석할 호스트 저장소를 worker의 /work에 read-only로 마운트
echo "MNEMOS_SOURCE_ROOT=/absolute/path/to/source-repo" >> .env

# (d) 기본 worker와 플랫폼 빌드/기동. runnable in-repo 분석기가 함께 복사됨
docker compose up -d --build

# (e) 부팅 자가진단으로 설정 확인
docker compose exec platform python -m app.cli verify
# database/redis/crypto와 등록한 언어의 analyzer availability를 확인:
#   ok    config: mnemos_env=production
#   ok    database
#   ok    redis
#   ok    crypto: round_trip
# 사용할 언어가 unavailable이면 먼저 worker 이미지/바이너리를 보완해야 함.
```

### 2.2 운영자 계정 + 첫 프로젝트

```bash
# admin 계정 생성 (비밀번호는 프롬프트)
docker compose exec platform python -m app.cli create-user \
  --username yourname --role admin

# 그 후 GUI 에서:
# 1. /login 로그인
# 2. /projects → "Register a project" — GitLab URL 입력
# 3. /analysis → source path=/work, Git SHA=HEAD(또는 존재하는 SHA)
# 4. 첫 인덱스는 narration 체크 해제, agent extract limit=0으로 실행
```

소요 시간은 KLOC만으로 예측하지 마십시오. 파일/바이트 수, 언어별 stage 시간,
생성된 graph row 수를 첫 실행의 기준선으로 기록하십시오.

### 2.3 첫 게시와 run 상태 확인

새 프로젝트와 graph-publication migration으로 올라온 기존 프로젝트는 신뢰할 수
있는 baseline이 없으므로 `GraphHead.state=needs_rebuild`에서 시작합니다. 기본
`incremental` 요청도 worker가 이 상태에서는 `full`로 정규화하지만, 업그레이드
복구 때는 의도를 명확히 하도록 직접 `scope=full`, `summarize=false`,
`agent_extract_limit=0`을 선택하십시오. 등록된 모든 필수 producer가 완전하고
authoritative하게 끝나야 head가 `ready`가 됩니다.

상태는 다음 의미로 읽습니다.

| 상태 | 보장 | 운영 판단 |
|---|---|---|
| `running` | analyzer 결과가 run-scoped staging에만 쌓임 | current graph는 이전 head 그대로 |
| `published` | 새 source generation과 atomic receipt가 커밋됨 | source 조회 가능; findings/summary는 아직 처리 중 |
| `completed` | 요청된 게시 후 runtime/findings/summaries 단계가 오류 없이 종료 | 해당 run에서 요청한 파생 결과 사용 가능 |
| `partial` | source receipt는 유지되지만 게시 후 단계 하나 이상 실패/취소 | source는 사용 가능; `stats.postprocess.errors` 확인 전 파생 결과를 완전하다고 보지 않음 |
| `failed` / `cancelled` | source 게시 전에 종료 | 이전 ready head는 유지되고 실패 run의 stage는 current가 아님 |

`published`나 `partial`을 source 손상으로 오해해 기존 graph를 지우지 마십시오.
`partial`이면 run의 `error_log`와 `stats.postprocess.errors[].stage`를 확인하고
provider/worker/해당 파생 stage 문제를 고친 뒤 새 run을 실행합니다. 업그레이드
순서와 ready-head 검증은 [배포 가이드의 Upgrades](deployment.md#10-upgrades)를
따릅니다.

### 2.4 Claude Code 에서 MCP 로 조회

먼저 로그인한 operator/admin 세션으로 **해당 프로젝트 전용** MCP 키를
발급합니다. 아래의 `MNEMOS_SESSION`과 `MNEMOS_CSRF`는 브라우저의
`mnemos_session`, `mnemos_csrf` 쿠키 값입니다. 응답의 `token`은 이 응답에서만
볼 수 있고 서버에는 SHA-256 digest만 저장되므로 즉시 안전한 곳에 옮깁니다.

```bash
export MNEMOS_MCP_TOKEN="$(curl -fsS \
  -X POST "$MNEMOS_URL/api/v1/projects/$PROJECT_ID/mcp-keys" \
  --cookie "mnemos_session=$MNEMOS_SESSION; mnemos_csrf=$MNEMOS_CSRF" \
  -H "X-CSRF-Token: $MNEMOS_CSRF" \
  -H "Content-Type: application/json" \
  --data '{"label":"Claude Code source analysis"}' | jq -r .token)"
```

키 목록은 같은 경로의 `GET`으로 확인할 수 있지만 raw token/hash는 반환하지
않습니다. 폐기는 `DELETE
/api/v1/projects/$PROJECT_ID/mcp-keys/$KEY_ID`이며 반복 호출해도 최초
`revoked_at`을 유지합니다. 폐기된 키는 이미 실행 중인 MCP 프로세스의 다음
도구 요청부터 거부되고 새 프로세스 시작에도 사용할 수 없습니다.

`~/.config/claude-code/mcp.json`:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/Mnemos/docker-compose.yml",
        "exec", "-T", "-e", "MNEMOS_MCP_TOKEN", "platform",
        "python", "-m", "app.mcp.server", "--project", "<project-id>"
      ],
      "env": {
        "MNEMOS_MCP_TOKEN": "<one-time raw token from the project MCP-key API>"
      }
    }
  }
}
```

Claude Code 에서:
- "search_symbols 로 createOrder 찾아줘"
- "find_callers 로 OrdersRepo 호출하는 곳 보여줘"
- "list_findings — open + P1"

각 MCP 도구의 "Use when:" 가이드는 도구 description 에 포함 (PR-105).
viewer 소유의 기존 키에는 조회 도구만 보이고, `submit_plan`, `edit_file_in_worktree`,
`run_in_sandbox`, `submit_diff`는 operator/admin 소유 키에서만 실행됩니다.

## 3. 운영 신뢰

### 3.1 부팅 자가진단

| 명령 | 언제 |
|------|------|
| `python -m app.cli verify` | 새 환경, 첫 배포, 키 로테이션 후 |
| `curl /api/v1/health/ready` | 모니터링 (15초 주기) |
| `/health` GUI 탭 | 운영자가 즉시 보고 싶을 때 |
| FastAPI startup 자동 verify (PR-110) | 매 부팅. 하드 fail 시 컨테이너 재시작 루프 |

### 3.2 알림

`MNEMOS_NOTIFY_WEBHOOK_URL` 설정 시 새 P1 finding 마다 Slack 호환
envelope 으로 POST (PR-104). 실패는 `mnemos_notify_failures_total`
Prometheus 카운터로 잡힘.

### 3.3 분석기 정확도 측정 (실측값 게시)

`scripts/accuracy/measure.py` 가 분석기를 fixture 에 돌려 precision/recall/F1
을 산출. 결과는 floor (precision ≥ 0.85, recall ≥ 0.80, f1 ≥ 0.82) 와 비교.

**2026-07-07까지의 소형 fixture 베이스라인** (회귀 가드:
`server/tests/test_pr11[4-7]*.py`,
`test_pr122*.py`):

| 분석기 | Fixture | Symbols (P/R/F1) | Edges (P/R/F1) | dogfood | 평가 |
|--------|---------|------------------|-----------------|---------|------|
| ggoss-ts v1.0.0 | sample-ts-project | **1.00 / 1.00 / 1.00** | **1.00 / 1.00 / 1.00** | ui.js 60 sym, 302 calls | ✅ floor pass |
| ggoss-py v1.0.0 | sample-py-project | **1.00 / 1.00 / 1.00** | **1.00 / 1.00 / 1.00** | server/app 545 sym, 3810 calls | ✅ floor pass |
| ggoss-csharp | (fixture 미작성) | — | — | — | 작성 + .NET 빌드 필요 |
| ggoss-sql-mssql | (fixture 미작성) | — | — | — | 작성 + .NET 빌드 필요 |
| ggoss-sql-oracle | (fixture 미작성) | — | — | — | 작성 + oracledb 필요 |
| ggoss-binary-dotnet | (fixture 미작성) | — | — | — | 작성 + .NET 빌드 필요 |

PR-114~117 의 자율 라운드에서:
- ggoss-ts: arrow-function callee 누락 버그 발견+수정 (recall 0.667 → 1.0)
- ggoss-py: 신규. receiver-prefix 해소 (`repo.add()` → `OrdersRepo.add`)
- **Dogfood end-to-end**: Mnemos 자체가 server/app/ (17K LOC) 분석 →
  in-memory SQLite (PR-118 polyglot) 에 적재 → `merge.findings.run_all`
  6 detector 모두 실행 → 실제 Finding row 생성. 운영자가 진짜로
  "Mnemos 가 작동한다" 를 코드 실행으로 입증 가능.

표에서 미측정인 분석기는 해당 실행 바이너리와 정답 fixture를 먼저 준비해야
측정할 수 있습니다. `--profile analyzers build`는 standalone contract-test
이미지만 만들며 기본 worker에 바이너리를 설치하지 않습니다.

```bash
# 자체 측정
docker compose exec platform python /app/scripts/accuracy/measure.py \
  --analyzer ts --fixture sample-ts-project

# 새 fixture 추가
mkdir scripts/accuracy/fixtures/my-codebase/{src,}
cp -r /path/to/repo/* scripts/accuracy/fixtures/my-codebase/src/
# 그 다음 expected.json 직접 작성 (symbol + edge 정답)

# CI gating
docker compose exec platform python /app/scripts/accuracy/measure.py \
  --all --strict  # exit 1 if any metric below floor
```

### 3.4 운영 시스템 보호 (§2.5)

- **break-glass**: 모든 diff 는 self-review 통과 필수. 차단된
  diff 를 강제 통과시키려면 admin 이 `/api/v1/diff_submissions/{sid}/break_glass_grant`
  로 토큰을 발급 → operator 가 그 토큰으로 승인 (two-eyes).
- **DB read-only probe**: 새 ProjectDB 등록 시 자동 실행. RW
  권한이 감지되면 412 로 거부.
- **worktree 격리**: 분석기 컨테이너는 read-only mount + tmpfs.
  운영 mirror 에는 쓸 수 없음.

## 4. 자주 막히는 곳

| 증상 | 원인 | 해결 |
|------|------|------|
| 부팅 즉시 die | placeholder SECRET_KEY (PR-97) | `.env` 에서 `secrets.token_urlsafe(48)` 로 교체 |
| 부팅 즉시 die | crypto round-trip fail | FERNET_KEY mis-rotation, PR-110 starup verify |
| /health/ready 503 | DB/Redis 부팅 중 | 15초 기다리고 새로고침 |
| `analyzer_binary_not_found` | worker에 해당 분석기/의존성 없음 | 기본 in-repo 지원 언어인지 확인하고 worker를 다시 빌드; C#/.NET/DB는 별도 PATH 통합 필요 |
| 분석 시작 안 됨 | webhook URL 또는 source mirror 미설정 | GitLab webhook을 설정하고 `SOURCE_MIRROR_ROOT` 아래 `<project UUID>[.git]` mirror에 push SHA를 동기화 |
| MCP 프로세스가 즉시 종료 | MCP token 누락/폐기/다른 프로젝트 키 또는 소유 사용자 비활성화 | 해당 프로젝트 MCP-key API에서 새 키를 1회 발급하고 설정의 `--project`와 함께 교체 |
| seed-demo 거부 | `MNEMOS_ENV=production` | `--force` 또는 staging 환경에서만 사용 |
| `graph_snapshot_unavailable` / `current_graph_snapshot_unavailable` | ready atomic head가 없거나 migration 뒤 `needs_rebuild` | 필수 analyzer를 준비하고 authoritative `scope=full`을 완료; upgrade 중이면 ingress를 열기 전에 ready head/receipt 확인 |
| `graph_snapshot_changed_retry` / `current_graph_snapshot_changed_retry` | 읽는 동안 source 또는 overlay generation이 바뀜 | 결과를 섞지 말고 같은 bounded 조회를 다시 실행 |
| run이 `published`에 오래 머묾 | source는 게시됐지만 post-processing worker가 끝나지 않음 | worker heartbeat와 stage/error를 확인; stale recovery가 receipt를 보존한 채 `partial`로 닫도록 두고 DB 상태를 수동 수정하지 않음 |
| run이 `partial` | findings, summary, runtime 단계 중 하나 이상 실패/취소 | source는 사용 가능; `stats.postprocess.errors`의 stage를 복구한 뒤 새 run 실행 |
| 최신 finding/summary가 비어 있음 | 저장된 파생 row가 현재 source/overlay revision과 불일치해 fail-closed로 숨겨짐 | ready head에서 finding rebuild 또는 `summarize=true` continuation을 실행; 이전 prose를 current로 강제 노출하지 않음 |

## 5. 더 깊이 가는 링크

- [아키텍처](../architecture.md)
- [분석 전략](../analysis-strategy.md)
- [분석기 컨트랙트](../analyzer-contract.md)
- [배포와 drain/migrate/rebuild](deployment.md#10-upgrades)
- [대규모 시스템 readiness](large_system_readiness.md)
- [Phase 1 체크리스트](phase1_checklist.md)
- [성능 리뷰](performance-review.md)
- [ultrareview 사용법](../ultrareview.md)

## 6. 도움 요청

- 문서 누락 / 잘못된 단계: GitHub Issues
- 보안 이슈: `SECURITY.md` 참고 — 공개 이슈 만들지 말고 비공개 보고
