import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_approval")
    spec: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tasks: Mapped[list] = mapped_column(JSONB, nullable=False)
    impact_report: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    requester: Mapped[str] = mapped_column(String, nullable=False)
    worktree_path: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Reproducibility hints for the worktree behind ``worktree_path``.
    # JSONB instead of two scalar columns so we can extend (submodule
    # SHAs, branch name, mirror URL) without another migration —
    # Team B critique #2 against the original ``base_sha``/``head_sha``
    # scalar pair.
    worktree_meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    # Immutable source-analysis provenance for the worktree and Gate A
    # decision. Legacy rows remain nullable at rest, but every mutation
    # boundary rejects an unbound Plan; silently guessing from HEAD would
    # authorise edits against code other than the graph the operator reviewed.
    source_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
    source_git_sha: Mapped[Optional[str]] = mapped_column(
        String(length=64),
        nullable=True,
    )
    source_graph_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )
    source_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # PR-43 — optional assignee so a team can split planning load.
    assignee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["source_run_id", "project_id"],
            ["analysis_runs.id", "analysis_runs.project_id"],
            name="fk_plans_source_run_project",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(source_run_id IS NULL AND source_git_sha IS NULL AND "
            "source_graph_generation IS NULL AND "
            "source_overlay_generation IS NULL) OR "
            "(source_run_id IS NOT NULL AND source_git_sha IS NOT NULL AND "
            "source_graph_generation IS NOT NULL AND "
            "source_overlay_generation IS NOT NULL AND "
            "length(source_git_sha) BETWEEN 40 AND 64 AND "
            "source_graph_generation > 0 AND "
            "source_overlay_generation >= 0)",
            name="ck_plans_source_revision_shape",
        ),
        Index(
            "ix_plans_project_source_revision",
            "project_id",
            "source_run_id",
            "source_graph_generation",
            "source_overlay_generation",
        ),
    )


class DiffSubmission(Base):
    __tablename__ = "diff_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        # PR-130 — CASCADE: a DiffSubmission has no meaning without
        # its parent Plan, and the audit_log preserves the history.
        UUID(as_uuid=True),
        ForeignKey("plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    task_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_approval")
    diff: Mapped[str] = mapped_column(Text, nullable=False)
    test_results: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    self_review_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    auto_review_findings: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    gitlab_mr_iid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    gitlab_mr_url: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # PR-43 — assignee at the diff-submission grain too, so multiple
    # operators can split review load on a single plan.
    assignee_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Exact canonical graph revision against which Gate-B computed the
    # persisted verdict. Legacy rows remain NULL and are deliberately
    # unapprovable until a fresh review binds all four fields.
    review_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_source_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    review_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    review_git_sha: Mapped[Optional[str]] = mapped_column(
        String(length=64), nullable=True
    )

    __table_args__ = (
        CheckConstraint(
            "(review_run_id IS NULL AND review_source_generation IS NULL "
            "AND review_overlay_generation IS NULL AND review_git_sha IS NULL) "
            "OR (review_run_id IS NOT NULL AND review_source_generation IS NOT NULL "
            "AND review_overlay_generation IS NOT NULL AND review_git_sha IS NOT NULL "
            "AND review_source_generation > 0 AND review_overlay_generation >= 0 "
            "AND length(review_git_sha) BETWEEN 40 AND 64)",
            name="ck_diff_submissions_review_revision",
        ),
        Index(
            "ix_diff_submissions_review_revision",
            "review_run_id",
            "review_source_generation",
            "review_overlay_generation",
        ),
    )


class DiffBreakGlassGrant(Base):
    """One-time, time-limited authorisation to approve a `blocked` diff.

    Spec §2.5 requires "운영 시스템은 신성하다" — no setting may disable the
    Gate B veto. The previous `override=true + rationale` shortcut violated
    that. A grant exists only after an admin re-runs the ultrareview
    pipeline and observes a non-blocked verdict, and is consumed atomically
    by an approver who is **not** the admin who issued it (2-eyes).
    """

    __tablename__ = "diff_break_glass_grants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        # PR-130 — CASCADE: grants only exist for an extant submission.
        UUID(as_uuid=True),
        ForeignKey("diff_submissions.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    issued_by: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    rerun_review_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The grant authorizes only the exact canonical revision reviewed by the
    # issuer. Approval consumes it with these fields in the atomic predicate.
    review_run_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("analysis_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    review_source_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    review_overlay_generation: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    review_git_sha: Mapped[Optional[str]] = mapped_column(
        String(length=64), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consumed_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    __table_args__ = (
        Index("idx_break_glass_submission_active",
              "submission_id", "consumed_at", "expires_at"),
        CheckConstraint(
            "(review_run_id IS NULL AND review_source_generation IS NULL "
            "AND review_overlay_generation IS NULL AND review_git_sha IS NULL) "
            "OR (review_run_id IS NOT NULL AND review_source_generation IS NOT NULL "
            "AND review_overlay_generation IS NOT NULL AND review_git_sha IS NOT NULL "
            "AND review_source_generation > 0 AND review_overlay_generation >= 0 "
            "AND length(review_git_sha) BETWEEN 40 AND 64)",
            name="ck_diff_break_glass_review_revision",
        ),
    )
