# Knowledge Production Platform — Phase 1 설계 명세 v2

> **문서 목적**: 이 문서는 Phase 1 구현의 **단일 진실 원천(single source of truth)** 이다. AI 코딩 에이전트(Claude Code)와 사람 개발자 1인이 함께 구현하는 과정에서 모든 의사결정의 근거, 범위의 경계, 인터페이스 계약을 제공한다.

> **v2 개정 요지**: v1이 "분석 파이프라인"에 치우쳐 있던 것과 달리 v2는 **"축적된 지식 자산으로 무한히 요청을 처리하는 상시 운영 플랫폼"** 으로 재정의한다. 분석은 상시 배경 작업, 주된 활동은 **요청 처리**(질의응답 · 데이터 조회 · 개발 수행) 세 가지다.

---

## 0. 문서 사용법

### 0.1 독자

- **주 독자**: Claude Code (구현 담당)
- **부 독자**: 사람 개발자 1인 (리뷰·승인·방향 결정)

### 0.2 AI 에이전트 작업 지시

1. 섹션 1~3 (목표·원칙·용어)을 먼저 읽고 기억. 이후 모든 결정은 여기 제시된 원칙에 부합해야 함
2. 섹션 4 아키텍처에서 구성요소 경계 파악
3. 구현할 모듈을 정한 뒤 섹션 5~13 중 해당 부분 정독
4. 코드 작성 전 섹션 12(데이터 모델), 13(인터페이스 계약) 반드시 참조
5. 섹션 14(안전 원칙)는 언제나 우선. 효율과 충돌 시 안전 선택
6. 섹션 15(로드맵) 주차 범위 준수. 앞선 주차 작업을 미리 하지 말 것
7. 모호한 부분 발견 시 임의로 결정하지 말고 사용자에게 질문

### 0.3 답하지 않는 것

- 구체 변수명·함수명 (구현자 결정)
- UI 픽셀 수준 디자인 (원칙만 제시, 구현자 재량)
- 테스트 프레임워크 선택 (언어별 관례)
- 로깅 포맷 (섹션 14.4 감사 요구만 충족하면 자유)

### 0.4 확정된 프로젝트 컨텍스트

| 항목 | 값 |
|---|---|
| 개발 인력 | 사람 1명 + Claude Code 협업 |
| 호스팅 | Windows Server 또는 Linux Server (자체 인프라) |
| 컨테이너 | Docker + Docker Compose |
| Git 호스팅 | GitLab 자체 서버 (self-hosted) |
| 사용자 인터페이스 | **모든 기능 GUI 기반** (CLI는 보조 수단) |
| 첫 지원 언어 | C#, TypeScript |
| 첫 지원 DB | MSSQL, Oracle |
| LLM 제공자 | Anthropic Claude (요약용), Claude Code (대화/개발) |

---

## 1. 목표와 배경

### 1.1 한 문장 정의

**운영 중인 복합 언어·복합 DB 시스템을 지속적으로 분석·축적하여, 그 축적된 지식 자산으로 개발·질의응답·데이터 조회 요청을 상시 처리하는 자체 호스팅 플랫폼.**

### 1.2 플랫폼의 본질

v2에서 명확히 하는 세 가지:

- **살아있는 자산**: 지식 그래프는 한 번 만들고 끝나는 산출물이 아니라 **시스템이 변할 때마다 갱신되는 조직의 기억장치**
- **상시 요청 처리**: Claude Code 채널로 **매일 반복적으로** 상호작용. 요청 종류는 다음 세 가지가 대등한 1급 기능
  - (a) **질의응답**: "결제 실패 시 재시도 로직 어디?", "이 함수 수정하면 뭐가 영향?"
  - (b) **데이터 조회**: "Orders 테이블 샘플 10건 보여줘", "실제 status 필드에 들어있는 값 분포는?"
  - (c) **개발 수행**: "이 엔드포인트 캐싱 추가해줘", "이 필드 nullable로 변경"
- **피드백 루프**: 요청 처리 중 확인·수정된 사실은 지식 자산에 반영되어 `certainty` 강화

### 1.3 해결하는 문제

