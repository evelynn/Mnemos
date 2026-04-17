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


@dataclass
class MRResult:
    ok: bool
    iid: int | None
    url: str | None
    message: str


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
        ["git", "-c", "user.email=mnemos@local", "-c", "user.name=Mnemos", "commit", "-m", f"{plan_title} ({task_id})"],
        ["git", "push", "-u", "origin", branch],
    ]:
        code, output = await _run(cmd)
        if code != 0:
            return MRResult(
                ok=False, iid=None, url=None, message=f"git_step_failed: {' '.join(cmd)}: {output[:400]}"
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
    except Exception as exc:  # noqa: BLE001
        return MRResult(ok=False, iid=None, url=None, message=f"gitlab_api_failed: {exc}")
