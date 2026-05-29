"""PR-130b — FK ondelete 정책 박제.

PR-130 의 데이터 무결성 audit 가 7개 FK 의 ondelete 미명시 발견.
각 의도를 모델에 명시 + alembic migration 0023 으로 schema 적용.

CASCADE (자식 무의미 시 함께 삭제):
- DiffSubmission.plan_id → plans
- DiffBreakGlassGrant.submission_id → diff_submissions
- AnalysisStage.run_id → analysis_runs

SET NULL (자식 자립 가능, orphan 허용):
- User.organization_id → organizations
- Secret.user_id → users
- Project.organization_id → organizations
- ProjectDB.secret_id → secrets

이 테스트는 7 FK 의 ondelete 정책이 의도된 값과 일치 확인.
미래 refactor 가 정책 drop 하지 못하도록 가드.
"""

from __future__ import annotations

from pathlib import Path

_MODELS = Path(__file__).resolve().parents[1] / "app" / "models"


def _src(name: str) -> str:
    return (_MODELS / name).read_text(encoding="utf-8")


def test_diff_submission_plan_id_cascade():
    """DiffSubmission 은 Plan 없이 의미 없음 — CASCADE."""
    assert 'ForeignKey("plans.id", ondelete="CASCADE")' in _src("plans.py")


def test_break_glass_grant_submission_id_cascade():
    assert 'ForeignKey("diff_submissions.id", ondelete="CASCADE")' in _src("plans.py")


def test_analysis_stage_run_id_cascade():
    """Stage row 는 Run 없이 의미 없음 — CASCADE."""
    assert 'ForeignKey("analysis_runs.id", ondelete="CASCADE")' in _src("stages.py")


def test_user_organization_id_set_null():
    """User 는 org 없이도 존재 가능 — SET NULL."""
    assert 'ForeignKey("organizations.id", ondelete="SET NULL")' in _src("auth.py")


def test_secret_user_id_set_null():
    """Secret 은 owner 사라져도 audit 보존 — SET NULL."""
    assert 'ForeignKey("users.id", ondelete="SET NULL")' in _src("auth.py")


def test_project_organization_id_set_null():
    assert 'ForeignKey("organizations.id", ondelete="SET NULL")' in _src("projects.py")


def test_project_db_secret_id_set_null():
    """ProjectDB 가 misconfigured 으로 남아 admin 이 rebind 가능 — SET NULL."""
    assert 'ForeignKey("secrets.id", ondelete="SET NULL")' in _src("projects.py")


# ─── alembic migration 0023 박제 ────────────────────────────────


def test_alembic_0023_migration_exists():
    """0023_fk_ondelete migration 이 모든 7 FK 변경 포함."""
    mig = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions"
        / "0023_fk_ondelete.py"
    )
    src = mig.read_text(encoding="utf-8")
    # 모든 FK change tuple 확인.
    for table in (
        "diff_submissions", "diff_break_glass_grants", "analysis_stages",
        "users", "secrets", "projects", "project_dbs",
    ):
        assert f'"{table}"' in src, f"missing migration entry for {table}"
    # Loop-driven migration — 호출은 2개 (upgrade + downgrade) 지만
    # _FK_CHANGES 리스트가 모든 7 entry 포함해야 함.
    assert src.count("ondelete=") >= 1
    # CASCADE 와 SET NULL 양쪽 정책 정확히 적용.
    assert '"CASCADE"' in src
    assert '"SET NULL"' in src
    # downgrade 정의 + ondelete 제거 (default 회귀).
    assert "def downgrade()" in src
    assert "def upgrade()" in src


def test_alembic_0023_chained_to_0022():
    mig = (
        Path(__file__).resolve().parents[1] / "alembic" / "versions"
        / "0023_fk_ondelete.py"
    )
    src = mig.read_text(encoding="utf-8")
    assert 'down_revision: Union[str, None] = "0022_node_embedding"' in src


# ─── 모든 FK 가 ondelete 명시 강제 (회귀 가드) ─────────────────


def test_every_fk_in_models_declares_ondelete():
    """미래 refactor 가 ondelete 없는 FK 추가 못 하게 가드."""
    import re
    found_no_ondelete = []
    for f in sorted(_MODELS.glob("*.py")):
        src = f.read_text(encoding="utf-8")
        for m in re.finditer(
            r'ForeignKey\(\s*"([^"]+)"([^)]*)\)', src
        ):
            target = m.group(1)
            rest = m.group(2)
            if "ondelete=" not in rest:
                found_no_ondelete.append(f"{f.name}: ForeignKey({target}) without ondelete")
    assert not found_no_ondelete, (
        "FK without explicit ondelete (data integrity audit):\n  "
        + "\n  ".join(found_no_ondelete)
    )