- 엔터프라이즈 시스템은 복합 언어(C#, TypeScript, SQL 등) + 복합 DB(Oracle, MSSQL) + 빌드된 DLL로 구성
- 문서는 낡고, 지식은 특정 사람 머리에 있으며, 퇴사·이동 시 손실
- 일반 AI 코딩 도구는 구체 지식 없어 도움 제한적
- 일회성 분석은 하자마자 낡음 — 지속 갱신 필수
- **스키마만 보고는 실제 데이터가 뭔지 모름** — 샘플 확인 필수
- 운영 시스템은 실수에 민감하므로 AI 자율 행동 무제한 허용 불가

### 1.4 해결하지 않는 것

- 신규 프로젝트 스캐폴딩
- 일반 코드 포매팅·린팅 (기존 도구 사용)
- 성능 프로파일링·로드 테스트
- 보안 감사 (SAST 통합 가능하나 대체 아님)
- DB 관리자 업무 (DDL 직접 실행, 백업, 복구)

### 1.5 Phase 1 성공 기준

- 실제 운영 중인 C# + TS + MSSQL/Oracle 시스템 1종을 GUI로 등록 → 8시간 내 1차 분석 완료
- 등록 이후 **상시 모드로 전환** — git push, DB 스키마 변경, 런타임 관측을 지속 반영
- Claude Code에서 세 요청 종류(질의·데이터·개발) 자연스럽게 수행
- 데이터 조회 시 **PII 마스킹된 샘플** 제공
- 개발 요청은 Gate A/B 승인을 거쳐 **GitLab PR(Merge Request) 생성**
- 모든 LLM·MCP·파일 쓰기·DB 접근·데이터 조회가 감사 로그
- 세 안전 격리 원칙(소스·DB·런타임) 자동 강제

---

## 2. 설계 원칙

### 2.1 원칙 1 — 언어 중립 지식 그래프가 1급 시민

각 분석기·추출기·수집기·샘플러는 공통 지식 그래프의 정보원(source)일 뿐. 상위 계층은 소스를 차별하지 않는다.

**함의**: 새 언어·새 DB 추가 시 상위 에이전트·대시보드를 수정하면 안 됨. 수정해야 하면 그래프 모델이 불완전.

### 2.2 원칙 2 — 경계는 계약으로 이어진다

언어·DB·바이너리가 만나는 지점은 명시적 계약(HTTP·gRPC·토픽·테이블·파일·DLL export)을 가진다. 소스를 직접 잇지 않고 계약을 중심으로 잇는다.

**함의**: TS `fetch`와 C# `[HttpPost]`는 같은 `Contract` 노드를 참조. ID는 정규화된 URL 패턴.

### 2.3 원칙 3 — 정보는 기여하는 만큼만

소스 없음, 권한 제한, 불투명 DLL — 한계를 숨기지 않고 "알려진 것 / 추정된 것 / 모르는 것"을 그래프에 명시.

**함의**: 모든 노드·엣지는 `certainty` 플래그 보유. `inferred`는 사용자에게 반드시 표기.

### 2.4 원칙 4 — Claude Code에 위임, 우리는 감싼다

대화·개발 루프는 Claude Code가 수행. 우리는 (a) 지식 생산, (b) 안전 게이트, (c) 도구 제공.

**함의**: 자체 LLM 호출은 분석 파이프라인(L1~L5 요약)에서만. 대화·Plan·Coder 에이전트를 만들지 않음.

### 2.5 원칙 5 — 운영 시스템은 신성하다

효율·편의가 안전과 충돌하면 안전. main 직접 push 금지, 운영 DB 쓰기 금지, 운영 배포 금지. 이 가드를 끌 설정 스위치는 **만들지 않음**.

### 2.6 원칙 6 — Bottom-up 점진적 분석

어떤 LLM 호출도 전체 코드베이스를 보지 않음. 함수→파일→모듈→도메인→시스템의 계층 집계.

### 2.7 원칙 7 — 플랫폼은 상시 운영 서비스

"실행하고 결과 받기"가 아니라 "계속 켜져있고 일하는" 서비스.

**함의**:
- 분석은 이벤트 기반(webhook)·주기 기반(cron)으로 **자동 재실행**
- 지식 자산은 **append-only 이력**, 과거 스냅샷 조회 가능
- 대시보드·MCP 서버는 24/7 동작 전제
- 모든 상태는 **restart-safe** (메모리 의존 금지)

### 2.8 원칙 8 — 데이터 최소 권한·최소 노출

실제 운영 DB 접근은 필연적이나 최소화·마스킹·감사가 필수.

**함의**:
- DB 계정은 **read-only** 전용
- 샘플링은 **LIMIT/TOP 제한** + **PII 마스킹** 후 저장
- 사용자가 데이터 요청 시 **요청 목적 기록**, 감사 로그 필수
- 민감 테이블(사용자 지정)은 데이터 조회 금지, 스키마만

### 2.9 원칙 9 — 모든 기능은 GUI로

CLI는 보조 수단일 뿐. 운영자가 수행하는 모든 작업(등록·설정·실행·승인·조회)은 GUI로 가능해야 함.

**함의**:
- 프로젝트 등록 GUI
- DB 접속 정보 관리 GUI (등록·테스트·갱신·삭제)
- GitLab 연동 설정 GUI
- 분석 트리거·중단·재시작 GUI
- 데이터 조회 히스토리 GUI
- 감사 로그 검색 GUI
- 플랫폼 설정 GUI (LLM 키, OTel 엔드포인트, 등)

### 2.10 원칙 10 — 1인 개발자 친화

1인 개발이므로 복잡도 통제가 핵심. Kubernetes, 마이크로서비스, 분산 트랜잭션 같은 무거운 설계는 Phase 1 범위 밖.

**함의**:
- **Docker Compose 단일 호스트** 배포
- **단일 Python 서버** 프로세스 (스레드·asyncio로 충분)
- **외부 의존성 최소** (Postgres, Redis, 분석기 컨테이너만 필수)
- 사람 리뷰 불가능한 복잡 로직은 지양

---

## 3. 용어 정의

| 용어 | 정의 |
|---|---|
| **Component** | 배포 가능 단위. 서비스, DLL, 외부 API, DB 인스턴스 포함. 투명(소스 있음) 또는 불투명(DLL만). |
| **Symbol** | 코드가 정의하는 실행 단위. 함수, 클래스, stored procedure, trigger, DLL export. |
| **Contract** | 컴포넌트 경계의 명시적 계약. HTTP endpoint, gRPC method, queue topic 등. |
| **DataEntity** | 데이터가 머무는 곳. 테이블, 뷰, 패키지 상수, Redis 키 패턴, 파일 포맷. |
| **DataSample** | DataEntity에서 채취한 샘플 레코드. PII 마스킹 후 저장된 사본. |
| **Certainty** | 노드·엣지의 확실성. `verified`(정적+런타임) / `asserted`(한 소스) / `inferred`(LLM/휴리스틱). |
| **Source** | 그래프에 정보를 기여하는 주체. 분석기, 추출기, 샘플러, 수집기. |
| **L0~L5** | 분석 계층. L0=원시 사실(LLM 미사용) / L1=함수 / L2=파일 / L3=모듈 / L4=도메인 / L5=시스템. |
| **Gate A** | Plan 승인 게이트. 사용자가 SPEC+태스크 승인. |
| **Gate B** | Diff 승인 게이트. 사용자가 최종 변경 승인 → GitLab MR 생성. |
| **Opaque Component** | 소스 없는 Component. 바이너리 메타+런타임 관측으로만 정보 수집. |
| **Finding** | Merge 엔진이 탐지한 정보원 간 불일치·이상. |
| **Session** | Claude Code ↔ 플랫폼 연결 단위. MCP로 식별. |
| **Request** | Session 내 단일 작업 요청 (질의·데이터·개발 중 하나). |
| **Analysis Run** | 한 번의 분석 실행. 전체 또는 증분. |
| **Snapshot** | 특정 시점의 지식 그래프 스냅샷. append-only로 누적. |

---

## 4. 상위 아키텍처

### 4.1 책임 분할

```
┌──────────────────────────────────────────────────────────────┐
│  사용자 GUI (웹 대시보드)                                      │
│  프로젝트 등록 · 설정 · 분석 모니터링 · Gate A/B ·               │
│  데이터 조회 내역 · 감사 로그 · Findings                        │
└──────────────────────────────────────────────────────────────┘
                            ↕ HTTP/SSE
┌──────────────────────────────────────────────────────────────┐
│  Knowledge Production Platform (우리가 만드는 것)              │
│                                                              │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐  │
│  │ 상시 분석 루프 │→ │ 지식 자산     │→ │ MCP 서버 + 아티팩트  │ │
│  │ (이벤트/cron) │  │ (append-only) │ │ (Claude Code용)      │  │
│  └──────────────┘ └──────────────┘ └──────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 요청 처리 도구 (MCP)                                    │  │
│  │ · 질의응답 도구 (search_symbols, get_symbol, ...)       │  │
│  │ · 데이터 조회 도구 (sample_data, query_data, ...)        │  │
│  │ · 개발 도구 (submit_plan, submit_diff, edit_file, ...)  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 승인 게이트 + 샌드박스 + 감사 로그                      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
                            ↕ MCP (stdio/HTTP)
┌──────────────────────────────────────────────────────────────┐
│  Claude Code (사용자 IDE/터미널, 우리가 만들지 않음)           │
│  대화형 질의·개발·편집. 우리 MCP 도구 자유 사용.                │
└──────────────────────────────────────────────────────────────┘
                            ↕ read-only
┌──────────────────────────────────────────────────────────────┐
│  운영 시스템 (관찰 대상)                                       │
│  GitLab 저장소 · Oracle/MSSQL DB · OTel 트레이스                │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 구성 요소 목록

**분석기 풀** (외부 프로세스, 언어별 Docker 컨테이너):
- `ggoss-csharp` — Roslyn 기반
- `ggoss-ts` — TypeScript Compiler API
- `ggoss-sql-mssql` — T-SQL 파서 + 스키마 추출 + DMV + **샘플 데이터 채취**
- `ggoss-sql-oracle` — PL/SQL 파서 + 스키마 추출 + V$SQL + **샘플 데이터 채취**
- `ggoss-binary-dotnet` — Mono.Cecil 기반 DLL 표면 추출
- `ggoss-runtime-otel` — OTLP 수신기 (서버 프로세스 내 포함)

**서버** (단일 Python 프로세스):
- HTTP/SSE API (FastAPI)
- MCP 서버 (Python mcp SDK)
- 상시 분석 스케줄러 (ARQ + cron)
- GitLab 연동 (webhook 수신, MR 생성)
- Merge & Reconcile 엔진
- Extractor 에이전트 런타임 (L1~L5 요약)
- 아티팩트 생성기 (AGENTS.md, Skills, `.mcp.json`)
- 데이터 샘플러·쿼리 실행기
- 샌드박스 매니저 (Docker 컨테이너 제어)
- 감사 로거

**저장소**:
- PostgreSQL 16 + pgvector (모든 영속 데이터)
- Redis 7 (큐·잠금·캐시)
- 파일시스템 (분석 대상 레포 복사본, worktree, 아티팩트)

**대시보드** (SSR HTMX + 선택적 React 섬):
- 8개 탭: Projects, Analysis, Data, Plans, Diffs, Findings, Audit, Settings

### 4.3 기술 스택

| 영역 | 선택 | 근거 |
|---|---|---|
| 분석기 (C#) | .NET 8 콘솔, Roslyn | 컴파일러 품질 의미 분석 |
| 분석기 (TS) | Node 20, TypeScript Compiler API | 공식 API, 타입 추론 완전 |
| 분석기 (MSSQL) | .NET 8, `TransactSql.ScriptDom` + ADO.NET | 공식 T-SQL 파서 |
| 분석기 (Oracle) | Python 3.12, `antlr4-python3-runtime` + `oracledb` | PL/SQL 파서 + 공식 드라이버 |
| 분석기 (바이너리) | .NET 8, `Mono.Cecil` | 메타데이터 표준 |
| 런타임 수신기 | Python, `opentelemetry-proto` | OTLP 표준 |
| 서버 | Python 3.12, FastAPI, uvicorn | LLM 생태계, SSE, pydantic |
| ORM | SQLAlchemy 2.x + Alembic | 표준 선택 |
| 큐 | Redis + ARQ | asyncio 네이티브 |
| DB | PostgreSQL 16 + pgvector | 사실·요약·임베딩 일원화 |
| Git | `pygit2` (libgit2) + `python-gitlab` | worktree + GitLab API |
| LLM (요약) | `anthropic` 공식 SDK | Claude Code와 모델 일관성 |
| MCP 서버 | `mcp` 공식 Python SDK | Anthropic 표준 |
| 프론트 | HTMX + Alpine.js 기본, React는 Gate A/B와 데이터 뷰어만 | 1인 개발 친화적 |
| 시각화 | Mermaid (문서), D3 force-directed (영향도 그래프) | 용도 분리 |
| 샌드박스 | Docker + 공식 .NET SDK, Node 이미지 | 빌드 격리 |
| 인증 | 단순 사용자명/비밀번호 + 세션 쿠키 | 1인 사용이므로 과도 설계 회피 |

### 4.4 배포 — Docker Compose

**단일 호스트(Windows Server 또는 Linux Server) + Docker Compose** 로 배포. Kubernetes는 Phase 2 이후.

`docker-compose.yml` 서비스 목록:
- `platform` — Python 서버 + 대시보드 (단일 컨테이너)
- `postgres` — PostgreSQL 16 + pgvector 확장
- `redis` — Redis 7
- `analyzer-csharp` — 요청 시 ad-hoc 실행 (Compose profile 또는 외부 docker run)
- `analyzer-ts` — 동일
- `analyzer-mssql` — 동일
- `analyzer-oracle` — 동일
- `sandbox-dotnet` — 개발 시 빌드·테스트용
- `sandbox-node` — 동일

Windows Server는 **Linux 컨테이너 모드** 또는 **WSL2 백엔드** 사용. 네이티브 Windows 컨테이너는 Phase 1에서 지원하지 않음.

---

## 5. 지식 그래프 모델

### 5.1 노드 타입

#### Component

```yaml
id: string            # "svc.OrderService" | "bin.Legacy.Pricing.dll" | "db.mssql.OrderDb"
kind: enum            # service | library | binary | database | external_api | queue_cluster
name: string
language: string | null
is_opaque: bool       # true = 소스 없음
source_locations: [path]
runtime_addresses: [string]
metadata: jsonb
certainty: enum
created_by: [source_name]
```

#### Symbol

```yaml
id: string            # "csharp:Order.OrderService.Create(CreateOrderRequest)"
kind: enum            # function | method | class | interface | type | stored_procedure | trigger | export
name: string
component_id: string
signature: string
location: {file, line, col} | null
visibility: enum      # public | internal | private
is_entry_point: bool
xml_doc: text | null
metadata: jsonb
certainty: enum
created_by: [source_name]
```

#### Contract

```yaml
id: string            # 정규화된 ID (아래 규칙)
kind: enum            # http_endpoint | grpc_method | queue_topic | dll_export | file_drop
name: string
spec: jsonb           # 파라미터·응답·보안
metadata: jsonb
certainty: enum
created_by: [source_name]
```

**Contract ID 정규화 규칙 (중요)**:
- HTTP: `http.{METHOD}.{path-pattern}` (path variable 은 `{name}`, 쿼리스트링 제외)
- gRPC: `grpc.{service-fqn}.{method}`
- Kafka/MQ: `mq.topic.{topic-name}` | `mq.queue.{queue-name}`
- DLL export: `dll.{assembly-name}.{type-fqn}.{method-signature}`
- 파일 드롭: `file.drop.{path-pattern}` (glob 허용)

정규화 실패 시 `inferred` + `candidate_ids` 배열.

#### DataEntity

```yaml
id: string            # "db.mssql.OrderDb.dbo.Orders" | "db.oracle.PAYMENT.PAYMENTS"
kind: enum            # table | view | stored_procedure | package | redis_key_pattern | file_format
component_id: string
name: string
schema: jsonb         # 컬럼, 타입, 제약, 인덱스
sample_available: bool
sample_last_refreshed: timestamp | null
is_sensitive: bool    # 사용자 지정, true면 샘플 조회 금지
metadata: jsonb
certainty: enum
created_by: [source_name]
```

### 5.2 엣지 타입

공통 필드: `id, source_node_id, target_node_id, kind, certainty, created_by, metadata`.

- **CALLS**: Symbol → Symbol/Contract. `invocation_site`, `call_type`, `exercised`, `exercise_count`
- **EXPOSES**: Component/Symbol → Contract
- **READS / WRITES**: Symbol → DataEntity. `access_pattern`, `columns_touched`
- **CONTAINS**: Component → Symbol/DataEntity

### 5.3 확실성 규칙

| 상황 | certainty |
|---|---|
| 정적 분석 + 런타임 관측 | `verified` |
| 정적만 | `asserted` |
| 런타임만 | `asserted` |
| LLM 요약·추정 | `inferred` |

`inferred` 표시는 UI·MCP 응답·생성 문서 **모든 곳에서 의무**.

### 5.4 Append-only 스냅샷

모든 노드·엣지는 **시간 축**을 가진다:
- `valid_from` / `valid_to` (현재 유효한 것은 `valid_to = null`)
- 변경 시 기존 레코드에 `valid_to = now()` 세팅 + 새 레코드 추가

과거 시점의 그래프를 조회 가능: `WHERE valid_from <= :t AND (valid_to IS NULL OR valid_to > :t)`.

### 5.5 저장 구조

PostgreSQL, 섹션 12.2 참조. Neo4j 미사용.

---

## 6. 분석기 플러그인 규격

### 6.1 공통 CLI 인터페이스

모든 분석기가 지원:

```
ggoss-<name> probe <path>
  → { applicable: bool, reason: string, files_found: int }

ggoss-<name> inventory <path>
  → { files: [...], modules: [...], errors: [...] }

ggoss-<name> symbols <path> [--output <file>]
ggoss-<name> calls <path> [--output <file>]
ggoss-<name> contracts <path> [--output <file>]
ggoss-<name> data_access <path> [--output <file>]
ggoss-<name> schema              # 이 분석기의 JSON Schema
```

출력: JSON Lines, 한 줄 = 한 레코드.

### 6.2 공통 출력 래퍼

```json
{
  "record_type": "symbol" | "contract" | "data_entity" | "edge" | "sample",
  "source_name": "ggoss-csharp",
  "source_version": "1.0.0",
  "analyzed_at": "2026-04-17T10:00:00Z",
  "data": { /* 5.1, 5.2 스키마 */ }
}
```

### 6.3 DB 분석기 추가 커맨드 (v2 신규)

DB 분석기(`ggoss-sql-mssql`, `ggoss-sql-oracle`)는 추가 커맨드:

```
ggoss-sql-<db> live_schema --conn-ref <secret-id>
  → 라이브 DB에서 스키마 + 저장 로직 추출

ggoss-sql-<db> live_stats --conn-ref <secret-id>
  → DMV/AWR/V$SQL에서 실행 통계

ggoss-sql-<db> sample --conn-ref <secret-id> --table <fqn> --limit <n>
  → 샘플 레코드 채취 (PII 마스킹 전 원본, 이후 파이프라인이 마스킹)

ggoss-sql-<db> query --conn-ref <secret-id> --sql-file <path>
  → 제한적 쿼리 실행 (read-only, LIMIT 강제)
```

### 6.4 에러 처리 규약

- 부분 실패 감내: 파일 1개 파싱 실패로 전체 실패 불가
- 에러는 stderr JSON Lines:
  ```json
  {"level":"error","file":"Order.cs","message":"...","recoverable":true}
  ```
- `recoverable: false` 만 exit code 非0

### 6.5 성능 요구

- 10만 라인 레포: `symbols` 10분 이내
- 30만 라인 레포: 30분 이내
- 메모리: 4GB 이내

---

## 7. 언어별 분석기 상세

### 7.1 ggoss-csharp

**언어**: C# / .NET 8 콘솔
**핵심 라이브러리**: `Microsoft.CodeAnalysis.*`, `Microsoft.CodeAnalysis.Workspaces.MSBuild`
**입력**: `.sln` 또는 `.csproj`
**사전 조건**: 빌드 가능 (NuGet 복원 완료)

**추출 대상**:

- `symbols`: 클래스·인터페이스·구조체·레코드·열거형·메서드·프로퍼티·필드·이벤트. `DocumentationCommentId`로 cross-project 통일.
- `calls`: `SymbolFinder.FindCallersAsync`. 호출 사이트 위치 포함.
- `contracts` (ASP.NET):
  - `[HttpGet/Post/...]`, `[Route]` attribute
  - `MapGet`, `MapPost` minimal API
  - `ControllerBase` convention routing
  - SignalR Hub 메서드
  - gRPC `ServiceBase`
  - MassTransit/Rebus `IConsumer<T>`
  - Quartz/Hangfire 잡
- `data_access`:
  - EF Core `DbSet<T>` + LINQ expression tree
  - Dapper 문자열 SQL → SQL 분석기로 2차 위임
  - `FromSqlRaw`, `ExecuteSqlRaw`
  - `CommandType.StoredProcedure`

**주의사항**:
- Cross-project 비교: 반드시 `DocumentationCommentId`로 ID 통일
- 제네릭 인스턴스화는 "개념 노드" 하나로 통합
- Source generator: `obj/Generated/` 포함
- 빌드 실패 시 어떤 파일의 어떤 오류인지 상세 보고

### 7.2 ggoss-ts

**언어**: Node 20 + TypeScript
**핵심 라이브러리**: `typescript` 공식 컴파일러 API
**입력**: `tsconfig.json` 또는 프로젝트 루트
**사전 조건**: `npm/pnpm install` 완료, TypeScript 4.5+

**추출 대상**:

- `symbols`: 클래스·인터페이스·타입 alias·enum·함수·메서드·React FC·NestJS 데코레이터
- `calls`: TypeChecker + findReferences. 동적 `import()` 추적. React hook 콜백도 CALLS로
- `contracts`:
  - `fetch(url)`, `axios.*` 호출 (URL 추출)
  - WebSocket, SSE
  - tRPC, GraphQL 클라이언트
  - NestJS `@Get/Post` (백엔드 TS)
- `data_access`: TypeORM, Prisma, Sequelize, MongoDB 드라이버, SQL 리터럴

**주의사항**:
- Monorepo: project references 처리
- JSX/TSX 활성화
- Barrel export 간접 체인 추적
- URL 리터럴 복원 실패 시 `inferred`

### 7.3 ggoss-sql-mssql

**언어**: C# / .NET 8
**핵심 라이브러리**: `Microsoft.SqlServer.TransactSql.ScriptDom`, `Microsoft.Data.SqlClient`

**동작 모드**:
1. **오프라인 파일 모드**: `.sql` 파일 파싱만
2. **라이브 모드**: read-only 연결

**라이브 모드 추출**:

- `symbols`: `sys.sql_modules`에서 프로시저·트리거·뷰·함수 정의 + TSqlParser AST
- `data_access`: 프로시저 본문 SELECT/INSERT/UPDATE/DELETE + 대상 테이블·컬럼
- `contracts`: Linked Server 접근 → CALLS (`cross_db=true`)
- `DataEntity`: 테이블·뷰·컬럼·제약·인덱스 전수 + 실행 통계(`sys.dm_exec_query_stats`)

**샘플 채취 (live_schema + sample)**:
- `SELECT TOP (@N) * FROM <table>` — 설정된 N (기본 10)
- 결과는 JSON Lines로 반환, **마스킹은 서버 측에서 수행**
- 시간 제한 10초, 초과 시 중단

**주의사항**:
- 실행 통계 조회는 부하 고려, 메인터넌스 윈도우 외 허용 시간 설정
- 권한 부족 시 graceful degradation (스키마만)
- 암호화된 프로시저 (`WITH ENCRYPTION`) 는 메타만

### 7.4 ggoss-sql-oracle

**언어**: Python 3.12
**핵심 라이브러리**: `antlr4-python3-runtime` + plsql grammar, `oracledb` (thin mode)

**라이브 모드 추출**:

- `symbols`: `ALL_SOURCE`에서 패키지·프로시저·함수·트리거 본문
- `data_access`: PL/SQL AST에서 테이블 접근 + `DBA_DEPENDENCIES` 조합
- `DataEntity`: `ALL_TABLES`, `ALL_TAB_COLUMNS`, `ALL_CONSTRAINTS`, `ALL_INDEXES`
- 실행 통계: AWR(라이선스 동의 필요) 또는 `V$SQL` + `V$SQLSTATS`

**샘플 채취**:
- `SELECT * FROM <table> FETCH FIRST :n ROWS ONLY`
- 동일한 마스킹 플로우

**주의사항**:
- AWR는 Enterprise Edition + Diagnostic Pack 라이선스. 설정에서 명시적 opt-in 필요
- V$ 뷰는 세션 스냅샷 제한적
- Database link 접근은 `cross_db` CALLS

### 7.5 ggoss-binary-dotnet

**언어**: C# / .NET 8
**핵심 라이브러리**: `Mono.Cecil` (assembly load 없이 메타만 읽음)

**추출 대상**:
- `symbols`: public 타입·메서드·프로퍼티 + 시그니처 + attribute
- `Component`: `kind=binary`, `is_opaque=true` + 참조 어셈블리 목록
- 내부 호출 그래프는 추출하지 않음 (불투명 유지)

### 7.6 ggoss-runtime-otel

**언어**: Python (서버 프로세스 일부)
**프로토콜**: OTLP (gRPC 4317, HTTP 4318)

**수집 대상**:
- HTTP 서버/클라이언트 span
- DB span (`db.system`, `db.statement`, `db.operation`)
- gRPC span

**처리**:
1. 수신 시 PII scrubber 필수:
   - `http.request.body`, `http.response.body` 마스킹
   - `db.statement` 리터럴 값 마스킹 (prepared template만 남김)
   - 이메일·전화·주민번호 패턴 마스킹
2. 요청 단위 call chain 재조립 (`parent_span_id`)
3. Contract/DataEntity와 매칭, 엣지 `exercised` 갱신
4. 매칭 실패 관측은 `asserted` 신규 엣지

**샘플링**: 기본 1%, 설정으로 0.1%~10%.

---

## 8. 데이터 샘플러 (v2 신규)

### 8.1 목적

스키마만으로는 "이 컬럼에 실제로 뭐가 들어있는지" 알 수 없다. 샘플을 채취해 (a) 그래프 상의 DataEntity 정보 보강, (b) 사용자 요청 시 즉시 제공.

### 8.2 샘플링 정책

**채취 시점**:
- 최초 분석 실행 시 모든 table/view에 대해 자동
- 스키마 변경 감지 시 해당 entity 재채취
- 사용자가 GUI에서 수동 트리거
- 주기적 갱신 (기본 7일)

**채취 크기**:
- 기본 10행, 설정으로 1~100 가능
- 대용량 테이블도 `LIMIT/TOP`으로 안전
- 전체 count는 별도 `SELECT COUNT(*)` (비용 큰 경우 근사치만)

**제외**:
- `is_sensitive=true` 테이블은 자동 채취 제외
- 사용자가 특정 컬럼 마스킹 규칙 지정 가능 (정규식)

### 8.3 PII 마스킹

**자동 탐지 패턴** (Phase 1 기본):
- 이메일 `[\w.+-]+@[\w-]+\.[\w.-]+`
- 전화 `\d{2,3}-?\d{3,4}-?\d{4}`
- 주민번호 `\d{6}-?\d{7}`
- 신용카드 `\d{4}-?\d{4}-?\d{4}-?\d{4}`
- IP 주소

**컬럼명 기반 힌트**:
- `password`, `token`, `secret`, `api_key`, `ssn`, `rrn` → 완전 마스킹
- `email`, `phone`, `name`, `address` → 부분 마스킹 (앞 3자만)

**마스킹 후 저장** — 저장소에는 원본이 절대 남지 않음.

### 8.4 저장 구조

```sql
CREATE TABLE data_samples (
  id uuid PRIMARY KEY,
  project_id uuid,
  data_entity_id text,
  sample_rows jsonb NOT NULL,        -- 마스킹된 N행
  row_count_estimate bigint,
  distinct_value_stats jsonb,        -- 주요 컬럼별 distinct 값 분포
  sampled_at timestamp,
  valid_until timestamp              -- 이 시점 후 stale
);
```

### 8.5 통계 수집

샘플 채취와 함께 가벼운 통계:
- 컬럼별 null 비율
- 컬럼별 distinct count (근사, HyperLogLog 또는 단순 `COUNT(DISTINCT)` with LIMIT)
- 주요 열거형 컬럼의 값 분포 (top 10)
- 수치 컬럼의 min/max/avg

이 통계는 LLM이 요약·설명 시 매우 유용 ("`status` 필드는 주로 `ACTIVE`, `DELETED`, `PENDING` 세 값이 쓰임").

### 8.6 실시간 쿼리 실행

사용자가 Claude Code 에서 "이 조건으로 몇 건 있어?" 같은 요청 시:

- MCP 도구 `query_data`로 제한적 쿼리 실행 허용
- **read-only 계정**만 사용
- **자동 LIMIT 추가** (없으면 기본 100)
- **쿼리 시간 제한** (기본 30초)
- 결과는 마스킹 후 반환
- 모든 실행은 감사 로그 기록

---

## 9. Merge & Reconcile 엔진

### 9.1 역할

다수 소스가 같은 노드·엣지에 기여 시 충돌 조정 + `certainty` 계산 + Finding 생성.

### 9.2 실행 시점

- 배치 분석 완료 시: 전체 merge
- 런타임 수신 시: 1시간 윈도우 증분 merge
- 샘플 채취 완료 시: DataEntity 통계 갱신

### 9.3 Merge 규칙

**노드 merge**: 동일 ID → 하나로 통합. `created_by`에 모든 소스. 원본은 `node_sources`에 보존. `certainty`는 최상위 선택.

**엣지 merge**: (source, target, kind) 삼중키. metadata는 merge (exercised OR, exercise_count SUM).

**우선순위**:
- 스키마: 라이브 DB > SQL 파일 > 정적 코드 추정
- Symbol 시그니처: 정적 > 런타임
- 호출 빈도: 런타임 유일
- 비즈니스 의미: 사람 > LLM

### 9.4 Finding 생성

| 조건 | 종류 | 심각도 |
|---|---|---|
| 정적 A→B 존재, 런타임 30일 미관측 | `dead_path_suspected` | info |
| 런타임 A→B, 정적 부재 | `dynamic_call_detected` | warning |
| 코드에 T 참조, 스키마에 T 없음 | `schema_mismatch` | error |
| `inferred` 엣지 30일 미승인 | `unverified_claim` | info |
| 불투명 컴포넌트 호출에서 에러 우세 | `opaque_component_failing` | warning |
| 같은 Contract를 여러 Component EXPOSES | `duplicate_endpoint` | error |

Finding은 대시보드에 누적. 상태 변경: `open` → `acknowledged` / `resolved` / `false_positive`.

### 9.5 Append-only 이력

모든 merge 결과는 **이전 스냅샷을 지우지 않음**. `valid_to`로 경계 표시.

---

## 10. 계층적 요약 (L1~L5)

### 10.1 계층 정의

| 계층 | 입력 | 출력 크기 |
|---|---|---|
| L0 | 원시 코드 | 그래프 사실 |
| L1 | 함수 본문 + 호출 대상 시그니처 | ~200 토큰 |
| L2 | 파일의 L1 요약 | ~500 토큰 |
| L3 | 모듈의 L2 요약 + 모듈 경계 엣지 | ~1K 토큰 |
| L4 | 도메인의 L3 요약 + 도메인 경계 엣지 | ~2K 토큰 |
| L5 | 모든 L4 요약 + 시스템 경계 Contract | ~5K 토큰 |

### 10.2 경계 결정

- **모듈 경계**: 자동 — 디렉토리/네임스페이스. 사용자 GUI로 재정의 가능.
- **도메인 경계**: 반자동 — 그래프 커뮤니티 탐지(Louvain 등)로 초안 제안, 사용자가 GUI에서 이름 부여·재편.

### 10.3 Extractor 에이전트

단일 에이전트, 계층별 프롬프트. 출력 필수 스키마:

```json
{
  "summary": "한 문장",
  "detailed": "자세한 설명",
  "claims": [
    {
      "claim": "OrderService는 취소 시 RefundService를 호출",
      "evidence": [
        { "kind": "edge", "edge_id": "uuid", "certainty": "verified" }
      ]
    }
  ],
  "open_questions": ["요약으로 풀리지 않은 의문"]
}
```

**claims 필수**. 근거 없는 주장은 `open_questions`로.

### 10.4 Validator

자동 검증:
- evidence의 edge_id/node_id가 그래프에 존재
- edge의 certainty가 claim을 뒷받침
- claim 엔티티가 evidence에 포함

실패 claim 제거 후 요약 재구성. 3회 재시도 후도 실패면 사람 검토 큐.

### 10.5 증분 재계산

- 파일 해시 변경 → 그 파일 L1·L2 재계산
- 모듈 경계 엣지 변화 → 해당 L3 재계산
- 도메인 경계 변화 → L4 재계산
- L5는 L4 변화 시에만

Phase 1에서는 **전체 재계산** 우선 구현, **증분은 Phase 1 후반(Week 6~7)에 최소 형태로 지원**.

---

## 11. MCP 서버 — Claude Code와의 주 인터페이스

### 11.1 구현

Python `mcp` 공식 SDK. 서버 프로세스와 동일 호스트, stdio 또는 TCP.

### 11.2 도구 분류 (3종 요청 대응)

**질의응답 도구** (읽기):
- `search_symbols`
- `get_symbol`
- `find_callers`
- `find_callees`
- `impact_analysis`
- `get_contract`
- `get_data_access`
- `get_module_summary`
- `find_runtime_path`
- `list_findings`

**데이터 조회 도구** (v2 신규):
- `get_data_entity`
- `get_sample_data`
- `get_column_stats`
- `query_data`
- `search_data` (샘플 내 값 검색)

**개발 도구** (쓰기/승인):
- `submit_plan`
- `submit_diff`
- `read_file`
- `edit_file_in_worktree` (v2 신규)
- `run_in_sandbox`

### 11.3 질의응답 도구 상세

#### search_symbols

```yaml
input:
  query: string
  kind: enum | null
  component_id: string | null
  top_k: int = 20
output:
  results: [{symbol_id, name, component_id, kind, score, excerpt}]
```

벡터 + BM25 앙상블 (RRF).

#### get_symbol

```yaml
input:
  symbol_id: string
  include: enum[] = ["summary","signature","location"]
output:
  symbol: Symbol
  l1_summary: string | null
  neighbors: {callers_count, callees_count}
```

**소스 본문은 반환하지 않음.** Claude Code가 자기 파일 읽기로 읽게 함.

#### find_callers / find_callees

```yaml
input:
  symbol_id: string
  transitive: bool = false
  max_depth: int = 3
  include_dynamic: bool = true
output:
  edges: [{caller_id, callee_id, site, certainty, exercised}]
  truncated: bool
```

#### impact_analysis

```yaml
input:
  symbol_id: string
  kinds: enum[] = ["direct","transitive","tests","data"]
output:
  directly_affected: [symbol_id]
  transitively_affected: [symbol_id]
  affected_tests: [symbol_id]
  affected_data_entities: [{entity_id, kind}]
  opaque_components_touched: [component_id]
  runtime_exercised: bool
```

#### get_contract

```yaml
input:
  contract_id: string
output:
  contract: Contract
  exposers: [component_id]
  callers: [symbol_id]
  runtime_stats: {p50, p95, p99, error_rate} | null
```

#### get_data_access

```yaml
input:
  symbol_id: string
output:
  reads: [{entity_id, access_pattern, columns}]
  writes: [{entity_id, access_pattern, columns}]
```

#### get_module_summary

```yaml
input:
  component_id: string
  level: int  # 2~5
output:
  summary: string
  detailed: string
  claims: [{claim, evidence}]
  generated_at: timestamp
  certainty_breakdown: {verified, asserted, inferred}
```

#### find_runtime_path

```yaml
input:
  entry_contract_id: string
  time_window: string = "7d"
output:
  common_paths: [{frequency, chain: [{symbol_id | contract_id | entity_id}]}]
```

#### list_findings

```yaml
input:
  severity: enum | null
  status: enum[] = ["open"]
  component_id: string | null
  limit: int = 50
output:
  findings: [Finding]
```

### 11.4 데이터 조회 도구 상세 (v2 신규)

#### get_data_entity

```yaml
input:
  entity_id: string
output:
  entity: DataEntity
  reader_symbols: [symbol_id]
  writer_symbols: [symbol_id]
  sample_available: bool
  is_sensitive: bool
```

#### get_sample_data

```yaml
input:
  entity_id: string
  limit: int = 10  # 최대 100
output:
  columns: [{name, type, nullable, is_masked}]
  rows: [[value, ...]]
  row_count_estimate: bigint
  sampled_at: timestamp
  masking_applied: bool
```

**마스킹된 샘플만 반환**. `is_sensitive=true`이면 거부.

#### get_column_stats

```yaml
input:
  entity_id: string
  column: string
output:
  null_ratio: float
  distinct_count_estimate: bigint
  value_distribution: [{value, frequency}]  # top 10 (열거형의 경우)
  numeric_stats: {min, max, avg, p50, p95} | null
```

#### query_data

**제한된 read-only 쿼리 실행**.

```yaml
input:
  component_id: string       # DB component
  sql: string                # SELECT만 허용
  limit: int = 100           # 자동 추가
  purpose: string            # 감사 로그용
output:
  columns: [{name, type}]
  rows: [[value, ...]]
  row_count: int
  executed_at: timestamp
  execution_ms: int
  masking_applied: bool
```

**안전 규칙**:
- `SELECT`만 허용 (INSERT/UPDATE/DELETE/DDL 차단)
- 자동 LIMIT 추가
- 30초 타임아웃
- read-only 계정만 사용
- 결과 마스킹 필수
- **모든 실행은 감사 로그** (purpose 필수)

#### search_data

샘플 내에서 값 검색 (운영 DB에 쿼리 보내지 않음).

```yaml
input:
  component_id: string | null
  value_pattern: string      # 정규식 또는 literal
  columns: [string] | null
output:
  hits: [{entity_id, column, sample_row_index, masked_snippet}]
```

### 11.5 개발 도구 상세

#### submit_plan

```yaml
input:
  spec: {title, motivation, non_goals, success_criteria}
  tasks: [{id, title, description, affects, depends_on}]
  target_component_id: string
  requester: string   # Claude Code session ID
output:
  plan_id: uuid
  status: "pending_approval"
  gate_a_url: string
  impact_report: {directly_affected, opaque_risk, estimated_risk}
```

제출 후 Claude Code는 **Gate A 승인까지 대기** (polling 또는 SSE).

#### submit_diff

```yaml
input:
  plan_id: uuid
  task_id: string
  diff: string                  # unified diff
  test_results: {passed, failed, log_url}
  self_review_notes: string
output:
  submission_id: uuid
  status: "pending_approval"
  gate_b_url: string
  auto_review_findings: [{severity, rule, location, message}]
```

Gate B 승인 시 GitLab MR 생성.

#### read_file

```yaml
input:
  project_id: uuid
  file_path: string
  revision: "HEAD" | sha | null
output:
  content: string
  encoding: string
  size: int
```

플랫폼이 보유한 레포 복사본에서 파일을 읽어 반환. Claude Code가 분석 대상 레포에 직접 접근할 수 없는 경우 사용.

#### edit_file_in_worktree (v2 신규)

```yaml
input:
  plan_id: uuid
  task_id: string
  file_path: string
  edits: [{old_str, new_str} | {replace_range: {start, end}, new_text: string}]
output:
  success: bool
  resulting_diff: string        # 이 호출의 누적 diff
```

**규칙**:
- 플랫폼이 제공하는 worktree 안에서만 수정
- 수정 결과는 `submit_diff`로 최종 제출해야 실제 반영
- 여러 번 호출 가능, 누적됨

#### run_in_sandbox

```yaml
input:
  plan_id: uuid
  command: string          # allowlist 내
  working_dir: string | null
  timeout_sec: int = 300
output:
  exit_code: int
  stdout: string
  stderr: string
  duration_ms: int
```

**allowlist (Phase 1)**:
- `dotnet build`, `dotnet test`, `dotnet run`
- `npm run *`, `pnpm *`, `yarn *`
- `pytest`, `python -m ...`
- `git status`, `git diff`, `git log` (read-only git)

### 11.6 MCP 접근 제어

- 읽기 도구: 프로젝트 단위 API 키로
- `submit_plan`, `submit_diff`, `edit_file_in_worktree`, `run_in_sandbox`: 동일 API 키, 하지만 Gate 승인 없이 실제 반영 불가
- `query_data`: **추가로 purpose 기록 필수**
- 모든 호출 감사 로그

### 11.7 응답 크기 제한

- 단일 응답 50KB
- 초과 시 `truncated: true` + `cursor`
- 페이징은 `cursor` 방식

---

## 12. 데이터 모델

Alembic migration으로 관리. 모든 테이블은 append-only 원칙 가능한 한 준수.

### 12.1 인증·설정 테이블

```sql
CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  username text UNIQUE NOT NULL,
  password_hash text NOT NULL,
  role text NOT NULL DEFAULT 'admin',   -- Phase 1은 admin 단일
  created_at timestamptz DEFAULT now()
);

