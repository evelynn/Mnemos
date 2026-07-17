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
* Jobs use the worker's fixed queue.  Dynamic queue names are not discovered
  by ARQ workers and previously left every webhook run permanently queued.
  The worker is deliberately single-job until project-scoped locking exists.
* Only the project's configured default-branch ref is indexed. Mixing feature
  branch pushes into one current graph would flap facts and multiply load.
* MR open / merge events are *not* enqueued from here. Once the MR is
  merged the resulting commit shows up as a push to the target branch,
  which we already handle. Pre-merge "preview" analyses are out of
  scope for Phase 1.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.config import get_settings
from app.db import get_session
from app.models.auth import PlatformSetting
from app.models.graph import AnalysisRun
from app.models.projects import Project
from app.orchestrator.queue import get_queue

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
log = logging.getLogger(__name__)

_SETTING_KEY = "gitlab_webhook_secret"
# Label used when storing the GitLab webhook secret in the encrypted
# ``secrets`` table — spec §2.8 "no plaintext credentials". Resolution
# is encrypted-first; the legacy ``PlatformSetting`` JSON storage is
# read as a fallback so existing deployments keep working until they
# rotate the secret into the new table.
_SECRET_LABEL = "gitlab_webhook_secret"


async def _secret(db: AsyncSession) -> str | None:
    # Preferred path — encrypted Secret row, decrypted on use. Avoids
    # storing the HMAC key as a plaintext JSON setting (platform
    # review #12). Looked up by canonical label.
    from app.models.auth import Secret
    from app.safety.crypto import decrypt

    encrypted_rows = (
        await db.execute(
            select(Secret)
            .where(
                Secret.label == _SECRET_LABEL,
                Secret.organization_id.is_(None),
            )
            .limit(2)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    # Only platform-owned legacy rows are authoritative. Tenant-owned rows
    # using the reserved label are ignored even if they predate the CRUD
    # guard, and an ambiguous platform state fails closed.
    if len(encrypted_rows) > 1:
        log.error("multiple platform GitLab webhook secrets; refusing webhook auth")
        return None
    if encrypted_rows:
        enc = encrypted_rows[0]
        try:
            return decrypt(enc.ciphertext, enc.iv)
        except Exception:  # noqa: BLE001
            # A configured encrypted secret that cannot decrypt is ambiguous
            # authority. Do not silently fall back to plaintext legacy state.
            log.error(
                "gitlab_webhook_secret decrypt failed "
                "failure_code=webhook_secret_unavailable"
            )
            return None

    row = (
        await db.execute(
            select(PlatformSetting).where(PlatformSetting.key == _SETTING_KEY)
        )
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


def _default_branch_ref(default_branch: str) -> str:
    branch = default_branch.strip()
    if branch.startswith("refs/heads/"):
        return branch
    return f"refs/heads/{branch}"


@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    x_gitlab_event_uuid: str | None = Header(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    expected = await _secret(db)
    # Fail-closed: a webhook secret MUST be configured. The earlier
    # "if expected" was an open default — a fresh install with the
    # setting unset accepted forged GitLab events and enqueued
    # analyses against any project id in the payload (§2.5).
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="webhook_secret_not_configured",
        )
    if not x_gitlab_token or not hmac.compare_digest(x_gitlab_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_webhook_token"
        )

    body = await request.json()
    object_kind = body.get("object_kind")
    project_path = (body.get("project") or {}).get("path_with_namespace")
    ref: str | None = None
    default_branch_ref: str | None = None
    before: str | None = None
    after: str | None = None

    enqueued: dict[str, Any] | None = None
    # For a push event that fails to enqueue, this records *why* — a
    # silent 200 left an operator whose project's gitlab_project_id is
    # unset wondering why no analysis ever ran (§2.7 always-on).
    skip_reason: str | None = None
    if object_kind == "tag_push":
        # A tag points at a commit already on a branch we analyse via
        # its push event; re-analysing it would just be a redundant
        # run (and its all-zero ``before`` SHA defeats job dedup).
        skip_reason = "tag_push_no_new_commits"
    elif object_kind == "push":
        project = await _resolve_project(db, body)
        before = str(body.get("before") or "")
        after = str(body.get("after") or "")
        ref = str(body.get("ref") or "")
        if project is not None:
            default_branch_ref = _default_branch_ref(project.default_branch)
        if project is None:
            skip_reason = "project_not_registered"
        elif not after or not ref:
            skip_reason = "malformed_push_payload"
        elif ref != default_branch_ref:
            # The persisted graph represents the configured default branch.
            # Enqueuing feature-branch pushes would make current facts flap
            # between unrelated histories and multiply load during bursts.
            skip_reason = "non_default_branch"
        elif not get_settings().source_mirror_root.strip():
            skip_reason = "source_mirror_not_configured"
        if project is not None and after and ref and skip_reason is None:
            job_id = _job_id(project.id, before, after, ref)
            run = AnalysisRun(
                id=uuid.uuid4(),
                project_id=project.id,
                status="queued",
                triggered_by=f"webhook:gitlab:{x_gitlab_event_uuid or 'unknown'}",
                git_sha=after,
                scope="incremental",
                stats={
                    "job_id": job_id,
                    "queued_mode": "deterministic_index",
                    "source": "configured_git_mirror",
                    "webhook_ref": ref,
                    "default_branch_ref": default_branch_ref,
                },
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)

            try:
                queue = await get_queue()
                job = await queue.enqueue_job(
                    "run_ingest",
                    str(project.id),
                    str(run.id),
                    # Webhook-driven runs don't ship a source_path — the worker
                    # checks out the mirror at ``after`` itself.
                    "",
                    {
                        "scope": "incremental",
                        "git_sha": after,
                        "ref": ref,
                        "summarize": False,
                        "agent_extract_limit": 0,
                    },
                    _job_id=job_id,
                    # Small defer lets the GitLab side flush any post-receive
                    # state before the analyzer mirrors the new commits.
                    _defer_by=5,
                )
            except asyncio.CancelledError:
                run.status = "failed"
                run.completed_at = datetime.now(tz=timezone.utc)
                run.error_log = "analysis_enqueue_cancelled"
                await asyncio.shield(db.commit())
                raise
            except Exception:  # noqa: BLE001 — terminalize the row
                log.error(
                    "webhook analysis enqueue failed run_id=%s "
                    "failure_code=analysis_enqueue_failed",
                    run.id,
                )
                run.status = "failed"
                run.completed_at = datetime.now(tz=timezone.utc)
                run.error_log = "analysis_enqueue_failed"
                await db.commit()
                skip_reason = "analysis_enqueue_failed"
            else:
                if job is None:
                    # ARQ refused a duplicate id. Keep an auditable terminal
                    # row, never a queued ghost with no backing job.
                    run.status = "cancelled"
                    run.completed_at = datetime.now(tz=timezone.utc)
                    run.error_log = "duplicate_webhook_job"
                    await db.commit()
                    skip_reason = "duplicate_push"
                else:
                    enqueued = {
                        "run_id": str(run.id),
                        "ref": ref,
                        "after": after,
                        "before": before,
                    }

    # A push that should have enqueued but didn't gets a distinct
    # ``webhook.skipped`` action so an operator can filter the audit
    # log for misrouted hooks instead of trawling every ``received``.
    action = "webhook.skipped" if skip_reason else "webhook.received"
    await audit_record(
        actor="gitlab",
        action=action,
        target=x_gitlab_event or "unknown",
        details={
            "event": x_gitlab_event,
            "event_uuid": x_gitlab_event_uuid,
            "object_kind": object_kind,
            "project": project_path,
            "ref": ref,
            "default_branch_ref": default_branch_ref,
            "before": before,
            "after": after,
            "enqueued": enqueued,
            "skip_reason": skip_reason,
        },
    )

    return {
        "status": "received",
        "enqueued": enqueued is not None,
        "skip_reason": skip_reason,
    }
