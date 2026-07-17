"""MR creation stub.

When GitLab configuration is present (base_url, token in platform_settings
under keys ``gitlab_base_url`` and ``gitlab_token``), create a branch and MR.
When it isn't, return a recorded-only dict so tests and demos can still run
end-to-end.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import PlatformSetting
from app.sandbox.worktree import (
    WorktreeDiffError,
    canonical_committed_diff,
    canonical_staged_diff,
)


@dataclass
class MRResult:
    ok: bool
    iid: int | None
    url: str | None
    message: str


class MRPayloadChanged(RuntimeError):
    """The Git index no longer matches the payload approved by Gate B."""


async def _get_setting(session: AsyncSession, key: str) -> str | None:
    row = (
        await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == key)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    val = row.value
    if isinstance(val, dict):
        return val.get("value") or val.get("secret")
    return str(val)


async def create_mr_from_worktree(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    worktree: Path,
    plan_title: str,
    task_id: str,
    description: str,
    expected_diff: str | None = None,
) -> MRResult:
    base_url = await _get_setting(session, "gitlab_base_url")
    token = await _get_setting(session, "gitlab_token")
    project_ref = await _get_setting(session, f"gitlab_project:{project_id}")

    if not (base_url and token and project_ref):
        return MRResult(
            ok=False,
            iid=None,
            url=None,
            message="gitlab_not_configured (configure base_url/token/project id in settings)",
        )

    branch = f"ai/{uuid.uuid4().hex[:10]}-{task_id}"[:64]

    async def _run(cmd: list[str]) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(worktree),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        return proc.returncode or 0, out.decode("utf-8", errors="replace")

    for cmd in [
        ["git", "checkout", "-b", branch],
        ["git", "add", "-A"],
    ]:
        code, _output = await _run(cmd)
        if code != 0:
            return MRResult(
                ok=False,
                iid=None,
                url=None,
                message="git_step_failed:prepare_index",
            )

    if expected_diff is not None:
        try:
            staged_diff = await canonical_staged_diff(worktree)
        except WorktreeDiffError as exc:
            raise MRPayloadChanged(
                "could not prove the final Git index matches the reviewed diff"
            ) from exc
        if staged_diff != expected_diff:
            raise MRPayloadChanged(
                "final Git index differs from the reviewed Gate-B payload"
            )

    # The commit consumes the exact index snapshot compared above. Verify the
    # resulting commit too: repository hooks may legally rewrite and re-stage
    # files during ``git commit``. Nothing is pushed until that final tree is
    # proven byte-for-byte equal to Gate B's payload.
    commit_cmd = [
        "git",
        "-c",
        "user.email=mnemos@local",
        "-c",
        "user.name=Mnemos",
        "commit",
        "-m",
        f"{plan_title} ({task_id})",
    ]
    code, _output = await _run(commit_cmd)
    if code != 0:
        return MRResult(
            ok=False,
            iid=None,
            url=None,
            message="git_step_failed:commit",
        )
    if expected_diff is not None:
        try:
            committed_diff = await canonical_committed_diff(worktree)
        except WorktreeDiffError as exc:
            await _run(["git", "reset", "--mixed", "HEAD^"])
            raise MRPayloadChanged(
                "could not prove the committed tree matches the reviewed diff"
            ) from exc
        if committed_diff != expected_diff:
            # Restore the authorised parent while preserving hook-modified
            # files for a fresh Gate-B review.
            await _run(["git", "reset", "--mixed", "HEAD^"])
            raise MRPayloadChanged(
                "committed tree differs from the reviewed Gate-B payload"
            )

    push_cmd = ["git", "push", "-u", "origin", branch]
    code, _output = await _run(push_cmd)
    if code != 0:
        return MRResult(
            ok=False,
            iid=None,
            url=None,
            message="git_step_failed:push",
        )

    # Create MR via python-gitlab
    try:
        from gitlab import Gitlab  # type: ignore

        gl = await asyncio.to_thread(Gitlab, base_url, private_token=token)
        gl_project = await asyncio.to_thread(gl.projects.get, project_ref)
        mr = await asyncio.to_thread(
            gl_project.mergerequests.create,
            {
                "source_branch": branch,
                "target_branch": "main",
                "title": plan_title,
                "description": description,
            },
        )
        return MRResult(ok=True, iid=mr.iid, url=mr.web_url, message="created")
    except Exception:  # noqa: BLE001
        return MRResult(ok=False, iid=None, url=None, message="gitlab_api_failed")