CREATE TABLE api_keys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid REFERENCES users(id),
  project_id uuid,                      -- null이면 전역
  token_hash text UNIQUE NOT NULL,
  label text,
  created_at timestamptz DEFAULT now(),
  revoked_at timestamptz
);

CREATE TABLE platform_settings (
  key text PRIMARY KEY,
  value jsonb NOT NULL,
  updated_at timestamptz DEFAULT now()
);
-- 예: 'anthropic_api_key', 'gitlab_base_url', 'otel_endpoint', 'default_sample_size'
-- 민감값은 실제로 비밀 저장소에 두고 여기엔 참조만

CREATE TABLE secrets (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  label text UNIQUE NOT NULL,
  kind text NOT NULL,                   -- 'db_connection', 'gitlab_token', 'llm_api_key'
  ciphertext bytea NOT NULL,            -- 서버 측 대칭 암호화
  iv bytea NOT NULL,
  created_at timestamptz DEFAULT now(),
  last_tested_at timestamptz,
  last_test_result text
);
```

### 12.2 프로젝트·분석 테이블

```sql
CREATE TABLE projects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  gitlab_project_id int NOT NULL,
  gitlab_url text NOT NULL,
  default_branch text DEFAULT 'main',
  languages text[] NOT NULL,            -- ['csharp','typescript']
  created_at timestamptz DEFAULT now(),
  updated_at timestamptz DEFAULT now()
);

