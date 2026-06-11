# Round PR-158 — docker-free 분석 stall fix (Agent SDK 기본 off)

작성: 2026-06-11 · 브랜치 `claude/update-readme-docs-0baecs`
트리거: 자율 라운드 단계 1 실행 정찰 — docker-free 부팅 후 실제 분석 run 을
완주시켜 보니 stage 18 `l1_summaries` 에서 멈춤.

## 발견된 결함 (실행으로 노출)

`python -m app.serve_local --seed-demo` 로 띄운 docker-free 인스턴스에서
analysis run 을 트리거하면 17개 스테이지(analyzer + link + findings)는 즉시
완료되지만 **`l1_summaries` 에서 60초+ 동안 진행 0/0** 으로 멈춘 듯 보였다.

근본 원인 — 추출기 백엔드 선택(`app/extractor/agent.py`):
1. `ANTHROPIC_API_KEY` → 직접 Anthropic SDK
2. `claude_agent_sdk` import 가능 + opt-out 안 함 → 번들 Claude Code CLI
3. 결정적 stub

`claude_agent_sdk` 는 의존성이라 로컬 모드에서도 항상 import 가능 →
`is_agent_sdk_available()` True → **매 L1/L2/L3 요약마다 번들 Claude CLI 를
subprocess 로 spawn**. 그러나 로컬 체험 환경엔 Claude Code 구독/네트워크가
없으므로 `summarize_via_agent_sdk` 의 `asyncio.wait_for(..., timeout_s=60)` 가
**요약 1건마다 최대 60초 대기** 후에야 stub 으로 폴백. 요약이 수십 건이면
run 이 수 분~사실상 무한정 멈춘 것처럼 보인다.

로그 증거(`/tmp/s6.log`): `Using bundled Claude Code CLI` 가 05:32:25,
05:32:49, 05:33:17, 05:33:29 … 로 반복 spawn, 매번 polling 시점에 `running`.

이는 docker-free 로컬 모드의 **핵심 약속("zero external services 로 체험")** 과
정면 충돌한다. local 모드는 명시적으로 외부 서비스 없는 trial 경로인데,
기본값이 외부(구독+네트워크 필요) Claude CLI 를 매 요약마다 시도한다.

## 보완

| ID | 변경 | 파일 |
|---|---|---|
| 158-1 | `_bootstrap_env` 에 `os.environ.setdefault("MNEMOS_DISABLE_AGENT_SDK", "1")` — 로컬 모드는 Agent SDK 경로(path 2)를 기본 off. `setdefault` 라 운영자가 `ANTHROPIC_API_KEY`(path 1, 무영향) 또는 `MNEMOS_DISABLE_AGENT_SDK=0` 으로 재활성 가능 | `app/serve_local.py` |
| 158-2 | bootstrap 테스트에 새 기본값 assert + override(=0) 존중 테스트 | `tests/test_pr135_docker_free_local_mode.py` |

## 검증 결과 (실측)

| 항목 | fix 전 | fix 후 |
|---|---|---|
| analysis run 완주 (ggoss-py, l1/l2/l3=5) | 60s+ `running` (stall) | **completed 3s** |
| l1/l2/l3 스테이지 | l1 에서 멈춤 | 셋 다 completed |
| 요약 생성 | 0 (대기 중) | l1 6건 + l3 1건 stub(`fallback_reason=no_backend`) |
| 로그 `bundled Claude CLI` spawn | 매 요약 반복 | **0건** |
| 정직성(model_used) | — | `stub` / `no_backend` 영속 (PR-138b 경로 유지) |

게이트: ruff 0 · pytest (not integration, −pr114) GREEN · mypy 69(불변) · boot ready 200.

## 영역 점수 갱신

| 영역 | before | after | 근거 |
|---|---:|---:|---|
| C. 운영 검증(배포) | 7.8 | **8.6** | docker-free trial 의 핵심 플로우(analysis run)가 처음으로 **완주**. "zero deps 로 띄워 분석" 약속이 실제로 동작 |
| E. L1~L3 LLM | 9.5 | 9.5 | 경로 자체는 불변(stub/anthropic/agent-sdk 모두 유지). 로컬 기본값만 조정 |

가중평균 영향: C 영역(가중 0.12) +0.8 → 약 +0.10/10.
