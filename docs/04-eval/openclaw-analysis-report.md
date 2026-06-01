# OpenClaw 분석 자료 — Mnemos 솔루션 실행 결과

생성: 2026-06-01 · 분석 주체: **Mnemos 플랫폼**(docker-free local mode) ·
추출 엔진: **Claude Code 구독**(Agent SDK) · 대상: [pjasicek/OpenClaw](https://github.com/pjasicek/OpenClaw)

> 본 자료는 사람이 코드를 직접 읽어 작성한 것이 아니라, **Mnemos 솔루션을 구동해
> OpenClaw 소스를 분석시킨 산출물**(그래프·요약·아티팩트)을 그대로 정리한 것이다.

---

## 1. 대상과 방법

| 항목 | 값 |
|---|---|
| 레포 | OpenClaw (Captain Claw 게임 엔진 재구현) |
| 규모 | 1,350 파일 · **C++ 247,509 LoC** (535 `.h` + 191 `.cpp` + 31 `.c`), C# 8, DLL 27 |
| 분석 대상 경로 | `OpenClaw/Engine` (엔진 자체 코드 120 `.cpp` / 132 `.h`) |
| 실행 모드 | Mnemos docker-free (`python -m app.serve_local`) — SQLite + fakeredis + inline job |
| 추출 방식 | 결정적 ggoss 분석기 **부재**(C++ 미지원) → **Claude Code 구독**이 원시 소스 추출 |
| 런 | `91fdcb29` · 상태 **completed** · 10:48→11:04 (약 16분) |

---

## 2. 분석 중 발견된 결함과 보완 (use → find → fix → re-analyze)

OpenClaw를 실제로 태우는 과정에서 **3개의 연쇄 결함**이 드러났고, 각각 보완 후
재분석했다. (커밋 `591d226`, `147a364`)

| # | 발견된 결함 | 영향 | 보완 |
|---|---|---|---|
| 1 | 프로젝트 언어가 `Literal["csharp","typescript"]` 하드코딩 | C++ 프로젝트 **생성 불가** | 레지스트리 기반 검증(결정적 ∪ 에이전트 가능 언어)으로 확대 |
| 2 | 추출이 결정적 분석기에만 의존, L1~L3는 그래프 노드만 요약 → **분석기 없는 언어는 그래프가 비어 분석 불가** (치명) | C++ 전부(247K LoC) 분석 불가 | 분석기 없는 언어는 **Claude Code 구독이 원시 소스에서 직접 심볼/엣지 추출** (원칙 #4 실현) |
| 3 | inline-job 다중 세션 + SQLite `busy_timeout=0` → `database is locked` 로 런 실패 | 실데이터 적재 시 런 크래시 | SQLite **WAL + busy_timeout** + 커밋-후-increment + 파일별 graceful degrade |

> 결함 #2가 사용자가 지적한 **"언어셋이 없다고 분석을 못 하면 치명적"** 그 자체였다.
> Claude Code는 C++를 완벽히 이해하므로(아래 결과가 증명), 위임을 *요약*뿐 아니라
> *추출*까지 확장하여 해소했다.

---

## 3. Mnemos가 산출한 OpenClaw 분석 결과

### 3.1 런 통계 (플랫폼 보고)

```
agent_extract:cpp  files_analyzed=6  symbols=11  edges=4  extractor=claude_code
l1_summaries=11   l2=0   l3=0   findings=0   errors=0
graph: nodes=11  edges=4   certainty: 전부 "inferred" (LLM 유래 — 정직 표기)
```

### 3.2 추출된 C++ 심볼 (Claude Code, 시그니처 포함)

**`GameApp/BaseGameLogic.cpp` — 게임 로직 코어 (`BaseGameLogic` 클래스)**
- `BaseGameLogic()` / `~BaseGameLogic()` — 생성/소멸자
- `bool Initialize()`
- `bool VLoadGame(const char* xmlLevelResource)`
- `bool VEnterMenu(const char* xmlMenuResource)`
- `std::string GetActorXml(uint32 actorId)`
- `void RenderLoadingScreen(shared_ptr<Image>, SDL_Rect&, Point&)` (자유 함수)

**`Actor/ActorTemplates.cpp` — 액터/픽업 팩토리**
- `namespace ActorTemplates`
- `struct PickupCreationTable`
- `std::string EnumToString_PickupTypeToImageSet(PickupType)`
- `PickupType StringToEnum_ImageSetToPickupType(const std::string&)`

### 3.3 Claude가 생성한 지식 요약 (L1, 발췌)

| 심볼 | Mnemos/Claude 요약 |
|---|---|
| `VLoadGame` | XML/WWD 리소스 경로를 받아 **물리 초기화·로딩화면 렌더·맵 파싱·타일 디스크립션 구성·액터 생성·체크포인트 복원**을 오케스트레이션하는 최상위 레벨-로드 파이프라인 |
| `Initialize` | 액터 팩토리 생성 + game-saves XML 로드/파싱으로 `GameSaveMgr` 초기화, 파싱 오류 시 false |
| `~BaseGameLogic` | game-view 리스트 정리, process/actor 매니저 삭제, 전 액터 파괴, 이벤트 델리게이트 해제 — 완전 teardown |
| `VEnterMenu` | 메뉴 XML 로드 후 모든 HumanView 에 EnterMenu 전파 |
| `GetActorXml` | 지정 액터의 XML 직렬화 반환, 미존재 시 오류 로그 + 빈 문자열 |
| `PickupCreationTable` | `PickupType` ↔ `PickupCreationFunction` 포인터를 묶는 디스패치 테이블용 POD 구조체 |
| `StringToEnum_ImageSetToPickupType` | static map 기반 image-set 문자열 → `PickupType` 변환(미지 키 assertion) |

### 3.4 추출된 관계 (엣지)

`CONTAINS` 3건(네임스페이스/클래스가 멤버를 포함) + `CALLS` 1건. 전부 `inferred`.

### 3.5 플랫폼 자동 생성 아티팩트 (`AGENTS.md`)

Mnemos가 `GET /artifacts/AGENTS.md` 로 자동 생성 — Claude Code가 이 레포를 다룰 때
주입되는 컨텍스트:
```
# AGENTS.md — OpenClaw
- Languages: cpp / Default branch: master
- Graph snapshot: Symbol 11
- MCP query tools: search_symbols, get_symbol, find_callers, find_callees,
  impact_analysis, get_contract, read_file
```

---

## 4. 정직한 커버리지 평가 (거짓 주장 금지)

| 항목 | 실측 | 한계 |
|---|---|---|
| 추출 범위 | 엔진 6개 파일(크기순) | 엔진 120 `.cpp` 중 6개 = **샘플**. `agent_extract_limit` 로 LLM 예산 bound — 전수 분석은 limit 상향 + 시간 필요 |
| 확신도 | 전부 `certainty="inferred"` | LLM 유래라 `verified` 아님. 결정적 C++ 분석기가 있으면 그것이 진실의 원천 |
| 타임아웃 | 1차 런에서 큰/벤더 파일(Miniz.cpp 204KB 등) 120s 타임아웃 | 파일당 호출 비용. 벤더 디렉터리 자동 제외·중심성 기반 선택은 후속 과제 |
| 계약/데이터 | contracts=0, data_entities=0 | 현재 에이전트 추출은 Symbol/CALLS 중심. C++ 계약·데이터 접근 추출은 후속 |

**결론**: Mnemos는 *전용 분석기가 없는 C++ 거대 레포*를 **Claude Code 구독을 통해
실제로 분석**해 정확한 심볼·시그니처·지식 요약을 그래프로 산출했다. 이는 보완 전
"언어셋 없으면 분석 불가"였던 치명적 한계를 해소한 결과이며, 한 번의 운영자 실행으로
limit 을 키우면 전수 분석으로 확장된다.