CREATE TABLE project_dbs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  kind text NOT NULL,                   -- 'mssql','oracle'
  display_name text NOT NULL,
  secret_id uuid REFERENCES secrets(id),  -- 접속 정보
  allow_awr bool DEFAULT false,         -- Oracle AWR 사용 명시 동의
  sensitive_tables text[] DEFAULT '{}',
  masking_rules jsonb DEFAULT '{}',
  maintenance_windows text[],           -- cron 표현, 이 시간에만 DMV 조회
  created_at timestamptz DEFAULT now()
);

CREATE TABLE analysis_runs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  status text NOT NULL,                 -- 'queued','running','completed','failed'
  triggered_by text NOT NULL,           -- 'manual','gitlab_webhook','scheduled'
  git_sha text NOT NULL,
  scope text NOT NULL DEFAULT 'full',   -- 'full','incremental'
  started_at timestamptz,
  completed_at timestamptz,
  stats jsonb,
  error_log text
);
```

### 12.3 지식 그래프 테이블 (append-only)

```sql
CREATE TABLE nodes (
  id text NOT NULL,
  project_id uuid NOT NULL,
  kind text NOT NULL,
  data jsonb NOT NULL,
  certainty text NOT NULL,
  created_by text[] NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,                 -- null = 현재 유효
  PRIMARY KEY (id, project_id, valid_from)
);

