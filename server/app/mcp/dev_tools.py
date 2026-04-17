"""Dev-mode MCP tool implementations (spec §11.5)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.queries import impact_analysis
from app.models.plans import DiffSubmission, Plan
from app.safety.self_review import review_diff, to_jsonable
from app.sandbox.runner import run_in_sandbox
from app.sandbox.worktree import (
    compute_diff,
    create_worktree,
    resolve_in_worktree,
    worktree_path,
)


async def submit_plan(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    spec: dict[str, Any],
    tasks: list[dict[str, Any]],
    target_component_id: str,
    requester: str,
) -> dict[str, Any]:
    impacts = await impact_analysis(
        session, project_id=project_id, symbol_id=target_component_id, max_depth=3
    )
    plan = Plan(
        project_id=project_id,
        spec=spec,
        tasks=tasks,
        impact_report={
            "directly_affected": impacts["directly_affected"],
            "transitively_affected": impacts["transitively_affected"],
            "opaque_components_touched": impacts["opaque_components_touched"],
        },
        requester=requester,
    )
    session.add(plan)
    await session.commit()
    await session.refresh(plan)

    worktree = await create_worktree(plan.id, project_id)
    plan.worktree_path = str(worktree)
    await session.commit()

    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "gate_a_url": f"/plans#{plan.id}",
        "worktree_path": plan.worktree_path,
        "impact_report": plan.impact_report,
    }


async def edit_file_in_worktree(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    file_path: str,
    edits: list[dict[str, Any]],
) -> dict[str, Any]:
    plan = (
        await session.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return {"error": "plan_not_found"}
    if plan.status not in {"approved", "executing"}:
        return {"error": "plan_not_approved"}
    try:
        path = resolve_in_worktree(plan_id, file_path)
    except ValueError as exc:
        return {"error": str(exc)}

    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text() if path.exists() else ""
    for edit in edits:
        old = edit.get("old_str")
        new = edit.get("new_str")
        if old is not None and new is not None:
            if old not in text:
                return {"error": f"old_str_not_found: {old[:60]}"}
            text = text.replace(old, new, 1)
        elif "replace_range" in edit and "new_text" in edit:
            rng = edit["replace_range"]
            text = text[: rng["start"]] + edit["new_text"] + text[rng["end"] :]
        else:
            return {"error": "edit_missing_old_new_or_range"}
    path.write_text(text)
    diff = await compute_diff(plan_id)
    plan.status = "executing"
    await session.commit()
    return {"success": True, "resulting_diff": diff[:20000]}


async def submit_diff(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    task_id: str,
    diff: str | None = None,
    test_results: dict[str, Any] | None = None,
    self_review_notes: str | None = None,
) -> dict[str, Any]:
    plan = (
        await session.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return {"error": "plan_not_found"}
    if diff is None:
        diff = await compute_diff(plan_id)

    review = review_diff(diff)
    submission = DiffSubmission(
        plan_id=plan_id,
        task_id=task_id,
        diff=diff,
        test_results=test_results,
        self_review_notes=self_review_notes,
        auto_review_findings=to_jsonable(review),
    )
    session.add(submission)
    await session.commit()
    await session.refresh(submission)
    return {
        "submission_id": str(submission.id),
        "status": submission.status,
        "gate_b_url": f"/diffs#{submission.id}",
        "auto_review_findings": to_jsonable(review),
    }


async def run_in_sandbox_tool(
    session: AsyncSession,
    *,
    plan_id: uuid.UUID,
    command: str,
    timeout_sec: int = 300,
) -> dict[str, Any]:
    plan = (
        await session.execute(select(Plan).where(Plan.id == plan_id))
    ).scalar_one_or_none()
    if plan is None:
        return {"error": "plan_not_found"}
    if plan.status not in {"approved", "executing"}:
        return {"error": "plan_not_approved"}
    result = await run_in_sandbox(
        command=command,
        working_dir=worktree_path(plan_id),
        timeout_sec=max(5, min(timeout_sec, 1800)),
    )
    return {
        "exit_code": result.exit_code,
        "stdout": result.stdout[:20000],
        "stderr": result.stderr[:20000],
        "duration_ms": result.duration_ms,
    }
