"""GitLab webhook receiver — verifies the signature and enqueues an
incremental analysis run.

Spec §1.5 / §2.7: the platform is an always-on service that re-analyses
on every push. The previous implementation only audited the event and
returned 200 — Week 3 work in the original roadmap.

Design notes:

* The idempotency key the ARQ pool uses to dedupe duplicate webhooks is
  ``sha256(project_id | before | after | ref)``. We deliberately include
  ``before`` so a force-push that re-uses an old ``after`` SHA (e.g. a
  revert-of-revert) still creates a fresh job instead of being eaten by
  ARQ's dedup window.
* Per-branch jobs ride a dedicated ``_queue_name`` so a worker only
  processes one push for a given branch at a time. This preserves the
  user-visible ordering "the analysis you see is the one for the latest
  push you sent on this branch".
* MR open / merge events are *not* enqueued from here. Once the MR is
  merged the resulting commit shows up as a push to the target branch,
  which we already handle. Pre-merge "preview" analyses are out of
  scope for Phase 1.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.db import get_session
from app.models.auth import PlatformSetting
from app.models.graph import AnalysisRun
from app.models.projects import Project
from app.orchestrator.queue import get_queue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

_SETTING_KEY = "gitlab_webhook_secret"


async def _secret(db: AsyncSession) -> str | None:
    row = (
        await db.execute(select(PlatformSetting).where(PlatformSetting.key == _SETTING_KEY))
    ).scalar_one_or_none()
    if row is None:
        return None
    value = row.value
    if isinstance(value, dict):
        return value.get("secret")
    return str(value) if value else None


async def _resolve_project(
    db: AsyncSession, payload: dict[str, Any]
) -> Project | None:
    """Match the webhook to a Project row.

    GitLab webhooks include the numeric project id under ``project.id``
    and the namespaced path under ``project.path_with_namespace``. We
    prefer the numeric id because it survives renames; the path is a
    fallback for installs that pre-date our storing it.
    """
    proj_node = payload.get("project") or {}
    gitlab_pid = proj_node.get("id")
    if isinstance(gitlab_pid, int):
        row = (
            await db.execute(
                select(Project).where(Project.gitlab_project_id == gitlab_pid)
            )
        ).scalar_one_or_none()
        if row is not None:
            return row
    return None


def _job_id(project_id: uuid.UUID, before: str, after: str, ref: str) -> str:
    """Idempotency key for ARQ ``enqueue_job(_job_id=...)``.

    Including ``before`` is the bit that makes force-push handling
    correct: ARQ dedups by ``_job_id`` for a brief window, so two
    pushes that happen to share the resulting ``after`` SHA would
    otherwise produce only one analysis.
    """
    raw = f"{project_id}|{before}|{after}|{ref}".encode("utf-8")
    return "webhook:" + hashlib.sha256(raw).hexdigest()


def _queue_name(project_id: uuid.UUID, ref: str) -> str:
    """Per-branch serialization queue.

    A single worker dequeues from this name at most once at a time so
    two near-simultaneous pushes on the same branch don't race each
    other and clobber an in-flight analysis run's outputs.
    """
    return f"ingest:{project_id}:{ref}"


@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    x_gitlab_event_uuid: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    expected = await _secret(db)
    if expected:
        if not x_gitlab_token or not hmac.compare_digest(x_gitlab_token, expected):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_webhook_token"
            )

    body = await request.json()
    object_kind = body.get("object_kind")
    project_path = (body.get("project") or {}).get("path_with_namespace")

    enqueued: dict[str, Any] | None = None
    if object_kind in ("push", "tag_push"):
        project = await _resolve_project(db, body)
        before = str(body.get("before") or "")
        after = str(body.get("after") or "")
        ref = str(body.get("ref") or "")
        if project is not None and after and ref:
            run = AnalysisRun(
                project_id=project.id,
                status="queued",
                triggered_by=f"webhook:gitlab:{x_gitlab_event_uuid or 'unknown'}",
                git_sha=after,
                scope="incremental",
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)

            queue = await get_queue()
            await queue.enqueue_job(
                "run_ingest",
                str(project.id),
                str(run.id),
                # Webhook-driven runs don't ship a source_path — the worker
                # checks out the mirror at ``after`` itself.
                "",
                {"scope": "incremental", "git_sha": after, "ref": ref},
                _job_id=_job_id(project.id, before, after, ref),
                _queue_name=_queue_name(project.id, ref),
                # Small defer lets the GitLab side flush any post-receive
                # state before the analyzer mirrors the new commits.
                _defer_by=5,
            )
            enqueued = {
                "run_id": str(run.id),
                "ref": ref,
                "after": after,
                "before": before,
            }

    await audit_record(
        actor="gitlab",
        action="webhook.received",
        target=x_gitlab_event or "unknown",
        details={
            "event": x_gitlab_event,
            "event_uuid": x_gitlab_event_uuid,
            "object_kind": object_kind,
            "project": project_path,
            "enqueued": enqueued,
        },
    )

    return {"status": "received", "enqueued": enqueued is not None}