CREATE INDEX idx_nodes_current ON nodes(project_id, id) WHERE valid_to IS NULL;
CREATE INDEX idx_nodes_kind_current ON nodes(project_id, kind) WHERE valid_to IS NULL;
CREATE INDEX idx_nodes_data_gin ON nodes USING gin(data);

CREATE TABLE edges (
  id uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id uuid NOT NULL,
  source_id text NOT NULL,
  target_id text NOT NULL,
  kind text NOT NULL,
  data jsonb NOT NULL DEFAULT '{}',
  certainty text NOT NULL,
  created_by text[] NOT NULL,
  valid_from timestamptz NOT NULL DEFAULT now(),
  valid_to timestamptz,
  PRIMARY KEY (id, valid_from)
);

CREATE INDEX idx_edges_source_current ON edges(project_id, source_id) WHERE valid_to IS NULL;
CREATE INDEX idx_edges_target_current ON edges(project_id, target_id) WHERE valid_to IS NULL;

CREATE TABLE node_sources (
  node_id text NOT NULL,
  project_id uuid NOT NULL,
  source_name text NOT NULL,
  raw_data jsonb NOT NULL,
  contributed_at timestamptz DEFAULT now(),
  PRIMARY KEY (node_id, project_id, source_name, contributed_at)
);
```

### 12.4 요약·임베딩 테이블

```sql
CREATE TABLE summaries (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  target_id text NOT NULL,
  level int NOT NULL,
  analysis_run_id uuid REFERENCES analysis_runs(id),
  summary text NOT NULL,
  detailed text,
  claims jsonb,
  open_questions text[],
  model_used text NOT NULL,
  tokens_used int,
  generated_at timestamptz DEFAULT now(),
  superseded_by uuid REFERENCES summaries(id)
);

CREATE INDEX idx_summaries_current ON summaries(project_id, target_id, level) WHERE superseded_by IS NULL;

CREATE TABLE symbol_embeddings (
  symbol_id text NOT NULL,
  project_id uuid NOT NULL,
  embedding vector(1024),
  model text NOT NULL,
  created_at timestamptz DEFAULT now(),
  PRIMARY KEY (symbol_id, project_id, model)
);

CREATE INDEX idx_embeddings_vec ON symbol_embeddings USING ivfflat (embedding vector_cosine_ops);
```

### 12.5 데이터 샘플 테이블 (v2 신규)

```sql
CREATE TABLE data_samples (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  data_entity_id text NOT NULL,
  sample_rows jsonb NOT NULL,
  row_count_estimate bigint,
  column_stats jsonb,
  masking_applied bool DEFAULT true,
  sampled_at timestamptz DEFAULT now(),
  valid_until timestamptz
);

CREATE INDEX idx_samples_entity ON data_samples(project_id, data_entity_id, sampled_at DESC);

CREATE TABLE data_query_log (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  db_component_id text NOT NULL,
  sql text NOT NULL,
  purpose text NOT NULL,
  requester text NOT NULL,              -- 'user:<id>' or 'claude_code:<session>'
  row_count int,
  execution_ms int,
  error text,
  executed_at timestamptz DEFAULT now()
);
```

### 12.6 자율 개발 테이블

```sql
CREATE TABLE plans (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  status text NOT NULL,                 -- 'pending_approval','approved','rejected','executing','completed'
  spec jsonb NOT NULL,
  tasks jsonb NOT NULL,
  impact_report jsonb,
  requester text NOT NULL,
  worktree_path text,
  created_at timestamptz DEFAULT now(),
  approved_at timestamptz,
  approved_by text,
  feedback text
);

