# Mnemos — 운영자 시작 가이드 (PR-112)

이 문서는 새 운영자가 Mnemos 를 **30분 안에** 가치까지 도달하기 위한
단일 진입점입니다. 깊이 있는 주제는 각 섹션 끝의 링크로 분기합니다.

## 0. 한눈에

Mnemos 는 **복합 언어 · 복합 DB 시스템을 지속적으로 분석 · 축적하여,
그 지식 자산으로 개발 · 질의응답 · 데이터 조회 요청을 상시 처리하는
자체 호스팅 플랫폼**입니다.

- 분석기 5종 (C#, TypeScript, MSSQL, Oracle, .NET 바이너리)
- 결과는 bitemporal 지식 그래프 (Node/Edge/Contract/DataEntity)
- Claude Code 같은 에이전트가 MCP 로 조회
- 모든 운영 기능은 GUI 에서 가능 (CLI 는 부팅 자가진단 + 데모만)

## 1. 5분: 데모로 즉시 체험

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

그 다음 `http://localhost:8080` 접속 → 위 자격증명으로 로그인.

대시보드 첫 화면에 보일 것:
- **Triage now** 카드에 P1 findings 1개 (`duplicate_endpoint`)
- 좌측 stat 카드: Projects 1, Runs 3, Open findings 5
- "Latest analysis runs" 에 completed/failed/running 3종
- /findings 탭: 6 항목 (P1~P4, open/acknowledged/resolved 혼합)
- /graph 탭: 18 노드 4종 (Component/Symbol/Contract/DataEntity)
- /health 탭: 5개 서브시스템 상태

데모 모드 종료/재시작 시 같은 명령으로 wipe + reseed. 운영 환경
(`MNEMOS_ENV=production`) 에서는 `--force` 없이는 거부됨.

## 2. 30분: 실제 GitLab 프로젝트 1개 분석

### 2.1 사전 준비 (한 번만)

```bash
# (a) 안전한 SECRET_KEY 생성 — placeholder 사용 시 PR-97 가드가 부팅 거부
echo "SECRET_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(48))')" >> .env

# (b) Fernet 키 생성 — KMS 가 secrets 를 암호화하는 데 씀
echo "FERNET_KEY=$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')" >> .env

# (c) 분석기 이미지 빌드 (5종 전부)
docker compose --profile analyzers build

# (d) 부팅 자가진단으로 모든 설정 확인
docker compose exec platform python -m app.cli verify
# 다음 5개 모두 'ok' 라고 출력되어야 함:
#   ok    config: mnemos_env=production
#   ok    database
#   ok    redis
#   ok    crypto: round_trip
#   ok    analyzers: all present
```

### 2.2 운영자 계정 + 첫 프로젝트

```bash
# admin 계정 생성 (비밀번호는 프롬프트)
docker compose exec platform python -m app.cli create-user \
  --username yourname --role admin

# 그 후 GUI 에서:
# 1. /login 로그인
# 2. /projects → "Register a project" — GitLab URL 입력
# 3. /analysis → 방금 만든 프로젝트의 "Run analysis" 버튼
```

분석 시작 ~5분 후 (10K LOC 기준) `/findings` 에 결과가 나타납니다.

### 2.3 Claude Code 에서 MCP 로 조회

`~/.config/claude-code/mcp.json`:

```json
{
  "mcpServers": {
    "mnemos": {
      "command": "docker",
      "args": [
        "compose", "-f", "/path/to/Mnemos/docker-compose.yml",
        "exec", "-T", "platform",
        "python", "-m", "app.mcp.cli", "--project", "<project-id>"
      ],
      "env": {
        "MNEMOS_MCP_TOKEN": "<token from /settings>"
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

### 3.3 분석기 정확도 측정

```bash
# 운영 환경에서 한 번만 실행 — 정확도 베이스라인 확보
docker compose exec platform python /app/scripts/accuracy/measure.py \
  --all --strict

# 출력: 분석기 × fixture 별 precision/recall/F1
# floor (precision ≥ 0.85, recall ≥ 0.80, f1 ≥ 0.82) 아래로 떨어지면 exit 1
```

자체 코드베이스를 fixture 로 추가하려면 `scripts/accuracy/fixtures/`
에 디렉토리 + `expected.json` 만 만들면 됩니다.

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
| `analyzer_binary_not_found` | 이미지 미빌드 (PR-98) | `docker compose --profile analyzers build` |
| 분석 시작 안 됨 | webhook URL 미설정 | GitLab project → webhooks → `https://<host>/api/v1/webhooks/gitlab` |
| MCP 도구 호출 401 | MNEMOS_MCP_TOKEN 누락 | `/settings → Connections` 에서 발급 |
| seed-demo 거부 | `MNEMOS_ENV=production` | `--force` 또는 staging 환경에서만 사용 |

## 5. 더 깊이 가는 링크

- 아키텍처: `docs/architecture.md`
- 분석 전략: `docs/analysis-strategy.md`
- 분석기 컨트랙트: `docs/analyzer-contract.md`
- 배포: `docs/operator-guide/deployment.md`
- 대규모 시스템 readiness: `docs/operator-guide/large_system_readiness.md`
- Phase 1 체크리스트: `docs/operator-guide/phase1_checklist.md`
- 성능 리뷰: `docs/operator-guide/performance-review.md`
- ultrareview 사용법: `docs/ultrareview.md`

## 6. 도움 요청

- 문서 누락 / 잘못된 단계: GitHub Issues
- 보안 이슈: `SECURITY.md` 참고 — 공개 이슈 만들지 말고 비공개 보고
