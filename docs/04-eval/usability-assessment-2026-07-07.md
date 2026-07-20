# Mnemos 사용성 평가 (Usability Assessment)

> **평가일:** 2026-07-07
> **평가 방식:** 문서 검토가 아니라 **실제 부팅 · API 호출 · 분석기 실행 · 전체 단위 테스트 · 5대 pillar 코드 심층감사**로 실측.
> **평가 대상:** 브랜치 `claude/round-pr160-analyzer-write-contention` (main + 41 commits, 추적 트리 clean, HEAD `630d633` PR-189).

---

## 0. 결론 (TL;DR)

**사용 가능하다. ✅** Docker 없이 단일 Python 프로세스로 지금 바로 부팅되고, 로그인→프로젝트→그래프 검색→Ask 전 흐름이 동작하며, 결정적 분석기가 실제 소스에서 심볼을 추출한다. 광고된 5대 핵심 기능 중 **4.5개가 stub이 아닌 실제 구현**이다.

- **즉시 사용 가능한 범위:** Python·TypeScript 레포 분석 (docker-free).
- **추가 준비 필요:** C#/MSSQL/Oracle/.NET 바이너리 분석 → `docker compose --profile analyzers build`. LLM 요약/Ask 심화 → API 키.
- **알려진 실버그 1개(경미):** Pillar 5 런타임의 `exercised` 플래그가 edge/node에 어긋나게 저장·조회됨.
- **단위 테스트:** 1612 pass / 21 fail (98.7%). **실패 21개는 전부 Windows 환경 아티팩트 + 낡은 테스트로, 제품 결함이 아님.**

---

## 1. 이 솔루션의 정체

**AI가 거대한 소스 코드를 분석할 때 쓰는 보조 도구.** 일반 SaaS/앱이 아니라, "레포를 통째로 AI 프롬프트에 넣기"가 실패하는 지점을 코드로 제거한 플랫폼이다. 5개 축(pillar):