CREATE TABLE diff_submissions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  plan_id uuid REFERENCES plans(id),
  task_id text NOT NULL,
  status text NOT NULL,
  diff text NOT NULL,
  test_results jsonb,
  self_review_notes text,
  auto_review_findings jsonb,
  submitted_at timestamptz DEFAULT now(),
  approved_at timestamptz,
  approved_by text,
  gitlab_mr_iid int,
  gitlab_mr_url text
);
```

### 12.7 Findings

```sql
CREATE TABLE findings (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  kind text NOT NULL,
  severity text NOT NULL,
  status text NOT NULL DEFAULT 'open',
  subject_node_id text,
  subject_edge_id uuid,
  detail jsonb NOT NULL,
  first_seen_at timestamptz DEFAULT now(),
  last_seen_at timestamptz DEFAULT now(),
  resolved_at timestamptz,
  resolved_by text
);
```

### 12.8 MCP 세션·요청

```sql
CREATE TABLE mcp_sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id uuid REFERENCES projects(id),
  api_key_id uuid REFERENCES api_keys(id),
  client_info jsonb,                    -- Claude Code 버전 등
  started_at timestamptz DEFAULT now(),
  last_activity_at timestamptz DEFAULT now(),
  closed_at timestamptz
);

CREATE TABLE mcp_requests (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id uuid REFERENCES mcp_sessions(id),
  tool_name text NOT NULL,
  arguments jsonb,
  response_summary text,                -- 전문 저장은 선택 (용량 고려)
  duration_ms int,
  error text,
  requested_at timestamptz DEFAULT now()
);
```

### 12.9 감사 로그

```sql
CREATE TABLE audit_log (
  id bigserial PRIMARY KEY,
  project_id uuid,
  actor text NOT NULL,                  -- 'user:<id>','system','claude_code:<session>','agent:extractor'
  action text NOT NULL,
  target text,
  details jsonb,
  occurred_at timestamptz DEFAULT now()
);

CREATE INDEX idx_audit_project_time ON audit_log(project_id, occurred_at DESC);
CREATE INDEX idx_audit_actor_time ON audit_log(actor, occurred_at DESC);
```

---

## 13. HTTP API 계약

### 13.1 규약

- Base URL: `/api/v1`
- 인증: 세션 쿠키(GUI) 또는 Bearer (API)
- 응답: JSON, 에러는 RFC 7807
- 비동기: 202 + `operation_id`, SSE로 진행 구독

### 13.2 주요 엔드포인트 (GUI 지원 위주)

```
# 인증
POST   /api/v1/auth/login
POST   /api/v1/auth/logout
GET    /api/v1/auth/me

# 설정
GET    /api/v1/settings
PATCH  /api/v1/settings
POST   /api/v1/secrets
GET    /api/v1/secrets
POST   /api/v1/secrets/{id}/test       # 접속 테스트
PATCH  /api/v1/secrets/{id}
DELETE /api/v1/secrets/{id}

# 프로젝트
POST   /api/v1/projects                # 등록
GET    /api/v1/projects
GET    /api/v1/projects/{id}
PATCH  /api/v1/projects/{id}
DELETE /api/v1/projects/{id}

# DB 연결
POST   /api/v1/projects/{id}/dbs
GET    /api/v1/projects/{id}/dbs
PATCH  /api/v1/projects/{id}/dbs/{db_id}
POST   /api/v1/projects/{id}/dbs/{db_id}/test

# 분석
POST   /api/v1/projects/{id}/analyze           # 즉시 시작
POST   /api/v1/projects/{id}/analyze/schedule  # 주기 설정
GET    /api/v1/analysis_runs/{id}
GET    /api/v1/analysis_runs/{id}/events       # SSE
POST   /api/v1/analysis_runs/{id}/cancel

# 그래프·검색 (GUI용)
GET    /api/v1/projects/{id}/graph/stats
GET    /api/v1/projects/{id}/graph/search?q=...
GET    /api/v1/projects/{id}/nodes/{node_id}
GET    /api/v1/projects/{id}/nodes/{node_id}/neighbors

# 데이터 (v2 신규, GUI용)
GET    /api/v1/projects/{id}/data_entities
GET    /api/v1/projects/{id}/data_entities/{entity_id}
GET    /api/v1/projects/{id}/data_entities/{entity_id}/sample
POST   /api/v1/projects/{id}/data_entities/{entity_id}/refresh_sample
POST   /api/v1/projects/{id}/data/query        # 제한 쿼리
GET    /api/v1/projects/{id}/data_query_log

# Plan & Diff
GET    /api/v1/projects/{id}/plans
GET    /api/v1/plans/{id}
POST   /api/v1/plans/{id}/approve
POST   /api/v1/plans/{id}/reject
POST   /api/v1/plans/{id}/regenerate
GET    /api/v1/diff_submissions/{id}
POST   /api/v1/diff_submissions/{id}/approve
POST   /api/v1/diff_submissions/{id}/partial_approve
POST   /api/v1/diff_submissions/{id}/reject

# 아티팩트
GET    /api/v1/projects/{id}/artifacts
GET    /api/v1/projects/{id}/artifacts/{name}
POST   /api/v1/projects/{id}/artifacts/{name}/regenerate

# Findings
GET    /api/v1/projects/{id}/findings
PATCH  /api/v1/findings/{id}

# 감사·관측
GET    /api/v1/audit
GET    /api/v1/mcp_sessions
GET    /api/v1/mcp_sessions/{id}/requests

