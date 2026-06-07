# Round PR-141 — SQLite write-contention fix (agent-extraction run)

작성: 2026-06-01 · 브랜치 `claude/gallant-ramanujan-aRxAo` · 이전 commit `591d226`
트리거: PR-140 의 Claude-Code 추출이 OpenClaw 실분석 중 `failed`

## 발견된 결함 (실분석 중 노출)

PR-140 의 `agent_extract:cpp` 스테이지가 OpenClaw 엔진 .cpp 6~8개를 Claude
Code 로 추출하던 중 런이 **failed**:

```
sqlite3.OperationalError: database is locked
  at StageTracker._flush -> UPDATE analysis_stages ...
```

근본 원인 — 두 가지가 겹침:
1. **세션 경합**: 추출 스테이지가 한 세션에 노드를 INSERT(미커밋, 쓰기 락 보유)한
   상태에서 `stage.increment()` 가 *별도 세션*으로 `analysis_stages` 를 UPDATE.
   SQLite 는 단일 writer + 기본 `busy_timeout=0` → 즉시 lock 오류.
2. 이는 StageTracker(열린 세션 + increment) 패턴의 **잠재적 동시성 버그**였고,
   로컬 모드(API + inline job 동일 프로세스)에서 *실데이터를 처음 적재한* 에이전트
   스테이지가 처음 노출. 결정적 분석기는 로컬에서 0건 추출이라 안 터졌고, 프로덕션
   Postgres 는 MVCC 라 무사 → 그래서 여태 안 보였다.

부수 관찰: 큰/벤더 파일(Miniz.cpp 204KB 등) Claude 호출이 120s 타임아웃 →
graceful None(파일 단위 degrade) 이라 실패 원인은 아니나 수율 저하.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 141-1 | SQLite 연결에 `journal_mode=WAL` + `busy_timeout=10000` + `synchronous=NORMAL` PRAGMA (sqlite URL 일 때만; Postgres 무영향). 로컬 모드 쓰기 경합의 **시스템적** 해결 — 모든 스테이지에 이로움 | `app/db.py` |
| 141-2 | 에이전트 스테이지: 파일별 노드를 **짧은 세션에 커밋한 뒤** `stage.increment()` 호출 → 락 윈도우 제거. 파일별 ingest 를 try/except 로 감싸 한 파일 DB 오류가 런 전체를 죽이지 않게 (analyzer 와 동일한 graceful degrade) | `app/orchestrator/jobs.py` |
| 141-3 | 에이전트 호출 타임아웃 120→150s (큰 파일 수율 ↑) | `app/extractor/agent_extract.py` |

## 검증 (게이트)

| 게이트 | 결과 |
|---|---|
| ruff `app/` | **0** |
| WAL pragma | `journal_mode=wal`, `busy_timeout=10000` 실측 |
| 타깃 테스트 (pr139/pr140/pr135 docker-free) | **21 passed** |
| pytest `not integration` (−pr114) | **1454 passed / 6 failed** (회귀 0; 실패 6은 기존 환경: pr116 툴체인 5 + pr138d flake 1) |
| OpenClaw 재분석 | `database is locked` **0건** — agent_extract 가 lock 없이 진행 |

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| C. 운영검증(배포) | 8.3 | **8.6** | 로컬 모드 쓰기 경합 제거 — 실데이터 분석 안정화 |
| K. 코드/타입 위생 | 7.8 | **8.0** | 잠재 동시성 버그 fix + graceful degrade |
