# 대규모 분석 throughput 실측 + 병목 규명 — 2026-07-08

> 성숙도·안정성 보강 ① — "초대형 모노레포 처리량". 가짜 fix 대신 실측 + 진짜
> 제약 규명. 대상: codebase-memory-mcp(1,775 files, C 본체), run 1aaa1048.

## 1. 실측 (스테이지별, analysis_stages 실데이터)

| 스테이지 | 시간 | items | rate |
|---|---|---|---|
| **calls:cpp** | **109.3s** | 56,116 edges | 513/s |
| symbols:cpp | 19.3s | 11,168 | 579/s |
| data_access:cpp | 6.1s | 169 | — |
| calls:typescript | 3.1s | 878 | 281/s |
| calls:python | 2.6s | 951 | 365/s |
| 기타 (contracts/summaries/…) | ~7s | — | — |
| **합계** | **~148s** | | |

`calls:cpp` 하나가 전체의 **74%**.

## 2. 병목 분해 (추출 vs DB ingest)

- `ggoss-cpp calls` **단독 추출**(전체 레포, DB 없음): **~8.4s** → 56,116 edges.
- 파이프라인 `calls:cpp` **스테이지**: **109.3s**.
- ∴ **추출 8s(7%) / DB ingest ~101s(93%)**. 병목은 **추출이 아니라 DB 쓰기**
  (~555 edges/s). 브랜치명 `analyzer-write-contention`과 정확히 일치.

## 3. 근본 원인 (인덱스 아님)

- `ix_edges_identity (project_id, source_id, target_id, kind, valid_to)` 인덱스가
  **이미 존재** — upsert WHERE와 정확히 매칭. close-UPDATE는 O(log n), O(n²) 아님.
- SQLite pragma도 **이미 최적**: `journal_mode=WAL` + `busy_timeout=10000` +
  `synchronous=NORMAL`. 값싼 튜닝 여지 없음.
- 진짜 원인 = **구조적 문쓰기 volume**: `upsert_edge`가 edge당
  **① close-UPDATE(valid_to 닫기) + ② INSERT** = 56k edge × 2 = **11만+ 개별
  SQL문**을 ORM으로 하나씩, SQLite **단일 writer**에 직렬화. + commit-every-50
  (1,120 commits) + StageTracker 진행률 쓰기.

## 4. 진짜 fix 경로 (우선순위, 각 리스크 명시)

1. **Fresh-mode skip (예상 ~2x).** 초기 분석(빈 그래프)에선 close-UPDATE가
   전부 no-op(닫을 게 없음). run 시작 시 프로젝트가 비었으면 `fresh=True`를
   ingest에 전달해 close-UPDATE 56k건을 스킵 → 문쓰기 절반. **리스크**: 재분석/
   중복 edge의 bitemporal 정확성. 단일 run 내 중복 edge(cpp calls + link_calls)
   edge-case 검증 필수 → **신중한 별도 PR** 필요(하네스 없이 서두르면 위험).
2. **Bulk INSERT (예상 추가 개선).** per-record `session.add` 대신 executemany/
   multi-value INSERT로 56k INSERT를 배치. ORM 경로 변경.
3. **Bulk close-UPDATE.** 교체 대상 identity 집합을 IN절 한 번으로 닫기.
4. **Postgres 스테이지 병렬화.** 언어/verb 독립 스테이지를 gather. SQLite는
   단일 writer라 무효 → Postgres 전용. 아키텍처 변경.

## 5. 사내-사용 관점 (완화 요인)

- 서버 배포(고사양) + **증분 재분석**(evidence_hash) + **분석 1회→무한 재조회**로
  첫 패스 비용 amortize. scale-safe(안 죽음, budget 청킹).
- 즉 사내 "1회 인덱싱 후 에이전트 재조회" 시나리오에선 첫 패스 latency가
  치명적이지 않음. 단 **초대형 모노레포 첫 패스**(수백만 LOC)는 실질 잔존 갭 —
  위 fix 1~2가 정공법.

## 6. 판정

- **측정 완결**: 병목은 DB ingest(~555 edges/s), 추출 아님. 인덱스·pragma는 이미
  최적. 구조적 문쓰기 volume이 원인.
- **이번 세션 조치**: 가짜 fix 금지 + write-path가 활성 작업(브랜치) 영역이라,
  리스크 있는 write-path 변경은 하지 않고 **실측·제약·로드맵을 확정**. fix 1
  (fresh-mode skip)이 다음 정공법이며, bitemporal 정확성 하네스와 함께 별도
  PR로 진행해야 안전.