# GitLab webhook 수신
POST   /webhooks/gitlab                # 서명 검증 필수
```

### 13.3 작업 큐 함수 (ARQ)

```python
run_ingest(project_id, git_sha)
run_analyzer(project_id, analyzer_name, path)
run_live_db_extract(project_id, db_id, mode)   # 'schema' | 'stats' | 'sample'
run_merge(project_id, run_id)
run_summarize(project_id, run_id, level)
run_artifact_gen(project_id, run_id)
run_runtime_correlator(project_id, window)
execute_task(plan_id, task_id)
run_sample_refresh(project_id, db_id, entity_id)
run_scheduled_analysis(project_id)             # cron 트리거
```

---

## 14. 안전 원칙 강제

### 14.1 소스 격리

- GitLab 저장소는 **shallow clone** (`--depth 50`)
- main 브랜치는 read-only fetch만
- 모든 수정은 worktree 내부, 브랜치는 `ai/<timestamp>-<slug>`
- 직접 push 금지 — 샌드박스 allowlist에 `git push` 없음
- **MR 생성만 허용**, merge는 사람이 GitLab UI에서

### 14.2 DB 격리

- 프로젝트 DB 등록 시 접속 정보는 `secrets` 테이블에 **암호화 저장**
- 접속 정보는 반드시 **read-only 계정**
- 등록 시 자동 검증: write 명령 시도 → 실패해야 통과 (GUI에서 테스트 버튼)
- 운영 DB 조회는 `maintenance_windows` cron에 해당할 때만 (DMV·AWR 같은 무거운 조회)
- **DDL 실행 절대 금지** — 파서가 쿼리 감지 시 차단

### 14.3 런타임 격리

- OTel 수신기는 수신만, 운영에 무엇도 주입하지 않음
- 검증 테스트는 분리 샌드박스에서만
- 샌드박스 네트워크: 운영 DNS resolve 금지 (blackhole), dev DNS만 허용
- NuGet, npm 같은 공용 레지스트리는 proxy 허용

### 14.4 감사 로그 의무

다음은 반드시 `audit_log`에 기록:

- 로그인·로그아웃·API 키 발급·취소
- 프로젝트·DB 설정 변경
- 분석 실행 시작·완료·실패·취소
- LLM 호출 (모델, 토큰, 비용)
- MCP 도구 호출 (누가·언제·어떤 도구·결과 요약)
- **데이터 쿼리 실행** (SQL, purpose, 결과 행 수)
- **샘플 데이터 조회**
- Plan 제출·승인·거부
- Diff 제출·승인·거부
- MR 생성
- DB 접근 (계정, 쿼리 종류)
- 파일 쓰기 (worktree 내부도)
- 비밀값 저장·수정·삭제

감사 로그는 append-only. 수정·삭제 불가.

### 14.5 사용자 승인 없이 금지

- 운영 레포 main 수정
- 운영 DB 쓰기 (read-only 계정 고수)
- 분석 목적 외 LLM 호출
- 바이너리 실행 (샌드박스 밖)
- 외부 API 호출 (Anthropic, GitLab, OTel 외)
- **민감 데이터 반환** (PII 마스킹 실패 시 응답 차단)

---

## 15. 8주 로드맵

### 15.1 개발 방식

- **1인 + Claude Code 협업**: 내가 설계·리뷰·승인, Claude Code가 코드 작성
- **매주 금요일 E2E 데모**: 실제 대상 레포로 시연
- **Definition of Done**: 구현 + 테스트 + 문서 업데이트 + 감사 로그 확인
- **티켓 단위**: 한 번에 Claude Code 에게 넘기기 좋은 1~3일 크기

### 15.2 주차별 범위

#### Week 1 — 기반 + GUI 뼈대

**목표**: 프로젝트 등록부터 DB 접속 정보 관리까지 GUI로 가능.

- Docker Compose 환경 (platform, postgres, redis)
- Alembic migration: 섹션 12.1, 12.2 테이블
- FastAPI 서버 스켈레톤 + 인증(로그인/세션)
- HTMX 대시보드: Projects, Settings 탭
- Secrets 관리 GUI: 추가·테스트·수정·삭제
- GitLab 연동 설정 (base URL + token)
- 감사 로그 기본 기록

**DoD 체크**: 브라우저에서 프로젝트 등록 → DB 접속 추가 → 테스트 버튼 성공 → 감사 로그 확인.

#### Week 2 — 분석기 규격 + 첫 분석 파이프라인

**목표**: `ggoss-csharp` probe/inventory/symbols 동작. 대시보드에 심볼 목록 표시.

- 분석기 공통 CLI 계약 문서 확정
- `ggoss-csharp` Docker 이미지 + probe/inventory/symbols
- Python 서버의 AnalyzerRunner (subprocess + JSON Lines 수신)
- ARQ 큐 설정: run_ingest, run_analyzer
- 섹션 12.3 테이블 구현
- Analysis 탭 GUI: 실행 버튼, 진행 SSE, 심볼 목록
- GitLab webhook 수신 (서명 검증만, 핸들링은 Week 3)

**DoD**: 실제 C# 레포 등록 → 분석 시작 → 10분 내 symbols 테이블에 노드 생성 → 대시보드 검색 가능.

#### Week 3 — TS 분석기 + 호출 그래프 + 첫 MCP

**목표**: C# + TS 호출 그래프 + MCP 서버 3개 도구.

- `ggoss-ts` Docker 이미지 + symbols/calls/contracts
- `ggoss-csharp` calls/contracts 추가
- Merge 엔진 v1 (단순 병합, 충돌 없는 경우만)
- MCP 서버 스켈레톤 + `search_symbols`, `get_symbol`, `find_callers`
- `.mcp.json` 자동 생성
- Claude Code 실연결 테스트

**DoD**: Claude Code에서 MCP 통해 심볼 조회 성공. 대시보드의 Analysis 탭에서 심볼 간 호출 관계 확인.

#### Week 4 — 계약 정규화 + C#↔TS 연결 + DB 분석 시작

**목표**: 풀스택 엔드포인트 매칭. MSSQL 스키마 추출.

- Contract ID 정규화 규칙 구현
- HTTP 엔드포인트 매칭 (C# 컨트롤러 ↔ TS fetch)
- `find_callees`, `impact_analysis`, `get_contract` MCP 도구
- `ggoss-sql-mssql` probe/inventory/live_schema
- DB 섹션 추가 (GUI): 스키마 브라우저
- AGENTS.md 첫 버전 자동 생성
- `read_file` MCP 도구

**DoD**: "이 API 누가 호출해?" 질의가 Claude Code에서 동작. MSSQL 스키마가 DataEntity로 등록.

#### Week 5 — Oracle + 바이너리 + 데이터 샘플링 (v2 핵심)

**목표**: 데이터 조회 기능 완성. 불투명 컴포넌트 모델링.

- `ggoss-sql-oracle` live_schema
- `ggoss-binary-dotnet` 표면 추출
- **데이터 샘플러**: 섹션 8 전체 구현
  - 자동 채취 (최초 분석 시)
  - PII 마스킹 파이프라인
  - `data_samples`, `data_query_log` 테이블
  - 컬럼 통계 수집
- **Data 탭 GUI**: 테이블 목록, 샘플 보기, 민감 표시
- MCP 도구: `get_data_entity`, `get_sample_data`, `get_column_stats`, `query_data`, `search_data`

**DoD**: Claude Code에서 "Orders 테이블 샘플 보여줘" → 마스킹된 10행 반환. 직접 쿼리 "WHERE status='PENDING' 카운트" 동작.

#### Week 6 — 런타임 + 교차 검증 + 요약

**목표**: OTel 통합, L1~L3 요약, Findings.

- `ggoss-runtime-otel` OTLP 수신 + PII scrubber
- Merge 엔진 v2 (시간 기반 증분, 6종 Finding 중 4종)
- **Extractor 에이전트 + Validator**: L1 요약
- L2, L3 요약
- Findings 탭 GUI
- `list_findings`, `get_module_summary`, `find_runtime_path` MCP 도구

**DoD**: 실제 운영 OTel 엔드포인트 연결 → 7일간 trace 수집 → 엣지 `exercised` 갱신 확인. L1/L2/L3 요약 자동 생성.

#### Week 7 — Plan 모드 + Gate A + 편집 도구

**목표**: 개발 요청의 Plan 단계 전부 동작.

- `submit_plan` MCP 도구
- Plan 저장 + impact_report 생성 + worktree 생성
- **Gate A GUI (React 섬)**: SPEC 편집기, 태스크 카드, 영향도 그래프 (D3)
- 승인·거부·재생성 플로우
- `edit_file_in_worktree` MCP 도구
- `run_in_sandbox` MCP 도구 + Docker 샌드박스
- 샌드박스 allowlist 구현

**DoD**: Claude Code에서 Plan 제출 → 대시보드 Gate A 화면 표시 → 사용자 승인 → Claude Code가 worktree에서 편집 + 샌드박스 빌드 성공.

#### Week 8 — Gate B + GitLab MR + 최종 완성

**목표**: 전체 사이클 완성. PR 생성. Phase 1 종료.

- 자체 리뷰 (정적 린터 통합, 규칙 검사)
- `submit_diff` MCP 도구
- **Gate B GUI**: Diff viewer, 헝크 선택, 자체 리뷰 표시
- GitLab MR 생성 (`python-gitlab`)
- 변경 리포트 Markdown 자동 생성
- 감사 로그 완성 + 감사 로그 검색 GUI
- 안전 원칙 테스트 (위반 시도 차단 확인)
- **최종 E2E 데모**: 실제 운영 레포로 "질의 → 데이터 조회 → Plan → Gate A → 실행 → Gate B → MR" 전체 사이클

**DoD**: 섹션 15.3 체크리스트 전부 통과.

### 15.3 Phase 1 종료 체크리스트

- [ ] Windows Server 또는 Linux Server에 Docker Compose로 배포 성공
- [ ] 로그인·설정·프로젝트 등록·DB 연결 등록 모두 GUI로 가능
- [ ] C# + TypeScript `symbols`, `calls`, `contracts`, `data_access` 동작
- [ ] MSSQL + Oracle 스키마 + 저장 프로시저/패키지 파싱 동작
- [ ] .NET DLL 표면 분석 동작, 불투명 컴포넌트 모델 동작
- [ ] **데이터 샘플 채취 + PII 마스킹 동작**
- [ ] **Claude Code에서 샘플 조회 + 제한 쿼리 실행 동작**
- [ ] OTel 수신기가 실제 trace 수신, PII scrubber 테스트 통과
- [ ] Merge 엔진이 6종 Finding 중 최소 4종 생성
- [ ] L1~L3 요약 생성, Validator 동작
- [ ] AGENTS.md, `.mcp.json`, Skills 번들 자동 생성
- [ ] MCP 서버 모든 도구 (질의·데이터·개발) 동작, Claude Code 연동 테스트
- [ ] Gate A, Gate B GUI 동작, 전체 워크플로우 E2E 성공
- [ ] **GitLab MR** 이 실제 생성됨
- [ ] 감사 로그에 의무 이벤트 모두 기록
- [ ] 안전 원칙 3종 자동 검증 통과
- [ ] 실제 운영 대상 레포 8시간 내 1차 분석 완료
- [ ] **등록 이후 상시 모드 동작** (GitLab webhook → 증분 분석 → 지식 갱신)

### 15.4 Phase 2 이후로 미룬 것 (명시)

- Java, Python, 네이티브 바이너리 분석기
- PostgreSQL, MySQL, MongoDB 지원
- Cursor/Copilot/Aider 설정 파일 export
- 완전한 증분 재분석 (Phase 1은 파일 단위만)
- 다중 사용자 + 역할 기반 권한
- 도식화 자동 생성 (Mermaid dynamic)
- L4, L5 요약
- 설명형·계획형 고급 질의
- Kubernetes 배포, 멀티 호스트
- 변경 리포트 PDF
- ADR/위키 자동 주입
- 벡터 인덱스 성능 튜닝 (ivfflat → HNSW 등)
- 샘플 데이터 버전 이력 조회

---

## 16. GUI 화면 명세

### 16.1 사이드바 네비게이션

8개 메뉴:
1. **Dashboard** — 전체 상태 요약
2. **Projects** — 프로젝트 목록·등록
3. **Analysis** — 분석 실행·이력
4. **Data** — DB 엔티티·샘플 브라우저
5. **Plans** — 개발 요청 Gate A
6. **Diffs** — 변경 Gate B
7. **Findings** — 불일치·이상 목록
8. **Audit** — 감사 로그 검색
9. **Settings** — 플랫폼 설정 (LLM, OTel, 사용자)

### 16.2 Projects 탭

- 프로젝트 카드 그리드: 이름, GitLab 링크, 언어, 마지막 분석 시각, drift 점수(문서-코드 일치율)
- **[+ 새 프로젝트 등록] 버튼** → 모달:
  - GitLab 저장소 선택 (API로 목록 조회)
  - 기본 브랜치 선택
  - 언어 체크박스 (C#, TypeScript)
  - DB 연결 (기존 등록된 것 중 선택, 없으면 [+ DB 추가] 버튼으로 즉석 추가)
  - [등록] → 자동으로 첫 분석 시작

### 16.3 DB 접속 정보 관리 (Settings > Connections)

- DB 연결 카드: 라벨, 종류(MSSQL/Oracle), 마지막 테스트 결과
- **[+ 새 DB 연결] 버튼** → 모달:
  - 종류 (MSSQL / Oracle)
  - 라벨 (사람이 읽기 쉬운 이름)
  - 접속 문자열 또는 host/port/database/user/password 입력
  - read-only 계정 경고 안내
  - [연결 테스트] 버튼 → 즉시 검증
  - [저장] (암호화 저장)
- 기존 연결 수정·삭제·재테스트
- 민감 테이블 지정 (정규식 + 컬럼 마스킹 규칙)
- 메인터넌스 윈도우 설정 (cron UI)
- Oracle AWR 사용 동의 체크박스

### 16.4 Analysis 탭

- 분석 이력 리스트: 시작 시각, 종료, 상태, 트리거(manual/webhook/scheduled), 통계 요약
- **[분석 시작] 버튼** + 스케줄 설정 (cron UI)
- 특정 런 클릭 → 상세 화면:
  - 진행 로그 (SSE 실시간)
  - 생성된 노드·엣지 통계
  - 실패 로그
  - [취소] 버튼 (진행 중인 경우)

### 16.5 Data 탭 (v2 신규, 핵심)

- 좌측: DB 트리 (Database > Schema > Table/View)
- 각 테이블에 샘플 사용 가능 여부 아이콘
- 테이블 클릭 시 우측:
  - **스키마 뷰**: 컬럼, 타입, 제약, 인덱스 목록
  - **샘플 뷰**: 마스킹된 N행 테이블, 마스킹 표시
  - **통계 뷰**: 컬럼별 null 비율, distinct count, 값 분포
  - **사용처**: 이 테이블을 읽는/쓰는 Symbol 목록 (그래프에서)
  - **[샘플 재수집] 버튼**
  - **[민감 표시] 토글**
- **[쿼리 실행] 버튼** → 쿼리 편집기:
  - SELECT 전용
  - 자동 LIMIT 추가 표시
  - [purpose 입력 필드] 필수
  - 실행 결과 테이블 (마스킹 적용됨)
  - 최근 쿼리 이력

### 16.6 Plans 탭 (Gate A)

- 대기중 / 승인 / 거부 / 진행중 / 완료 상태별 리스트
- 대기중 클릭 → **Gate A 화면 (React)**:
  - 좌: SPEC 마크다운 편집기 (실시간 편집 가능)
  - 중앙: 태스크 카드 리스트 (드래그·삭제·복제·분할)
  - 우: 영향도 그래프 (D3 force-directed, 빨강=직접·주황=전이·점선=불투명)
  - 하단: 예상 토큰·시간·리스크 배지
  - 액션: [승인하고 실행] / [피드백 재생성] / [폐기]
- Claude Code에게 승인 결과 전파

### 16.7 Diffs 탭 (Gate B)

- 대기중 Diff 리스트 + Plan별 그룹
- 클릭 → **Gate B 화면 (React)**:
  - 좌: 태스크 리스트 (상태 아이콘)
  - 중앙: Diff viewer (파일별 탭, 헝크별 체크박스)
  - 우: 자체 리뷰 Findings + 테스트 결과
  - 액션: [전체 승인 → MR 생성] / [선택 헝크 커밋] / [재작업 요청] / [폐기]
- MR 생성 시 GitLab 링크 표시

### 16.8 Findings 탭

- 심각도·상태·종류 필터
- 리스트 + 상세 뷰 (근거 그래프 조각 포함)
- 상태 변경 버튼: [확인함] / [해결됨] / [오탐]

### 16.9 Audit 탭

- 시간 범위·actor·action 필터
- 감사 로그 테이블 (페이징)
- 상세 확장: `details` jsonb 표시
- CSV 내보내기

### 16.10 Settings 탭

- **Connections**: DB 연결 (16.3)
- **LLM**: Anthropic API 키 등록, 모델 선택, 기본 샘플링 설정
- **GitLab**: base URL, admin token, webhook secret
- **OTel**: 수신 엔드포인트, 샘플링률
- **Users**: 사용자 추가·API 키 발급
- **Masking Rules**: 전역 PII 패턴 (기본 제공 + 사용자 추가)

---

## 17. 미결정 사항

개발 착수 전 확정 필요 / 또는 착수하면서 결정:

- 샘플 데이터 기본 크기 (기본 10행 권장, 사용자 재정의)
- L1 요약에 쓸 Claude 모델 (Sonnet 권장, Haiku는 품질 미달 예상)
- 임베딩 모델 (초기에는 Voyage `voyage-code-3` 유료 API, 나중에 로컬 이전)
- 감사 로그 보관 기간 (기본 1년 제안)
- 운영 DB 조회 기본 메인터넌스 윈도우 (사용자가 DB 등록 시 지정)
- 웹 대시보드의 React 섬 빌드 도구 (Vite 권장)
- 백업 정책 (Postgres pg_dump 일 1회 권장)

---

## 18. AI 구현 가이드라인

### 18.1 구현 순서 준수

섹션 15 주차 순서 엄수. Week 4 기능을 Week 2에 넣지 말 것. 의존성 역전 방지. 불확실하면 "이 기능은 Week N 범위입니까?" 질문.

### 18.2 스키마 변경 절차

- DB 스키마는 반드시 Alembic migration 추가 (직접 수정 금지)
- MCP 도구 스키마 변경은 버전 bump
- 분석기 출력 스키마 변경은 `schema` 커맨드 결과 동시 업데이트

### 18.3 테스트 요구

- **단위 테스트**: 분석기 파싱 로직, Merge 엔진, 마스킹 파이프라인
- **통합 테스트**: 샘플 레포 2종을 고정 입력으로 E2E
- **MCP 계약 테스트**: `mcp` SDK 테스트 하네스
- **안전 테스트**: DB·소스·런타임 격리 위반 시도 차단 확인
- **마스킹 테스트**: 실제 PII 포함 샘플을 넣고 유출 여부 검증

### 18.4 성능 기록

모든 분석기·LLM·MCP·DB 쿼리 소요 시간 기록. Phase 1 후반 튜닝 자료.

### 18.5 문서 동기화

- 이 문서 변경 시 커밋에 `[spec]` 태그
- 구현 중 애매한 부분 발견 시 **이 문서 업데이트 PR 먼저**, 구현 PR 나중
- 스펙 수정 없이 자의적 변경 금지

### 18.6 Claude Code 협업 프로토콜

- 한 번에 한 주차 분량만 범위로 지정
- 티켓 단위로 쪼개어 순차 요청
- 매 티켓 완료 시 사람이 diff 리뷰
- 주 금요일에 통합 테스트 + 데모

### 18.7 모르면 질문

다음 상황에서 구현 진행 전 반드시 질문:

- 스펙 조문끼리 상충해 보일 때
- "최선을 다해 추측"해야 할 때
- 외부 라이브러리 선택이 성능·보안에 큰 영향
- 안전 원칙(14장)과 효율이 충돌할 때 — **반드시 질문, 자의적 판단 금지**

---

## 부록 A — 디렉토리 구조

```
platform/
├── PLATFORM_SPEC.md                # 이 문서
├── README.md
├── docker-compose.yml
├── docker-compose.override.yml     # 로컬 개발용
├── .gitlab-ci.yml                  # 자체 GitLab CI
│
├── server/                         # Python 서버 (단일 프로세스)
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   └── versions/
│   ├── app/
│   │   ├── main.py                 # FastAPI 진입점
│   │   ├── api/                    # HTTP 라우터
│   │   │   ├── auth.py
│   │   │   ├── projects.py
│   │   │   ├── analysis.py
│   │   │   ├── data.py             # 데이터 조회 API
│   │   │   ├── plans.py
│   │   │   ├── diffs.py
│   │   │   ├── findings.py
│   │   │   ├── audit.py
│   │   │   ├── settings.py
│   │   │   └── webhooks.py
│   │   ├── mcp/                    # MCP 서버
│   │   │   ├── server.py
│   │   │   ├── tools_query.py
│   │   │   ├── tools_data.py
│   │   │   └── tools_dev.py
│   │   ├── orchestrator/           # ARQ 작업
│   │   ├── analyzers/              # 분석기 Runner (subprocess 래퍼)
│   │   ├── merge/                  # Merge & Reconcile
│   │   ├── extractor/              # L1~L5 요약
│   │   ├── data_sampler/           # 샘플링 + 마스킹
│   │   ├── runtime_receiver/       # OTel 수신기
│   │   ├── sandbox/                # Docker 샌드박스
│   │   ├── gitlab_client/          # GitLab API
│   │   ├── models/                 # SQLAlchemy
│   │   ├── safety/                 # 정책 강제
│   │   ├── artifacts/              # AGENTS.md 등 생성
│   │   ├── audit/                  # 감사 로거
│   │   └── dashboard/              # HTMX 템플릿
│   ├── react_islands/              # Gate A/B 등 React
│   │   ├── gate-a/
│   │   ├── gate-b/
│   │   └── data-viewer/
│   └── tests/
│
├── analyzers/
│   ├── ggoss-csharp/
│   │   ├── Dockerfile
│   │   ├── src/
│   │   └── tests/
│   ├── ggoss-ts/
│   │   ├── Dockerfile
│   │   ├── src/
│   │   └── tests/
│   ├── ggoss-sql-mssql/
│   │   ├── Dockerfile
│   │   └── src/
│   ├── ggoss-sql-oracle/
│   │   ├── Dockerfile
│   │   └── src/
│   └── ggoss-binary-dotnet/
│       ├── Dockerfile
│       └── src/
│
├── infra/
│   ├── docker/
│   ├── scripts/
│   └── docs/
│
└── docs/
    ├── user-guide/
    ├── operator-guide/
    └── api/
