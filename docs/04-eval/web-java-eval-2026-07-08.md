# 웹+Java 집중 평가 및 업그레이드 방향 재설정 — 2026-07-08

> 선행: `agent-context-real-repo-retest-2026-07-07.md` (C/C++ 커버리지 회복),
> 그리고 codebase-memory-mcp와의 전면 비교(158언어 전장에서 Mnemos 열세).
> 본 문서는 전장을 **웹 스택 + Java**로 좁혀 실측하고, 결과에 따라 업그레이드
> 방향을 재설정한 뒤 P0(ggoss-java)를 구현·검증한 기록이다.

## 1. 왜 전장을 좁혔나

전면 비교(9축 가중)에서 codebase-memory-mcp **8.8 / Mnemos 6.4**. 격차의 핵심은
언어 폭(160 vs 6)·속도(C vs Python)·성숙도였고, Mnemos가 "고유"로 믿었던 런타임
OTLP·cross-service 링킹조차 상대(`src/traces/traces.h`의 `ingest_traces`,
`pass_cross_repo.c`)가 이미 보유했다.

→ 158개 언어에선 못 이긴다. 하지만 **"웹+Java"는 5~6개 언어짜리 좁은 전장**이고,
여기선 (a) TS/JS가 이미 최상급이고 (b) 큰 구멍이 Java 하나이며 (c) Mnemos의
차별 계층(verified/inferred·L1-L3 요약·그라운딩 챗)이 붙을 여지가 있다.

## 2. 실측 — Mnemos의 웹+Java 현재 커버리지

| 언어 | Mnemos(구) | 실측 근거 |
|---|---|---|
| TS/TSX/JS/JSX | **강함** (실제 TS 컴파일러 타입해소 + Express/NestJS/Next 라우트 + EXPOSES/CALLS) | `ggoss-ts` `checker.getSymbolAtLocation` |
| **Java** | **0 (완전 사각)** | PetClinic → 심볼 0 |
| HTML | 0 (지정조차 불가) | SUPPORTED_LANGUAGES 부재 |
| CSS/SCSS | 0 (지정조차 불가) | SUPPORTED_LANGUAGES 부재 |
| Kotlin | LLM-only(inferred, 로컬 불가) | AGENT_LANGUAGE_EXTENSIONS만 |

**결정적 실측:** Spring PetClinic(Java 48 + HTML 12 + CSS 5)을 실제
`run_ingest`로 돌린 결과 → **그래프 전부 0** (`symbols:java` = `no_analyzer`,
`agent_extract:java` = `agent_sdk_unavailable`). Java 웹 백엔드는 out-of-box
100% 사각지대였다.

**핵심 통찰:** Mnemos의 왕관 보석인 cross-service 컨트랙트 링킹(Spring
`@GetMapping` ↔ TS `fetch`)이 Java 웹앱에서 **죽어 있었다** — Java 쪽 노드가
없어 링크 대상이 없었기 때문. ggoss-java는 언어 추가가 아니라 **pillar 5
차별점을 켜는 스위치**다.

## 3. 재설정된 업그레이드 방향

**기존(암묵):** 결정적 analyzer를 대형 C/C++ 엔터프라이즈로 확대(ggoss-cpp로 완료).
**신규:** **웹+Java 엔터프라이즈 스택에 수렴.** 작은 언어셋에서 추출 parity를
달성(TS 완료, Java가 유일한 큰 add, HTML/CSS는 소규모)하고, cbm이 구조적으로 못
하는 계층(그라운딩된 provenance + 런타임 인식 cross-service + LLM 서사/챗)에서
차별화. 158언어를 쫓지 말고 **"Spring+TS 웹 시스템을 위한, 런타임 인식 그라운딩
분석 계층"**을 소유.

우선순위 로드맵:

- **P0 — ggoss-java (본 PR로 완료).** 타입/메서드 + Spring/JAX-RS 컨트랙트 +
  CALLS + JPA data_access.
- **P1 — Java↔TS 컨트랙트-id 링킹 (본 PR에서 활성화 확인).** Spring 컨트랙트를
  `http_endpoint`+`spec`로 방출 → 플랫폼이 TS `fetch`와 같은 `http.<M>.<path>`
  노드로 정규화. **cbm 대비 실질 우위 지점** (여기에 verified/inferred·런타임
  reconcile·그라운딩 서사가 얹힌다).
