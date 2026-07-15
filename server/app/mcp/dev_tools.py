"""Dev-mode MCP tool implementations (spec §11.5)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_publication import GraphGenerationChanged, GraphPublicationError
from app.mcp.auth import (
    MCPAuthContext,
    MCPAuthenticationError,
    mcp_authentication_error,
    revalidate_mcp_auth_context,
)
from app.mcp.queries import impact_analysis
from app.models.plans import DiffSubmission, Plan
from app.plan_provenance import (
    PLAN_BASE_REVISION_MISMATCH_CODE,
    PLAN_SOURCE_REVISION_STALE_CODE,
    PLAN_SOURCE_REVISION_UNAVAILABLE_CODE,
    PlanBaseRevisionMismatch,
    PlanSourceRevision,
    PlanSourceRevisionStale,
    PlanSourceRevisionUnavailable,
    bind_plan_source_revision,
    lock_current_plan_for_action,
    lock_current_plan_source_revision,
)
from app.review_revision import (
    bind_review_revision,
    lock_review_graph_revision,
    read_review_graph_stamp,
)
from app.safety.review import run_pipeline
from app.sandbox.runner import run_in_sandbox
from app.sandbox.worktree import (
    WorktreeRevisionError,
    WorktreeRevisionMismatch,
    compute_diff,
    create_worktree,
    destroy_worktree,
    resolve_in_worktree,
    verify_worktree_revision,
    worktree_path,
)


def _revision_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PlanBaseRevisionMismatch):
        code = PLAN_BASE_REVISION_MISMATCH_CODE
    elif isinstance(exc, PlanSourceRevisionStale):
        code = PLAN_SOURCE_REVISION_STALE_CODE
    else:
        code = PLAN_SOURCE_REVISION_UNAVAILABLE_CODE
    return {
        "schema": "mnemos.error.v1",
        "error": code,
        "reason": str(exc),
        "retryable": isinstance(exc, PlanSourceRevisionUnavailable),
    }


def _worktree_meta(revision: PlanSourceRevision) -> dict[str, Any]:
    return {
        "base_sha": revision.git_sha,
        "source_run_id": str(revision.run_id),
        "source_graph_generation": revision.source_generation,
        "source_overlay_generation": revision.overlay_generation,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


async def _revalidate_mutation_auth(
    session: AsyncSession,
    *,
    context: MCPAuthContext,
    expected_project_id: uuid.UUID,
) -> None:
    """Lock the exact MCP identity/project boundary through the next commit."""

    if context.project_id != expected_project_id:
        raise MCPAuthenticationError("invalid_mcp_credentials")
    await revalidate_mcp_auth_context(
        session,
        context=context,
        lock=True,
    )


async def _plan_project_id(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    expected_project_id: uuid.UUID,
) -> uuid.UUID | None:
    value = (
        await session.execute(
            select(Plan.project_id).where(
                Plan.id == plan_id,
                Plan.project_id == expected_project_id,
            )
        )
    ).scalar_one_or_none()
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def _verify_plan_worktree(
    plan: Plan,
    revision: PlanSourceRevision,
) -> None:
    expected_path = worktree_path(plan.id)
    if plan.worktree_path != str(expected_path):
        raise WorktreeRevisionMismatch(
            "stored Plan worktree path is not the plan-scoped path"
        )
    await verify_worktree_revision(plan.id, revision.git_sha)


def _worktree_revision_error(exc: WorktreeRevisionError) -> dict[str, Any]:
    mismatch = isinstance(exc, WorktreeRevisionMismatch)
    return {
        "schema": "mnemos.error.v1",
        "error": (
            "plan_worktree_revision_mismatch"
            if mismatch
            else "plan_worktree_revision_unavailable"
        ),
        "reason": str(exc),
        "retryable": not mismatch,
    }


async def submit_plan(
    session: AsyncSession,
    *,
    auth_context: MCPAuthContext,
    project_id: uuid.UUID,
    spec: dict[str, Any],
    tasks: list[dict[str, Any]],
    target_component_id: str,
    requester: str,
    base_sha: str | None = None,
) -> dict[str, Any]:
    plan = Plan(
        id=uuid.uuid4(),
        project_id=project_id,
        spec=spec,
        tasks=tasks,
        requester=requester,
    )
    worktree_created = False
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=project_id,
        )
        revision = await lock_current_plan_source_revision(
            session,
            project_id=project_id,
            requested_base_sha=base_sha,
        )
        impacts = await impact_analysis(
            session,
            project_id=project_id,
            symbol_id=target_component_id,
            max_depth=3,
        )
        plan.impact_report = {
            "directly_affected": impacts["directly_affected"],
            "transitively_affected": impacts["transitively_affected"],
            "opaque_components_touched": impacts["opaque_components_touched"],
        }
        bind_plan_source_revision(plan, revision)
        session.add(plan)
        await session.flush()
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=project_id,
        )
        worktree = await create_worktree(
            plan.id,
            project_id,
            base_sha=revision.git_sha,
        )
        worktree_created = True
        plan.worktree_path = str(worktree)
        plan.worktree_meta = _worktree_meta(revision)
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=project_id,
        )
        await session.commit()
    except MCPAuthenticationError:
        await session.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        return mcp_authentication_error()
    except (
        PlanBaseRevisionMismatch,
        PlanSourceRevisionUnavailable,
    ) as exc:
        await session.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        return _revision_error(exc)
    except WorktreeRevisionError as exc:
        await session.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        return _worktree_revision_error(exc)
    except Exception:
        await session.rollback()
        if worktree_created:
            await destroy_worktree(plan.id, project_id)
        raise

    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "gate_a_url": f"/plans#{plan.id}",
        "worktree_path": plan.worktree_path,
        "impact_report": plan.impact_report,
        "source_revision": {
            "run_id": str(revision.run_id),
            "git_sha": revision.git_sha,
            "source_generation": revision.source_generation,
            "overlay_generation": revision.overlay_generation,
        },
    }


async def edit_file_in_worktree(
    session: AsyncSession,
    *,
    auth_context: MCPAuthContext,
    expected_project_id: uuid.UUID,
    plan_id: uuid.UUID,
    file_path: str,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    project_id = await _plan_project_id(
        session,
        plan_id,
        expected_project_id=expected_project_id,
    )
    if project_id is None:
        return {"error": "plan_not_found"}
    try:
        plan, revision = await lock_current_plan_for_action(
            session,
            plan_id=plan_id,
            project_id=project_id,
        )
    except (PlanSourceRevisionStale, PlanSourceRevisionUnavailable) as exc:
        await session.rollback()
        return _revision_error(exc)
    if plan.status not in {"approved", "executing"}:
        await session.rollback()
        return {"error": "plan_not_approved"}
    try:
        await _verify_plan_worktree(plan, revision)
    except WorktreeRevisionError as exc:
        await session.rollback()
        return _worktree_revision_error(exc)
    try:
        path = resolve_in_worktree(plan_id, file_path)
    except ValueError as exc:
        await session.rollback()
        return {"error": str(exc)}

    existed = path.exists()
    original = path.read_text() if existed else ""
    text = original
    for edit in edits:
        old = edit.get("old_str")
        new = edit.get("new_str")
        if old is not None and new is not None:
            if old not in text:
                await session.rollback()
                return {"error": f"old_str_not_found: {old[:60]}"}
            text = text.replace(old, new, 1)
        elif "replace_range" in edit and "new_text" in edit:
            rng = edit["replace_range"]
            text = text[: rng["start"]] + edit["new_text"] + text[rng["end"] :]
        else:
            await session.rollback()
            return {"error": "edit_missing_old_new_or_range"}
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        diff = await compute_diff(plan_id)
        plan.status = "executing"
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
        await session.commit()
    except MCPAuthenticationError:
        if existed:
            path.write_text(original)
        elif path.exists():
            path.unlink()
        await session.rollback()
        return mcp_authentication_error()
    except Exception:
        if existed:
            path.write_text(original)
        elif path.exists():
            path.unlink()
        await session.rollback()
        raise
    return {
        "success": True,
        "resulting_diff": diff[:20000],
        "source_revision": {
            "run_id": str(revision.run_id),
            "git_sha": revision.git_sha,
            "source_generation": revision.source_generation,
            "overlay_generation": revision.overlay_generation,
        },
    }


async def submit_diff(
    session: AsyncSession,
    *,
    auth_context: MCPAuthContext,
    expected_project_id: uuid.UUID,
    plan_id: uuid.UUID,
    task_id: str,
    diff: str | None = None,
    test_results: dict[str, Any] | None = None,
    self_review_notes: str | None = None,
) -> dict[str, Any]:
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    project_id = await _plan_project_id(
        session,
        plan_id,
        expected_project_id=expected_project_id,
    )
    if project_id is None:
        return {"error": "plan_not_found"}
    try:
        plan, revision = await lock_current_plan_for_action(
            session,
            plan_id=plan_id,
            project_id=project_id,
        )
    except (PlanSourceRevisionStale, PlanSourceRevisionUnavailable) as exc:
        await session.rollback()
        return _revision_error(exc)
    if plan.status not in {"approved", "executing"}:
        await session.rollback()
        return {"error": "plan_not_approved"}
    try:
        await _verify_plan_worktree(plan, revision)
    except WorktreeRevisionError as exc:
        await session.rollback()
        return _worktree_revision_error(exc)
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    # The initial check protects compute_diff without holding a database lock
    # through the potentially long review. The exact Plan/worktree revision is
    # locked and checked again after the review stamp revalidation below.
    await session.rollback()
    if diff is None:
        diff = await compute_diff(plan_id)

    try:
        review_stamp = await read_review_graph_stamp(
            session,
            project_id=project_id,
        )
        review = await run_pipeline(
            session,
            project_id=project_id,
            plan_id=plan_id,
            diff=diff,
        )
        # Gate-B deliberately releases its pre-review transaction.  Rebind the
        # immutable identity now, before GraphHead/Plan locks, and retain this
        # auth lock through the eventual DiffSubmission commit.  Revocation,
        # disablement, or role changes that win during review therefore cancel
        # persistence without reversing the global lock order (Auth -> Graph ->
        # Plan).
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
        review_revision = await lock_review_graph_revision(
            session,
            project_id=project_id,
            expected_stamp=review_stamp,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    except GraphGenerationChanged as exc:
        await session.rollback()
        return {
            "schema": "mnemos.error.v1",
            "error": "graph_revision_changed_during_review",
            "reason": str(exc),
            "retryable": True,
        }
    except GraphPublicationError as exc:
        await session.rollback()
        return {
            "schema": "mnemos.error.v1",
            "error": "canonical_graph_revision_unavailable",
            "reason": str(exc),
            "retryable": True,
        }
    try:
        plan, revision = await lock_current_plan_for_action(
            session,
            plan_id=plan_id,
            project_id=project_id,
        )
    except (PlanSourceRevisionStale, PlanSourceRevisionUnavailable) as exc:
        await session.rollback()
        return _revision_error(exc)
    if plan.status not in {"approved", "executing"}:
        await session.rollback()
        return {"error": "plan_not_approved"}
    try:
        await _verify_plan_worktree(plan, revision)
    except WorktreeRevisionError as exc:
        await session.rollback()
        return _worktree_revision_error(exc)
    if await compute_diff(plan_id) != diff:
        await session.rollback()
        return {
            "schema": "mnemos.error.v1",
            "error": "worktree_diff_changed_since_review",
            "reason": "worktree diff changed while Gate-B review was running",
            "retryable": True,
        }
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    review_findings = [finding.as_jsonable() for finding in review.findings]
    submission = DiffSubmission(
        plan_id=plan_id,
        task_id=task_id,
        diff=diff,
        test_results=test_results,
        self_review_notes=self_review_notes,
        auto_review_findings=review_findings,
        status="blocked" if review.verdict == "blocked" else "pending_approval",
    )
    bind_review_revision(submission, review_revision)
    session.add(submission)
    await session.commit()
    await session.refresh(submission)
    return {
        "submission_id": str(submission.id),
        "status": submission.status,
        "gate_b_url": f"/diffs#{submission.id}",
        "auto_review_findings": review_findings,
        "review_revision": {
            "run_id": str(review_revision.run_id),
            "git_sha": review_revision.git_sha,
            "source_generation": review_revision.source_generation,
            "overlay_generation": review_revision.overlay_generation,
        },
        "source_revision": {
            "run_id": str(revision.run_id),
            "git_sha": revision.git_sha,
            "source_generation": revision.source_generation,
            "overlay_generation": revision.overlay_generation,
        },
    }


async def run_in_sandbox_tool(
    session: AsyncSession,
    *,
    auth_context: MCPAuthContext,
    expected_project_id: uuid.UUID,
    plan_id: uuid.UUID,
    command: str,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    project_id = await _plan_project_id(
        session,
        plan_id,
        expected_project_id=expected_project_id,
    )
    if project_id is None:
        return {"error": "plan_not_found"}
    try:
        plan, revision = await lock_current_plan_for_action(
            session,
            plan_id=plan_id,
            project_id=project_id,
        )
    except (PlanSourceRevisionStale, PlanSourceRevisionUnavailable) as exc:
        await session.rollback()
        return _revision_error(exc)
    if plan.status not in {"approved", "executing"}:
        await session.rollback()
        return {"error": "plan_not_approved"}
    try:
        await _verify_plan_worktree(plan, revision)
    except WorktreeRevisionError as exc:
        await session.rollback()
        return _worktree_revision_error(exc)
    try:
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
        result = await run_in_sandbox(
            command=command,
            working_dir=worktree_path(plan_id),
            timeout_sec=max(5, min(timeout_sec, 1800)),
        )
        # End the same lock boundary only after the sandbox process exits.
        # A stale Plan can therefore never start under one publication and
        # report success after a different publication wins the head lock.
        await _revalidate_mutation_auth(
            session,
            context=auth_context,
            expected_project_id=expected_project_id,
        )
        await session.commit()
    except MCPAuthenticationError:
        await session.rollback()
        return mcp_authentication_error()
    except Exception:
        await session.rollback()
        raise
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout[:20000],
        "stderr": result.stderr[:20000],
        "duration_ms": result.duration_ms,
        "source_revision": {
            "run_id": str(revision.run_id),
            "git_sha": revision.git_sha,
            "source_generation": revision.source_generation,
            "overlay_generation": revision.overlay_generation,
        },
    }
