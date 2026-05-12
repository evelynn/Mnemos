import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    approved_by: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    feedback: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DiffSubmission(Base):
    __tablename__ = "diff_submissions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id"), nullable=False
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
        UUID(as_uuid=True), ForeignKey("diff_submissions.id"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    issued_by: Mapped[str] = mapped_column(String, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    rerun_review_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
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
    )
