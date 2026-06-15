# Round PR-160 — 결정적 분석 스테이지가 진행 보고 전에 그래프 행을 커밋 (docker-free SQLite 쓰기 경합)

작성: 2026-06-15 · 브랜치 `main` · 이전 commit `321e8ca` (PR-159 머지)
트리거: 중단된 자율 라운드 재개. docker-free 결정적 분석(PR-153 in-repo `ggoss-py`)이
실제 행을 스트리밍하게 되면서, PR-141 이 에이전트 스테이지에만 적용했던
commit-before-increment 가 결정적 분석 스테이지엔 빠져 있어 `database is locked` 재노출.

## 발견된 결함 (테스트/실행으로 노출)

`_run_analyzer_stage` 는 분석기 세션을 연 채(미커밋 그래프 INSERT = SQLite 쓰기 락 보유)
루프 안에서 `stage.increment()` 를 호출했다. `StageTracker.increment` 는 25건마다
`_flush()` 에서 **별도 세션**으로 `analysis_stages` 를 UPDATE 한다(`app/orchestrator/stages.py:164`).
SQLite 는 단일 writer 라, 결정적 분석기가 25행 이상 스트리밍하는 순간 두 번째 writer 가
충돌 → `database is locked`.

근본 원인 — PR-141 은 같은 클래스의 버그를 (a) `db.py` 에 WAL + `busy_timeout=10000` 으로
완화하고, (b) **에이전트 추출 스테이지**를 commit-before-increment 로 구조적으로 고쳤다.
그러나 **결정적 분석 스테이지**는 당시 로컬 모드에서 0행 추출이라 안 터져 그대로 뒀다
(PR-141 문서에 명시). PR-153/PR-144 가 docker-free in-repo `ggoss-py` 로 실제 심볼을
추출하게 되면서, 잠재 버그가 모든 비-toy 파이썬 분석(>25 심볼 — 사실상 모든 실제 저장소)에서
발현하게 됐다. `busy_timeout` 은 지연일 뿐, 긴 스트림이 임계를 넘으면 여전히 실패한다.

격리 재현 (신규 테스트, `busy_timeout=0` 파일 SQLite):
- fix 전: 25번째 행에서 `OperationalError: database is locked`
  (`UPDATE analysis_stages SET items_done=25 …`) — 즉시 실패.
- fix 후: 120행 전부 커밋·적재. 진행 보고 시점마다 **별도 커넥션**에서 이미 커밋된 행이
  가시(50 → 100 → 120). 통과.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 160-1 | `_run_analyzer_stage`: 진행을 batch(`pending_progress`)하고 **분석기 세션을 커밋한 뒤** `stage.increment()` 호출(임계 50), 루프 종료 후 최종 커밋 + 잔여 flush. 에이전트 스테이지(PR-141)의 검증된 패턴을 결정적 분석 스테이지에 적용 | `app/orchestrator/jobs.py` |
| 160-2 | 분석기 spawn 시 `shutil.which` 로 해소한 절대경로를 argv[0] 로 사용(이전엔 bare name). docker-free spawn 견고화(특히 Windows 의 bare-name exec 불안정). 바이너리가 PATH 에 없을 때의 graceful fallback 경로는 불변 | `app/analyzers/runner.py` |
| 160-3 | 결정적 회귀 테스트: 120행 스트리밍 → 진행 flush 직전 별도 커넥션에서 커밋 가시성 검증(commit-before-increment 불변식). old 코드에선 `database is locked` 로 실패하도록 설계 | `tests/test_pr160_analyzer_stage_write_contention.py` |
| 160-4 | `.gstack/` (로컬 툴링 산출물) gitignore | `.gitignore` |

## 검증 결과 (실측)

| 항목 | fix 전 | fix 후 |
|---|---|---|
| `_run_analyzer_stage` 120-symbol 스트림 (file SQLite, busy_timeout=0) | 25행째 `database is locked` | 120행 전부 적재 |
| commit-before-increment 불변식 | 진행 flush 시 커밋 0행 가시 | 50 → 100 → 120 커밋 가시 |
| 신규 회귀 테스트 | (해당 없음) | **PASS** (old 코드에선 FAIL 재현 확인 — mutation check) |

게이트:
- ruff `server/` (0.5.7, CI pin): **0**
- mypy `app`: **69** (불변, 25파일 / 150소스 검사)
- pytest `not integration`: **1566 passed / 19 failed / 18 skipped**. 19 실패는 **전부
  사전존재 Windows-환경 실패**(서브프로세스 cp949 디코드, WinError 193 fake-analyzer,
  node/dotnet 미설치, `/bin/true` POSIX). `git stash` 로 HEAD 베이스라인을 같은 스위트로
  돌려 **동일 19 집합** 확인 → **회귀 0**, 신규 테스트 +1 pass. (Linux CI 에선 GREEN.)
- docker-free boot: `test_full_http_stack_without_docker` **ready 200** (PR-135 15/15 pass)

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| 운영 검증 (배포) | 8.6 | **8.8** | docker-free 결정적 분석이 현실적 크기(>25 심볼) 저장소에서 쓰기 경합으로 죽던 경로 제거. 결정적 회귀 테스트가 외부 증거(§6-A) |

가중평균 영향: 운영검증(가중 0.07) +0.2 → +0.014/10 → 약 **90.9 → 91.0 / 100**. 나머지 차원 불변.

### PR-141 와의 관계
PR-141 = (a) WAL/busy_timeout 완화 + (b) **에이전트** 스테이지 구조 수정.
PR-160 = (b) 를 **결정적 분석** 스테이지로 완성. 두 스테이지가 이제 동일한 안전 패턴을 공유한다.

### 남은 관련 격차 (범위 외 — note만)
- `_run_db_live_schema_stages` 도 동일 패턴(루프 내 increment)을 쓰나 live MSSQL/Oracle 가
  필요해 docker-free 에서 미발현. 본 라운드 범위 밖 — 별도 라운드 후보로만 기록.
