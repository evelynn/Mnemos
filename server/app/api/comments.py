"""Comment endpoints (PR-43, closes audit C4).

A polymorphic comment surface for plans and diff_submissions.
Any authenticated user in the target's organisation can post +
read comments; only the author or an admin can edit / delete.

The audit log records every state change so an after-the-fact
review can reconstruct who commented when, including soft-edited
content.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.auth import User
from app.models.comments import VALID_TARGET_KINDS, Comment
from app.models.plans import DiffSubmission, Plan
from app.models.projects import Project

router = APIRouter(tags=["comments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class CommentOut(BaseModel):
    id: uuid.UUID
    target_kind: str
    target_id: uuid.UUID
    author_id: Optional[uuid.UUID]
    author_username: Optional[str]
    author_display_name: Optional[str]
    body: str
    created_at: datetime
    edited_at: Optional[datetime]


class CommentCreate(BaseModel):
    body: str = Field(..., min_length=1, max_length=4000)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _check_target_in_user_org(
    db: AsyncSession,
    kind: str,
    target_id: uuid.UUID,
    user: User,
) -> None:
    """Refuse comments on a target outside the caller's org."""
    if kind == "plan":
        row = (
            await db.execute(
                select(Plan, Project)
                .join(Project, Project.id == Plan.project_id)
                .where(Plan.id == target_id)
            )
        ).first()
        if row is None or row[1].organization_id != user.organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    elif kind == "diff_submission":
        row = (
            await db.execute(
                select(DiffSubmission, Plan, Project)
                .join(Plan, Plan.id == DiffSubmission.plan_id)
                .join(Project, Project.id == Plan.project_id)
                .where(DiffSubmission.id == target_id)
            )
        ).first()
        if row is None or row[2].organization_id != user.organization_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_target_kind",
        )


async def _out(c: Comment, db: AsyncSession) -> CommentOut:
    author = None
    if c.author_id is not None:
        author = (
            await db.execute(select(User).where(User.id == c.author_id))
        ).scalar_one_or_none()
    return CommentOut(
        id=c.id,
        target_kind=c.target_kind,
        target_id=c.target_id,
        author_id=c.author_id,
        author_username=author.username if author else None,
        author_display_name=author.display_name if author else None,
        body=c.body,
        created_at=c.created_at,
        edited_at=c.edited_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/v1/comments")
async def list_comments(
    target_kind: str,
    target_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[CommentOut]:
    if target_kind not in VALID_TARGET_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_target_kind",
        )
    await _check_target_in_user_org(db, target_kind, target_id, user)
    rows = (
        await db.execute(
            select(Comment)
            .where(Comment.target_kind == target_kind)
            .where(Comment.target_id == target_id)
            .order_by(Comment.created_at.asc())
        )
    ).scalars().all()
    return [await _out(c, db) for c in rows]


@router.post("/api/v1/comments", status_code=status.HTTP_201_CREATED)
async def create_comment(
    body: CommentCreate,
    target_kind: str,
    target_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> CommentOut:
    if target_kind not in VALID_TARGET_KINDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="invalid_target_kind",
        )
    await _check_target_in_user_org(db, target_kind, target_id, user)
    c = Comment(
        target_kind=target_kind,
        target_id=target_id,
        author_id=user.id,
        body=body.body,
    )
    db.add(c)
    await db.commit()
    await db.refresh(c)
    await audit_record(
        actor=f"user:{user.id}",
        action="comment.created",
        target=f"{target_kind}:{target_id}",
        details={"comment_id": str(c.id)},
    )
    return await _out(c, db)


@router.patch("/api/v1/comments/{comment_id}")
async def edit_comment(
    comment_id: uuid.UUID,
    body: CommentCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> CommentOut:
    c = (
        await db.execute(select(Comment).where(Comment.id == comment_id))
    ).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    # 404 before the author check so a cross-org admin can't even
    # confirm the comment exists.
    await _check_target_in_user_org(db, c.target_kind, c.target_id, user)
    # Author can edit; admin can edit any.
    if c.author_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not_comment_author"
        )
    c.body = body.body
    c.edited_at = datetime.now(tz=timezone.utc)
    await db.commit()
    await db.refresh(c)
    await audit_record(
        actor=f"user:{user.id}",
        action="comment.edited",
        target=f"comment:{c.id}",
        details={},
    )
    return await _out(c, db)


@router.delete("/api/v1/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comment(
    comment_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> None:
    c = (
        await db.execute(select(Comment).where(Comment.id == comment_id))
    ).scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    await _check_target_in_user_org(db, c.target_kind, c.target_id, user)
    if c.author_id != user.id and user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="not_comment_author"
        )
    await db.delete(c)
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="comment.deleted",
        target=f"comment:{c.id}",
        details={},
    )
