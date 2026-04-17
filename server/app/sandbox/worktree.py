"""Minimal worktree management for AI edits.

Phase 1 clones a project's repo mirror into /var/lib/mnemos/worktrees/<plan>/
and computes unified diffs with ``git diff``. Full libgit2 integration is
Phase 2 — shelling out keeps the surface tiny.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path

_WORKTREE_ROOT = Path("/var/lib/mnemos/worktrees")
_REPO_ROOT = Path("/var/lib/mnemos/repos")


def worktree_path(plan_id: uuid.UUID) -> Path:
    return _WORKTREE_ROOT / str(plan_id)


async def create_worktree(plan_id: uuid.UUID, project_id: uuid.UUID) -> Path:
    src = _REPO_ROOT / str(project_id)
    dst = worktree_path(plan_id)
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.exists():
        await asyncio.to_thread(shutil.copytree, src, dst, dirs_exist_ok=True)
    else:
        dst.mkdir(parents=True, exist_ok=True)
    return dst


async def compute_diff(plan_id: uuid.UUID) -> str:
    dst = worktree_path(plan_id)
    if not dst.exists():
        return ""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--no-color",
        cwd=str(dst),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8", errors="replace")


def resolve_in_worktree(plan_id: uuid.UUID, relative: str) -> Path:
    root = worktree_path(plan_id).resolve()
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root) + "/") and candidate != root:
        raise ValueError("path_escapes_worktree")
    return candidate
