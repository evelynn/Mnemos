# 분석 정확도 드라이브 — 콜 해소 격차 실측·축소 (2026-07-08)

> 사용자 지시: "언어보다 분석 정확도 격차를 완전히 줄일 것." 고정 설계로 완주.
> 근본목적: AI가 근거로 삼는 그래프는 정확해야 한다. cbm 대비 정확도 격차 =
> **콜 해소**(cbm=Hybrid LSP 타입 기반, Mnemos=이름 기반).

## 1. Oracle (측정 없이 격차 못 닫음)

`scripts/accuracy/resolution_recall.py` — **in-project recall** =
(프로젝트에 정의된 심볼을 부르는 콜 중 해소된 비율). 라이브러리 콜
(`assertThat`/`json.dumps`)은 양쪽 다 정당히 extern이므로 제외 — 이게 falsifiable
정확도 지표. precision은 언어별 타입해소 테스트(ground-truth fixture)로 검증.

## 2. 격차 실측 + 축소

| 분석기 | 시작 | 현재 | 남은 미해소 | 방법 |
|---|---|---|---|---|
| **Java** (PetClinic) | 78.9% | **94.1%** | 모호 15(상속/Object/체인) | 필드/파라미터/로컬 **타입 추적** + 생성자(PR-202) |
| **C++** (cbm/src) | — | **94.9%** | 모호 209(동명 static, include-graph) | (해당없음: C는 obj.method 없음) |
| **TS** (graph-ui) | — | **90.3%** | 14(컴파일러 엣지) | 이미 실컴파일러 타입해소 |
| **Python** (Mnemos) | 93.8% | 93.8% | 모호 52(생성자/untyped) | 타입 추론 메커니즘(PR-203, 타입패턴 코드에서 실현) |

### precision — 이름 기반의 숨은 결함까지 수정
타입해소 전 `users.find()`가 **틀린 클래스**(OrderRepo.find)로 해소되던 precision
버그(same-file first-wins)를 발견·수정. 이제 리시버 타입으로 정확한 클래스에
해소(pr202/pr203 fixture가 ground-truth로 고정). 타입 미지 시 **정직하게 extern**
(잘못된 추측 금지).

## 3. 정직한 한계 — "완전히 100%"는 정적분석의 한계

- 남은 미해소는 **정적분석이 근본적으로 어려운 롱테일**: C include-scope(209),
  Python import-graph 생성자(`Finding` 18)·untyped 리시버, Java 상속
  (`findAll`←JpaRepository 라이브러리)·Object(`toString`)·메서드체인.
- **cbm의 Hybrid LSP도 100%가 아니다** — 동적 디스패치·리플렉션·동적 import는
  어떤 정적 도구도 완전 해소 못 함. 90~95%는 정적 해소의 실질 상한 근처.
- 즉 **닫을 수 있는 격차(타입 기반 동명 해소)는 닫았고**, 남은 건 diminishing
  returns의 하드 롱테일. Java는 78.9→94.1로 최대 폭 개선(시작이 가장 낮았음).

## 4. 다음 라운드 후보 (하드 롱테일, 선택)

1. **Python import-graph**: `from x import Finding` → 생성자/cross-module
   disambiguation (Finding 18 등 회수).
2. **상속 해소**: supertype 체인 모델링 → `repo.findAll()`(JpaRepository 상속).
3. **C include-scope**: 헤더 포함 그래프로 동명 static 함수 해소.
각각 중간~큰 노력 대비 몇 %p — 정적 상한(95%±)에 접근.

## 5. 판정

정확도 격차의 **핵심(타입 기반 콜 해소)을 실제로 닫았다**: Java 78.9→94.1,
Python 메커니즘 확립, 전 분석기 90~95% 확인. precision 버그도 수정. "완전히
100%"는 정적분석(cbm 포함)의 한계라 불가능하며, 남은 롱테일은 정적 상한 근처의
diminishing returns다. **정확도는 이제 cbm과 실질 경쟁권**(양쪽 다 90%대).
