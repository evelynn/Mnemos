import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
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
