"""FK ondelete policies (PR-130 — data integrity audit)

Revision ID: 0023_fk_ondelete
Revises: 0022_node_embedding
Create Date: 2026-05-28

PR-130 의 데이터 무결성 audit 가 7개 FK 의 ondelete= 미명시
검출. Postgres default 는 NO ACTION (= RESTRICT) — 부모 row
삭제 시 자식 row 존재하면 실패. 의도된 동작이 case 마다 다름:

CASCADE (자식이 부모 없이 무의미):
- diff_submissions.plan_id → plans.id
- diff_break_glass_grants.submission_id → diff_submissions.id
- analysis_stages.run_id → analysis_runs.id

SET NULL (자식이 자립 가능, orphan 허용):
- users.organization_id → organizations.id
- secrets.user_id → users.id
- projects.organization_id → organizations.id
- project_dbs.secret_id → secrets.id

migration 은 drop_constraint + create_foreign_key 로 ondelete
정책만 갱신. 데이터 변경 0.
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0023_fk_ondelete"
down_revision: Union[str, None] = "0022_node_embedding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FK_CHANGES = [
    # (table, column, fk_name, target_table, ondelete)
    ("diff_submissions", "plan_id",
     "diff_submissions_plan_id_fkey", "plans", "CASCADE"),
    ("diff_break_glass_grants", "submission_id",
     "diff_break_glass_grants_submission_id_fkey", "diff_submissions", "CASCADE"),
    ("analysis_stages", "run_id",
     "analysis_stages_run_id_fkey", "analysis_runs", "CASCADE"),
    ("users", "organization_id",
     "users_organization_id_fkey", "organizations", "SET NULL"),
    ("secrets", "user_id",
     "secrets_user_id_fkey", "users", "SET NULL"),
    ("projects", "organization_id",
     "projects_organization_id_fkey", "organizations", "SET NULL"),
    ("project_dbs", "secret_id",
     "project_dbs_secret_id_fkey", "secrets", "SET NULL"),
]


def upgrade() -> None:
    for table, col, fk_name, target, ondelete in _FK_CHANGES:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name, table, target, [col], ["id"],
            ondelete=ondelete,
        )


def downgrade() -> None:
    for table, col, fk_name, target, _ in _FK_CHANGES:
        op.drop_constraint(fk_name, table, type_="foreignkey")
        op.create_foreign_key(
            fk_name, table, target, [col], ["id"],
        )
