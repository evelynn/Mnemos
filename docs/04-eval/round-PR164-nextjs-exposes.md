# Round PR-164 — ggoss-ts가 Next.js route handler의 EXPOSES 엣지를 방출 (dogfood)

작성: 2026-06-15 · 브랜치 `claude/round-pr160-analyzer-write-contention` · 이전 commit `7cb5957`
트리거: PR-162/163 dogfood(Smart-AI-Report-V4, Next.js)의 마지막 검증된 격차. 분석 그래프에
EXPOSES 엣지가 **0건**이라 `detect_duplicate_endpoints` + OTLP 런타임 reconcile(PR-159)가 TS
프로젝트에서 영구 무발화였다.

## 발견된 결함 (dogfood 실측)

Smart-AI 분석은 63개 HTTP contract를 뽑았으나 EXPOSES 엣지 **0건**. 원인 — ggoss-ts `cmdContracts`는
세 패턴만 감지했다: NestJS 데코레이터(`@Get` → EXPOSES), Express 라우터(`app.get('/x')` → EXPOSES),
클라이언트 `fetch('/api/..')`(CALLS). **Next.js App Router 패턴**(`app/**/route.ts`의
`export async function GET/POST`)은 감지기가 없었다. 그래서 클라이언트 fetch는 CALLS로 잡혀
contract 노드는 생겼지만, **서버 핸들러(EXPOSES)는 누락** → contract에 exposer가 없어
duplicate-endpoint 탐지·OTLP reconcile가 작동 불가. Next.js는 가장 흔한 TS 백엔드 형태인데도.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 164-1 | `cmdContracts`에 Next.js 감지기 추가: 파일 경로가 `app/**/route.{ts,tsx,js,mjs}`이고 HTTP 메서드명(GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS) 함수를 export 하면, 경로에서 URL을 도출(route group `(x)` 제거, dynamic `[id]`→`{id}`, `[...slug]`→`{slug}`)해 메서드당 contract + **EXPOSES** 엣지 방출. 기존 emitHttpContract/`<caller>` exposer 규약 재사용 | `analyzers/ggoss-ts/src/index.mjs` |
| 164-2 | 회귀 테스트: Next.js route handler EXPOSES, dynamic segment/route group 경로 도출, arrow `export const GET`, 비-route 파일의 GET export 무시 | `tests/test_pr164_nextjs_route_exposes.py` |

## 검증 결과 (실측 — dogfood 재실행 + 단위)

| | fix 전 | fix 후 |
|---|---:|---:|
| 분석 그래프 EXPOSES 엣지 (Smart-AI 전체) | **0** | **143** |
| Next.js handler 감지 (admin 서브셋 smoke) | 0 | 19 |
| contract id 수렴 | client CALLS만 | 동일 id에 CALLS(client) + **EXPOSES(server)** |

게이트: ruff 0 · pytest not-integration **1574 pass / 19 사전존재 Windows-환경**(PR-163 베이스라인
동일집합·회귀 0) · 실분석기 테스트(pr76/75/87/164) GREEN · mypy 불변(Python `app/` 무변경, JS 분석기만).
client fetch는 여전히 CALLS(방향 보존, test_pr76 통과).

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| OTLP runtime correlation | 8.6 | **8.7** | EXPOSES 엣지가 지배적 TS 백엔드(Next.js)에서 처음으로 존재 → PR-159 reconcile이 실제로 붙을 대상이 생김 + duplicate_endpoint 탐지가 TS에서 작동 가능. 0→143 실측. (라이브 OTel 송신자 미연결 한계는 잔존) |

가중평균: OTLP(가중 0.03)×(+0.1) → +0.003 → 약 **91.4/100** (구조적 unblock, 가중 기여 소).

## dogfood 검증 아크 완료
PR-162(정확 카운트) · PR-163(도메인-only 데이터맵) · PR-164(서버 EXPOSES 엣지) 로 Smart-AI dogfood가
드러낸 *코드로 닫을 수 있는* 그래프 결함 3건을 모두 닫음. 남은 격차는 환경의존(라이브 OTel/docker)
또는 설계(보안·인가 finding 룰 — dogfood의 실제 IDOR는 Claude가 잡고 Mnemos는 못 잡음).
