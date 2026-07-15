# cbm 능가 지점 — 분석·비교 계층 (2026-07-08)

> 사용자 지시: "cbm을 능가하도록, 특히 분석이나 비교부분에서."
> 전략: cbm이 이기는 축(속도·언어폭·Hybrid LSP)이 아니라, **cbm이 구조적으로
> 못 하는 분석·비교 계층**을 판다.

## cbm이 구조적으로 못 하는 것

cbm은 빠르고 넓은 **결정적 추출 + 쿼리 엔진**이다. 설계상 다음이 없다:
- **히스토리** — 재인덱싱만 하고 스냅샷을 안 남긴다 → 커밋/런 간 비교 불가.
- **verified/inferred 신뢰 모델** — 전결정적이라 신뢰 구분/그라운딩 개념 없음.
- **LLM 서사·그라운딩 챗** — 호출 에이전트에 위임.
- **findings 리스크 분석** — dead-code는 있으나 schema-mismatch/unverified-claim/
  duplicate-endpoint/dynamic-call/opaque-failing을 risk-score로 융합하지 않음.

## Mnemos가 능가하는 지점

### 1. 비교 — 시간축 그래프 diff (PR-204, cbm 불가능)
`compare_runs(run_a, run_b)`: bitemporal 그래프(valid_from/valid_to)로 두 런의
as-of 스냅샷을 뺄셈.
- 심볼/컨트랙트/데이터엔티티 **added/removed/modified** (certainty 보존).
- 엣지 종류별 델타(CALLS/EXPOSES/READS/WRITES added·removed).
- 런 사이 **신규 findings**.
- **change blast-radius**: 바뀐/삭제된 심볼을 호출한 caller = 리뷰어가
  재점검할 영향범위. → "무엇이 바뀌었고 **무엇에 영향**" (분석+비교 융합).
- **cbm은 히스토리가 없어 이 diff를 만들 수 없다.** 코드리뷰·회귀triage·
  릴리스노트에 직접 유용.

### 2. 분석 — 그라운딩·런타임 융합 (기보유, cbm 미보유)
- **findings 엔진**: schema_mismatch·unverified_claim·duplicate_endpoint·
  dynamic_call·dead_path·opaque_failing을 **risk_score**로 융합. exercised
  플래그(OTLP 실트레이스)로 리스크 가산(PR-196 수정으로 실제 작동).
- **verified/inferred** 1급 구분 + coverage_report 자기보고.
- **impact_analysis**: blast-radius + affected tests + data entities +
  runtime-exercised.
- **grounding**: 그래프에 근거 없는 LLM 주장 드롭 + L1-L3 요약 + 멀티프로바이더 챗.

## 정직한 경계

- **추출 정확도**는 능가가 아니라 parity(양쪽 90%대, PR-202/203). cbm의 Hybrid
  LSP를 이기진 않음 — 하지만 지지 않음.
- **속도·언어폭**은 cbm 우위 불변(C vs Python, 160 vs 18).
- Mnemos의 능가는 **추출 위에 얹는 분석·비교·그라운딩 계층**이다. 즉
  "더 빠른 추출기"가 아니라 **"히스토리·런타임·신뢰를 아는 분석 계층"**에서 이긴다.

## 판정

**비교(compare_runs)**는 cbm이 구조적으로 못 하는 신규 능가. **분석**(findings·
runtime 융합·grounding)은 이미 cbm이 안 하는 계층. 사용자 지시대로 "분석·비교
부분에서" Mnemos가 cbm을 능가하는 실제 기능을 확보했다. 이 계층은 추출 parity
(PR-202/203) 위에서만 의미가 있으므로, 정확도 드라이브와 함께 성립한다.
