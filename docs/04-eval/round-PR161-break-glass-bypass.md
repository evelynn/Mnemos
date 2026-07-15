# Round PR-161 — blocked diff 가 break-glass 없이 승인되던 Gate-B 우회 차단

작성: 2026-06-15 · 브랜치 `claude/round-pr160-analyzer-write-contention` · 이전 commit `f7581ee`
트리거: 자율 라운드 단계 1·2 — 최저 차원 **Plan/Diff workflow(8.0, 최대 WeightedImpact)**
를 실제로 읽어 측정. approve 경로에서 spec §2.5 Gate-B 거부권이 무력화돼 있었다.

## 발견된 결함 (코드 측정으로 노출, 테스트로 재현)

`POST /api/v1/diff_submissions/{id}/approve` (`app/api/diffs.py`) 는 ultrareview
verdict 가 `blocked` 인 diff 를 승인할 때 break-glass 토큰을 요구해야 한다(2-eyes +
15분 TTL + 재검토 통과). 그러나 게이트가 **잘못된 필드/형태**를 읽고 있었다:

```python
findings = submission.auto_review_findings or {}
verdict = findings.get("verdict") if isinstance(findings, dict) else None
if verdict == "blocked":            # ← 항상 False
```

`submit_diff` 는 `auto_review_findings` 를 **list**(findings 배열)로 저장한다
(`diffs.py` — `[f.as_jsonable() for f in report.findings]`). 따라서 실제 제출된
blocked diff 에서 `isinstance(findings, dict)` 는 False → `verdict = None` →
`None == "blocked"` 는 False → **게이트 전체가 스킵**된다. 결과: 임의의 operator 가
ultrareview 가 **critical** 로 막은 diff 를 **토큰 없이** 승인하고 GitLab MR 까지
밀어넣을 수 있었다. spec §2.5("운영 시스템은 신성하다") 의 핵심 불변식 위반 —
심각도 **Critical (governance/보안 우회)**.

근본 원인: `auto_review_findings` 는 list(findings)인데 두 소비처가 dict(`{verdict}`)
로 가정. 권위 있는 신호는 `submit_diff` 가 세팅하는 `submission.status`("blocked")다.

### 기존 테스트가 놓친 이유 (false confidence)
- `test_diff_break_glass._make_submission` 은 `auto_review_findings={"verdict": ...}`
  **dict** 로 fixture 를 손수 만들어, dict 경로에서만 게이트가 작동하는 것처럼 보였다.
- `test_pr120::test_blocked_diff_requires_break_glass` 는 `approve_submission` 을
  **호출하지 않고** `diff.status="approved"` 를 직접 대입 → 엔드포인트 게이트 미검증.
- 즉 어느 테스트도 **실제 list 형태를 엔드포인트에 통과**시키지 않아 우회가 숨었다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 161-1 | approve 게이트를 `submission.status == "blocked"`(권위 필드)로 전환. dict 형태에 의존하던 `auto_review_findings.get("verdict")` 제거 — break-glass 발급 후 dict 로 덮이는 경우(verdict 비-blocked, status 여전히 blocked)에도 일관되게 토큰을 요구 | `app/api/diffs.py` |
| 161-2 | 신규 회귀 테스트: **실제 `submit_diff`(list 저장) → `approve_submission`** 을 구동해 토큰 없는 blocked diff 가 **409** 로 거부되고 MR 생성에 도달하지 않음을 검증. old 코드에선 "DID NOT RAISE" 로 실패(mutation check) | `tests/test_pr161_break_glass_bypass.py` |

## 검증 결과 (실측)

| 항목 | old 코드 | fix 후 |
|---|---|---|
| 토큰 없이 blocked diff approve | **승인됨** (게이트 스킵 → MR 생성 도달) | **HTTP 409** `blocked_by_review` |
| `submit_diff` 저장 형태 | list (변함없음) | list — 이제 status 로 게이트 |
| MR 생성 도달 (토큰 없음) | 도달 | **미도달** |
| 신규 회귀 테스트 | FAIL ("DID NOT RAISE") | **PASS** |

게이트:
- ruff `server/` (0.5.7): **0**
- mypy `app`: **68** (기존 69 → -1; 161-1 이 list 타입에 대한 `.get` 잠재 오류 1건 제거. 신규 오류 0)
- pytest `not integration`: **1567 passed / 19 failed / 18 skipped** — 19 실패는 전부
  사전존재 Windows-환경(서브프로세스 cp949·WinError193·node/dotnet·`/bin/true`),
  PR-160 베이스라인과 **동일집합 → 회귀 0**. Plan/Diff 영역 40 pass(신규 포함). (Linux CI GREEN.)
- docker-free boot: `test_full_http_stack_without_docker` **ready 200**

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| Plan/Diff workflow | 8.0 | **8.5** | 워크플로의 헤드라인 안전장치(§2.5 Gate-B 거부권)가 실제로는 우회 가능했음을 발견·차단. 엔드포인트 게이트를 처음으로 실경로 테스트로 커버. 결정적 회귀 테스트가 외부 증거(§6-A) |

가중평균 영향: Plan/Diff(가중 0.05) +0.5 → +0.025/10 → 약 **91.0 → 91.2 / 100**. 나머지 차원 불변.

### 남은 관련 격차 (범위 외 — note만, 후속 라운드 후보)
- `list_submissions_filtered` (`diffs.py`) 의 `verdict` 필터도 같은 dict 가정
  (`auto_review_findings["verdict"]`)을 써서, list 형태 행에는 매칭되지 않는다 →
  대시보드 "Break-glass active: N" verdict 필터가 실데이터에서 미작동 가능. 또한
  verdict(clean/warn) 스칼라가 영속되지 않아 status 만으로는 clean/warn 구분 불가.
  (심각도 Low — 카운트/표시 문제. PR-161 의 보안 우회와 분리.)
- `approve_submission` 에 terminal-state 멱등 가드 부재 — 이미 approved 인 diff 를
  재승인하면 두 번째 MR 가 생성될 수 있음. (멱등성 후속 후보.)