- **P2 — HTML/CSS 지정가능화** + 고가치 추출(form action/href/Thymeleaf) 만.
- **P3 — Kotlin(JVM 웹)** ggoss-java 확장.
- **P4 — 깊이 parity + 요약 활성화:** Java 크로스파일 메서드 해소(import+FQN),
  추상/인터페이스 메서드(현재 미검출) 보강, LLM 백엔드로 L1-L3/챗 실서술.

## 4. P0 구현 결과 (ggoss-java)

새 파일: `analyzers/ggoss-java/src/ggoss_java.py` (순수 stdlib regex+brace,
의존성 0 — tree-sitter/javalang 미가용 확인 후 무의존 경로 선택),
`analyzers/ggoss-java/Dockerfile`. 등록: registry / runner `_INREPO_ANALYZERS`
/ docker-compose.

추출: `class/interface/enum/record/annotation_type/method`(생성자 태그) +
Spring(`@RestController/@Controller` + `@GetMapping/@PostMapping/…/@RequestMapping`)·
JAX-RS(`@Path`+`@GET/…`) → HTTP 컨트랙트(클래스 prefix 조인) + CALLS(같은파일→
전역유일→`java:extern:`) + JPA(`@Entity/@Table`) data_access.

구현 중 잡은 실버그: 파라미터 애노테이션(`@PathVariable(...) Integer id`)이
있는 메서드에서 애노테이션 이름이 메서드명을 가리는 문제 → 감지 단계에서
애노테이션 블랭킹으로 해결(테스트로 고정).

### PetClinic 재분석 (전/후)

| | 이전 | 이후 |
|---|---|---|
| symbols | **0** | **222** (class 45 · method 174 · interface 3) |
| edges | 0 | 947 (CALLS) |
| contracts | 0 | 16 |
| data_entities | 0 | 6 |
| L1/L2/L3 요약 | 0 | 56 / 9 / 1 |
| findings | 0 | 1 (`duplicate_endpoints`) |
| `symbols:java` | `no_analyzer` | **completed** |

### Ground-truth + 링킹 검증

- OwnerController 메서드 12개 전부 소스와 일치(애노테이션 오검출 0):
  `findOne/create/validate/initCreationForm/processCreationForm/…`.
- 컨트랙트 정규화: `@GetMapping("/owners")` → 노드 `http.GET./owners`.
- **Cross-service 링킹 증명:** `http_contract_id("GET","/owners")` = 저장된 Java
  노드 id와 동일 → TS `fetch('/owners')`가 **같은 노드로 자동 링크**됨. EXPOSES
  엣지가 Java 핸들러 → 컨트랙트 연결.

## 5. 테스트/게이트

- 신규 `tests/test_pr192_java_analyzer.py` (6): 타입/메서드, 파라미터-애노테이션
  회귀, 클래스 prefix 조인 + TS-정규화 동일 노드, CALLS 해소/extern 정직성,
  JPA `@Table` 엔티티, registry.
- 전제 갱신: `test_pr35`(8개 언어), `test_pr100`(8번째 analyzer 이미지).
- 관련 스위트 통과, ruff clean.

## 6. 남은 정직한 한계

- **추상/인터페이스 메서드 미검출** (본문 없는 `findByX(...);`) — Spring Data
  repository 쿼리 메서드가 아직 심볼이 안 됨. P4.
- CALLS는 이름 기반 해소(import/FQN 타입해소 없음) — 오버로드/동명 교차클래스
  미해소. 멤버/함수포인터/메서드레퍼런스 콜 미해소.
- 파라미터화 라우트는 프레임워크별 파라미터명 유지(Spring `{ownerId}` vs TS
  `{id}`) → 파라미터 경로는 링크 안 될 수 있음(정적 경로는 정확히 링크).
- HTML/CSS는 여전히 지정 불가(P2).

## 6.5 P2/P3/P4 구현 완료 (후속)

P2·P3·P4는 파일이 겹치지 않아(P3=별도 ggoss-kotlin, P4=ggoss-java 내부) 순차
구현했다.

- **P2 — ggoss-web (PR-193).** HTML `<form>`/`<a>`의 라우트(action/th:action,
  href/th:href, Thymeleaf `@{…}`)를 HTTP 컨트랙트로 추출 + template 심볼.
  CSS/SCSS는 stylesheet 심볼만(저가치). html/css/scss를 registry/
  SUPPORTED_LANGUAGES에 추가 → **이제 지정 가능**(생성 거부 해소). PetClinic
  재분석: template 17개, **HTML 템플릿 ↔ Java 핸들러 cross-service 링크 3개**
  실확인(`http.GET./owners/new` 등에 HTML CALLS + Java EXPOSES 공존). asset/
  webjars/외부URL/`#`는 제외.