```

---

## 부록 B — 첫 주 티켓 초안

Claude Code 에게 직접 넘길 수 있는 수준의 티켓:

### TKT-001: 저장소 및 Docker Compose 초기 설정
- **목표**: 로컬에서 `docker compose up` 으로 postgres + redis + platform 컨테이너가 모두 뜨고 헬스체크 통과
- **산출물**: 루트 `docker-compose.yml`, `server/Dockerfile`, `.env.example`, README 기본
- **DoD**: `curl http://localhost:16401/api/v1/health` → 200 OK

### TKT-002: Alembic 초기화 + 섹션 12.1 테이블
- **목표**: `users`, `api_keys`, `platform_settings`, `secrets` 테이블 생성
- **산출물**: `alembic/versions/0001_initial.py`
- **DoD**: `alembic upgrade head` 성공. 각 테이블에 테스트 데이터 insert/select 동작

### TKT-003: 인증 시스템 (로그인/세션)
- **목표**: 사용자명/비밀번호 로그인, 세션 쿠키 발급, `/api/v1/auth/me`
- **산출물**: `api/auth.py`, password 해싱 (`passlib`), 세션 저장 (Redis)
- **DoD**: POST /login 성공 후 /me 가 사용자 반환. 로그아웃 동작.

### TKT-004: 대시보드 레이아웃 + 로그인 화면
- **목표**: HTMX 기반 레이아웃. 로그인 화면. 로그인 후 사이드바 표시.
- **산출물**: `templates/base.html`, `templates/login.html`, 사이드바 컴포넌트
- **DoD**: 브라우저에서 로그인 시도 → 성공 후 대시보드 진입

### TKT-005: Secrets 관리 GUI + API
- **목표**: 비밀값 (DB 접속 등) 등록·수정·삭제·테스트 GUI
- **산출물**: `api/secrets.py`, 암호화 유틸 (Fernet), Secrets 페이지
- **DoD**: 브라우저에서 DB 접속 정보 추가 → `[테스트]` 버튼 → MSSQL 성공/실패 메시지 표시

### TKT-006: Projects CRUD + 등록 화면
- **목표**: 프로젝트 등록 GUI (GitLab 저장소 검색 포함)
- **산출물**: `api/projects.py`, `gitlab_client/`, Projects 페이지
- **DoD**: 브라우저에서 프로젝트 등록 → DB에 레코드 생성 → 대시보드에 표시

### TKT-007: 감사 로그 기본
- **목표**: 감사 로그 테이블 + 미들웨어로 주요 이벤트 자동 기록
- **산출물**: `audit/` 모듈, `audit_log` 테이블 migration
- **DoD**: 로그인·설정 변경·프로젝트 등록 모두 `audit_log`에 기록됨

**Week 1 목표**: TKT-001 ~ TKT-007 완료. 금요일 데모: "비어있는 상태에서 시작 → 로그인 → DB 접속 추가 → 프로젝트 등록 → 감사 로그 확인" 전체 클릭 투어.

---

*문서 버전: Phase 1 설계 v2.0*
*확정 사항: 1인 개발, Windows/Linux 호스팅, GitLab 자체 서버, 전 기능 GUI, 지속 운영·데이터 조회·파일 편집 반영*
*다음 개정 조건: 섹션 17 미결정 사항 중 하나라도 결정되면 버전 bump*