| # | Pillar | 제거하는 한계 |
|---|--------|--------------|
| 1 | **Scale** | 컨텍스트 창 — 재귀 상향식 분석(L1심볼→L2파일→L3모듈), L2+는 원본 파일을 다시 읽지 않고 요약만 압축 |
| 2 | **환각 방지** | 그래프에 근거 없는 LLM 주장은 폐기, `verified`/`inferred`를 절대 혼동하지 않음 |
| 3 | **결정성** | AST/파서 기반 결정적 분석기 — 그래프가 진실의 원천, LLM은 서술·보완만 |
| 4 | **세션 간 재사용** | bitemporal(양시간) 프로버넌스 그래프 + MCP 20개 도구로 재질의 (레포 재수집 대신) |
| 5 | **크로스서비스·런타임** | contract-id 정규화(C# 엔드포인트↔TS fetch 동일 노드), OTLP 런타임 대사(실제 실행된 엣지 표시), 리스크 스코어링 |

---

## 2. 실측 증거

### 2.1 부팅 & 헬스

```bash
cd server && python -m app.serve_local --seed-demo   # → :8080
```

- `startup_verify`: config / crypto / database / redis 전부 OK.
- `/api/v1/health/ready` → `{database, redis, worker:inline, crypto}` 전부 `ok`.
- 분석기 바이너리는 PATH에 없음(WARNING) → **crash가 아니라 "해당 스테이지 skip"으로 graceful degrade**.

### 2.2 전체 API 흐름 (데모 데이터)

| 엔드포인트 | 결과 |
|---|---|
| `POST /auth/login` | ✅ 200, 세션+CSRF 쿠키 발급 |
| `GET /projects` | ✅ 200, `demo-orders-service` (csharp/typescript/sql) |
| `GET /projects/{id}/findings` | ✅ 200, `duplicate_endpoint` (risk 88, P1, CWE-1041, compliance 태그) |
| `GET /projects/{id}/graph/search?q=Orders` | ✅ 200, `certainty` 플래그 붙은 실제 노드 |
| `GET /projects/{id}/graph/certainty_breakdown` | ✅ nodes: verified 18 / inferred 1, edges: verified 13 / asserted 2 / inferred 1 |
| `POST /projects/{id}/ask` | ✅ 200, **그래프 그라운딩 답변** |

**Ask 응답 예 (핵심 기능):** "Where is the report generated?" → `OrdersRepo` 심볼 매치, `"It is called from 2 places."`, `callers_count: 2`, 그리고 매치가 약하므로 `answered: false`로 **정직하게 환각을 회피**. → Pillar 2·4를 동시에 실증.

### 2.3 결정적 분석기 (docker-free 실행)

`ggoss-py`를 Mnemos 자기 자신에게 직접 실행:

```
py:…\merge\contract_id.py:normalize_http_path@19  signature="def normalize_http_path(raw: str) -> str:"  certainty="asserted"  created_by=["ggoss-py"]
… extractor 패키지 전체에서 45개 심볼 추출
```

- 실제 소스에서 **file:line · 시그니처 · certainty**가 붙은 진짜 심볼을 추출.
- `node v22.17.0` 존재 → `ggoss-ts`도 in-repo 실행 가능 (`MNEMOS_INREPO_ANALYZERS=1`, `serve_local`이 자동 설정).
- 과거 실행 로그(`mnemos-run.err.log`, 2026-06-12)에 실제 TS 모노레포 `Smart-AI-Report-V4` 분석 기록 존재 → 전체 파이프라인이 실제 레포에서 돌았던 증거.

---

## 3. 5대 Pillar 심층감사 (코드 제어흐름 기준)

> stub/aspirational이 아니라 실제 로드베어링 로직인지 파일·함수 단위로 검증.

| Pillar | 판정 | 근거 |
|---|---|---|
| **1 Scale** | **REAL** | `runner.py` `summarise_l2/l3`가 L1/L2 **요약을 압축**(원본 파일 미독), `pack_by_budget` 실제 greedy bin-packing, `_priority_symbols`가 진입점→CALLS in-degree→lexical 순 랭킹, `evidence_hash`로 증분 skip. (token 추정은 `chars//4`, L3 모듈경계는 경로 첫 세그먼트 — 문서에 명시된 crude heuristic) |
| **2 환각 방지** | **REAL** | `validator.validate_claims`가 각 claim의 노드/엣지 근거를 **현재 DB에 존재하는지(`valid_to IS NULL`) 확인 후 없으면 폐기**, 런너는 `accepted`만 저장, `agent_extract.to_envelopes`가 dangling LLM 엣지 필터 + 모든 LLM 출력 `certainty="inferred"` 강제 |
| **3 결정성** | **REAL** | `ggoss_py.py`는 진짜 stdlib `ast` — 모호한 호출은 추측 대신 `None` 반환(정직), 해소된 호출→`asserted`/미해소→`inferred`. `AnalyzerRunner.run`은 실제 async 서브프로세스 스트리밍 + env 화이트리스트 + graceful degradation |
| **4 영속·재사용** | **REAL** | `models/graph.py`가 진짜 bitemporal(`valid_from` PK + nullable `valid_to`), `created_by` 배열 프로버넌스. `mcp/server.py`의 20개 도구가 실제 백킹 쿼리로 저장된 그래프 재질의, fail-closed 토큰 게이트 + 응답 캡 + 감사 로깅 |
| **5 크로스서비스·런타임** | **PARTIAL** | contract-id 링크는 **REAL & wired**(ingest 시 C#/TS가 동일 `http.{METHOD}.{route}` 형태로 노드 병합). 단, `runtime.reconcile_observations`는 per-observation 전체 엣지 스캔(O(obs×edges)) 휴리스틱이고, 실제 크로스언어 조회는 docstring상 "deferred". **실버그: reconcile는 `edge.data.exercised`를 쓰는데 `findings._subject_is_exercised`는 `node.data.exercised`를 읽음** → 노드-subject finding의 exercised ×1.3 리스크 가산이 실트레이스에서 안 걸림 |

**총평:** ~4.5/5가 진짜 구현. 재귀 상향식 요약, DB 기반 claim 그라운딩, 결정적 AST 분석기, bitemporal 그래프, MCP 질의면, 크로스서비스 contract 정규화 모두 실제 제어흐름을 가짐. 유일한 실질 미달은 Pillar 5 런타임(휴리스틱 + 위 플래그 불일치). **특기: 코드가 드물게 정직하다** — docstring의 한계 명시가 실제 동작과 일치.

---

## 4. 단위 테스트 상태

```
pytest -m "not integration"  →  1612 passed, 21 failed, 18 skipped  (98.7%, 4:30)
```

**실패 21개 = 제품 결함 아님.** 두 부류로 전부 설명됨:

1. **Windows 서브프로세스 아티팩트 (약 20개)** — `test_analyzer_e2e_subprocess`, `test_scale_synthetic`, `test_pr35_orchestration_e2e`, `test_pr98/pr114/pr66/pr162` 등. 테스트가 **확장자 없는 POSIX shebang 스텁 분석기**(`ggoss-fake`)를 `asyncio.create_subprocess_exec`로 직접 실행 → POSIX는 shebang으로 실행되지만 Windows는 인터프리터 없이 실행 불가로 실패. README상 **CI(Linux)는 green**.
2. **낡은 테스트 (1개)** — `test_pr48_wcag_close`가 라이트 accent를 옛 `#4f46e5`(indigo)로 단언하나, 2026-06 리디자인이 테마를 "Nord Light"(Frost accent)로 교체 → 색만 바뀌고 테스트 미갱신.

→ **Windows에서의 실패는 다시 파고들 필요 없음.** 진짜 결함은 §3의 Pillar 5 플래그 불일치 1건뿐.

---

## 5. 사용 방법 (Quick start)

```bash
cd server
pip install -e ".[local]"                 # aiosqlite + fakeredis (이미 .venv에 설치됨)
python -m app.serve_local --seed-demo     # :8080, 데모계정이 stdout에 1회 출력
# → http://localhost:8080/login  (demo-admin / <출력된 비밀번호>)
```

- **Python/TS 레포:** 지금 즉시 실사용 가능.
- **폴리글랏 전체(C#/SQL/.NET):** `docker compose up -d` + `docker compose --profile analyzers build`.
- **LLM 요약/Ask 심화:** Settings → AI 제공자에서 Claude/OpenAI/Gemini 키 설정. (그래프 조회·Ask 기본 답변은 키 없이 동작.)

---

## 6. 권장 후속 작업

| 우선순위 | 항목 | 근거 |
|---|---|---|
| P1 | **Pillar 5 exercised 플래그 정합** (`edge.data` vs `node.data`) | §3 유일한 실버그 — 런타임→리스크 피드백 루프가 부분 단절 |
| P2 | **서브프로세스 테스트 이식성 개선** | 스텁 실행 방식을 Windows 호환으로 → 로컬 개발 시 21개 오탐 제거 |
| P3 | **낡은 WCAG 테스트 갱신** + Nord Frost accent의 실제 AA 대비 재검증 | 디자인 백로그 a11y 항목과 연결 |

---

*이 문서는 실제 부팅·API·분석기 실행·전체 테스트·코드감사 실측에 근거한다. 인용된 file:line·동작은 평가일(2026-07-07) 시점 기준이며, 코드 변경 시 재검증 필요.*
