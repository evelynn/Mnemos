"""PR-133 — Pydantic input validation bounds.

PR-132 의 input validation audit 가 55 입력 모델 필드의
max_length/max_items 누락 발견. unbounded str/list 는 DoS
위험:
- 10 MB password → bcrypt CPU 폭발
- 10K plan tasks → DB 폭발
- 무한 nested dict → memory 폭발

본 PR 은 가장 critical 한 3 endpoint fix:
1. LoginRequest.username/password (bcrypt DoS)
2. BreakGlassRequest.rationale (text field)
3. PlanSubmit.tasks (list DoS)

나머지 52 필드는 별도 후속 PR (높은 endpoint 부터 점진 적용).
이 테스트는 fix 된 것 박제 + 회귀 가드.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_login_request_rejects_oversized_password():
    """10 MB password → ValidationError, bcrypt 도달 안 함."""
    from app.api.auth import LoginRequest
    huge_pw = "x" * 10_000_000
    with pytest.raises(ValidationError):
        LoginRequest(username="alice", password=huge_pw)


def test_login_request_rejects_oversized_username():
    from app.api.auth import LoginRequest
    huge_un = "x" * 10_000
    with pytest.raises(ValidationError):
        LoginRequest(username=huge_un, password="ok-password")


def test_login_request_accepts_normal_input():
    """경계 안 정상 입력은 OK."""
    from app.api.auth import LoginRequest
    req = LoginRequest(username="alice", password="secure-pw-12345")
    assert req.username == "alice"


def test_login_password_max_512_covers_bcrypt_input():
    """bcrypt 자체는 72 bytes 만 사용. 512 max 는 safe upper bound."""
    from app.api.auth import LoginRequest
    # exactly 512 — should pass
    boundary_pw = "x" * 512
    LoginRequest(username="u", password=boundary_pw)
    # 513 — fail
    with pytest.raises(ValidationError):
        LoginRequest(username="u", password="x" * 513)


def test_break_glass_rationale_min_max_enforced():
    from app.api.break_glass import BreakGlassRequest
    # 너무 짧음 → fail (min ~ 200)
    with pytest.raises(ValidationError):
        BreakGlassRequest(rationale="too short")
    # 너무 김 (10K + 1) → fail
    with pytest.raises(ValidationError):
        BreakGlassRequest(rationale="x" * 10_001)
    # 적절한 길이 → OK
    BreakGlassRequest(rationale="Acknowledged blocking finding. " * 30)


def test_plan_submit_rejects_too_many_tasks():
    from app.api.plans import PlanSubmit, PlanTask, PlanSpec
    # 101 tasks → fail
    huge_tasks = [
        PlanTask(id=f"t{i}", title=f"task {i}")
        for i in range(101)
    ]
    with pytest.raises(ValidationError):
        PlanSubmit(
            spec=PlanSpec(title="test", motivation="r"),
            tasks=huge_tasks,
            target_component_id="comp",
            requester="user:test",
        )


def test_plan_submit_accepts_100_tasks_boundary():
    from app.api.plans import PlanSubmit, PlanTask, PlanSpec
    boundary_tasks = [
        PlanTask(id=f"t{i}", title=f"task {i}")
        for i in range(100)
    ]
    submit = PlanSubmit(
        spec=PlanSpec(title="test", motivation="r"),
        tasks=boundary_tasks,
        target_component_id="comp",
        requester="user:test",
    )
    assert len(submit.tasks) == 100


def test_plan_submit_target_component_id_max_300():
    from app.api.plans import PlanSubmit, PlanSpec
    with pytest.raises(ValidationError):
        PlanSubmit(
            spec=PlanSpec(title="t", motivation="m"),
            tasks=[],
            target_component_id="x" * 301,
            requester="u",
        )


# ─── audit guard: PR-133 fix 가 후속 PR 에서 dropped 못 함 ──────


def test_login_request_has_max_length_constraints():
    """source-level 검증 — Field(max_length=N) 명시 유지."""
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "auth.py"
    ).read_text(encoding="utf-8")
    assert 'username: str = Field(' in src
    assert 'max_length=64' in src
    assert 'max_length=512' in src


def test_break_glass_rationale_has_max_length():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "break_glass.py"
    ).read_text(encoding="utf-8")
    assert "max_length=10_000" in src or "max_length=10000" in src


def test_plan_submit_tasks_has_max_length():
    from pathlib import Path
    src = (
        Path(__file__).resolve().parents[1] / "app" / "api" / "plans.py"
    ).read_text(encoding="utf-8")
    assert "max_length=100" in src