- **P3 — ggoss-kotlin (PR-194).** 별도 분석기(ggoss-java 미수정). `fun` 기반
  함수 감지(톱레벨 포함), class/interface/object/enum/annotation + 중첩 FQN,
  Spring 애노테이션(=Java) → 같은 `http.<M>.<path>` 노드 정규화, CALLS
  same-file→전역유일→extern. kotlin을 결정적 셋에 등록.
- **P4 — ggoss-java 깊이.** (1) **추상/인터페이스 메서드 검출**(`;` 종료, `=`·
  enum·annotation 가드) → PetClinic에서 Spring Data repository 메서드
  `findById`/`findByLastNameStartingWith`/`findPetTypes` 3개가 심볼로(222→225,
  오검출 0). (2) **리시버-클래스 콜 해소**: `Type.method()`가 알려진 프로젝트
  클래스면 그 클래스 메서드로 해소(이름-only fallback보다 정확), `resolution:
  receiver_class` 메타 기록.

결정적 분석기: 6 → **12**(ts,py,cpp,java,csharp,sql + javascript, **html,css,
scss,kotlin**). 신규 테스트 pr193(6)·pr194(5)·pr192 P4(2) 추가. 관련 스위트
통과, ruff clean(전체 스위트의 pr35 D1/D2 실패는 clean HEAD와 동일한 Windows
subprocess 아티팩트, 신규 회귀 0).

## 6.6 재측정 + 비교 점수 재검증 (P0~P4 후)

PetClinic 최종 측정: 심볼 **242**(Java 225 + web 17: class 45·method 177·
interface 3·template 12·stylesheet 5), 추상메서드 3, 컨트랙트 22, DataEntity 6,
CALLS 934, EXPOSES 17, **cross-service 링크 3**, receiver_class 해소 3.
(세션 시작 시 전부 0.)

**웹+Java 전장 한정 재채점** (8축 가중; 158언어 전면전이 아니라 우리가 고른
전장):

| 축 | 가중 | cbm | Mnemos(전) | Mnemos(후) |
|---|---|---|---|---|
| 웹+Java 언어 커버리지 | 15% | 10 | 3 | 8 |
| 추출 깊이·의미정확도 | 15% | 9 | 4 | 6.5 |
| **cross-service 링킹**(client↔server↔template) | 15% | 8 | 3 | **9** |
| 그라운딩·신뢰정직성 | 12% | 8 | 9 | 9 |
| 런타임 인식(OTLP) | 8% | 8 | 8 | 8 |
| 요약·서사 계층 | 10% | 4 | 7 | 7 |
| 규모·성능 | 10% | 10 | 5 | 5 |
| 성숙도·배포 | 15% | 10 | 5 | 5 |
| **가중 총점** | | **8.6** | **5.2** | **7.2** |

격차 **3.4 → 1.4** 로 축소. Mnemos(후)가 **이기는 축**: cross-service 링킹
(9 vs 8), 그라운딩(9 vs 8), 요약 서사(7 vs 4). **무승부**: 런타임(8). **지는
축**: 커버리지·깊이(Hybrid LSP 타입해소 vs regex MVP), 규모(C vs Python),
성숙도(shipped vs pre-1.0) — 남은 격차는 대부분 "시간·엔지니어링으로 메울" 축
이지 접근법의 한계가 아니다.

정직: 전면 158언어전 점수(cbm 8.8/Mnemos 6.4)는 사실상 불변 — Mnemos는 여전히
언어 폭·속도·성숙도에서 뒤진다. 그러나 **우리가 고른 웹+Java 전장에서는 전략이
작동했다**: "Java 웹앱을 아예 못 봄"에서 "그라운딩된 cross-service 계층에서 앞섬"
으로 이동. 미션("웹+Java 엔터프라이즈 분석")에 한정하면 Mnemos는 이제 cbm과
경쟁 가능한 위치다.

## 7. 판정

웹+Java 전장에서 Mnemos는 이제 TS/JS(최상급) + Java(신규 결정적) 추출을 갖췄고,
**Spring 백엔드 ↔ TS 프론트 cross-service 링킹이 실동작**한다. 이는 cbm이 라우트
링킹은 하되 verified/inferred·런타임 reconcile·그라운딩 서사를 못 얹는 지점에서
Mnemos가 차별화할 토대다. 다음은 P2(HTML/CSS 지정가능화)와 P4(추상메서드 +
FQN 해소 + LLM 요약 활성화).
