# Known issues (honest, post-PR-154)

비-환경 잔여 이슈를 정직하게 기록. 제품 결함이 아닌 것은 그렇게 명시.

## 1. `test_pr138d::test_health_ready_reports_inline_worker_in_local_mode` — 풀스위트 한정 flake

**증상**: 단독 실행 통과, 풀스위트(`pytest -m "not integration"`)에서만 실패.

**근본 원인 (조사 완료, PR-154 라운드)**:
- 실패는 `/api/v1/health/ready` 가 **503** 반환(200 기대) → `overall = db∧redis∧worker∧
  crypto` 중 하나가 False.
- pr138d 의 module-scoped `seeded_state` 픽스처가 `os.environ["DATABASE_URL"]` 을
  자기 sqlite 로 바꾸고 **`importlib.reload(app.db)`** 로 엔진을 재생성한다. 그러나
  `app.api.health` 등 일부 모듈은 import 시점에 `from app.db import SessionLocal`
  로 **옛 엔진 참조를 캡처**해 둔다. 특정 풀스위트 수집/실행 순서에서 이전 모듈이
  app.db 를 dispose/reload 한 뒤 pr138d 가 다시 reload 하면, health 가 들고 있는
  `SessionLocal` 이 폐기된 엔진을 가리켜 `_check_db` SELECT 1 이 실패 → 503.
- 2-모듈 subset(pr135+pr138d, full_value_chain+pr138d)으로는 **재현 안 됨** → 특정
  다중 모듈 순서에서만 발생. 정확한 폴루터 특정엔 163파일 bisection 필요.

**왜 제품 결함이 아닌가**: 라이브 서버/`serve_local` 은 `importlib.reload(app.db)`
를 절대 하지 않는다(테스트 격리용 수법). 실제 `/health/ready` 는 매 검증에서 200
정상(예: PR-148 OTLP·G/I 라이브, PR-153 boot). 프로덕션(Postgres)도 무관.

**왜 지금 고치지 않는가**: 수정하려면 (a) 풀스위트 bisection(고비용) 또는 (b)
다수 테스트 모듈의 `app.db` reload/teardown 패턴을 재작성(고위험, 다른 e2e 테스트
회귀 위험). 둘 다 저신뢰·고위험이라 하네스의 "저가치/고위험 churn 금지" 원칙상
보류. 시도한 가설 2건(get_settings 캐시 / env 복원)은 원인이 아니어서 무효(revert).

**해소 조건**: 테스트 하네스가 app.db 를 reload 하는 대신 **의존성 주입/엔진
재바인드 헬퍼**를 쓰도록 e2e 픽스처를 통일(별도 테스트-인프라 작업).

## 2. mypy 69건 — 전부 false-positive

전수조사(score-audit-honest.md): short-circuit 으로 런타임 안전한 union-attr,
변수 재사용 타이핑, 옵셔널 deps 등. 런타임 버그 0. green 화하려면 대량 annotation/
`# type: ignore` = 코스메틱 churn(하네스 금지). 게이트는 "신규 0 + 단조 감소"로 운영.

## 3. 환경 밖 (이 컨테이너에서 불가)

- 실제 `docker compose up` 1회(C 배포) — docker daemon 없음. docker-free 로 대체 검증.
- live OpenTelemetry SDK → `/otlp/v1/traces`(I) — 송신자 없음. 수신·버퍼·적재는 실측.
- ggoss-ts/csharp 툴체인(pr116 5건) — node_modules/dotnet 미설치. SessionStart hook
  으로 `npm ci` 하면 해소 가능(테스트-env 작업).

## 4. `ScheduleWakeup` 미제공

이 환경엔 ScheduleWakeup 도구가 없어 `/loop` 자동 재실행 불가 — 매 라운드 사용자
재호출로 진행. (제품과 무관, 하네스 실행 환경 한계.)
