"""Worktree management built on real ``git worktree`` (spec §2.5, §7.3).

The previous implementation just ``shutil.copytree``-d the mirror into a
plan-specific directory. That had three problems flagged by the audit
team:

1. Every plan duplicated the full ``.git/objects`` tree on disk —
   linear cost in mirror size per plan, not per change.
2. Nothing prevented an analyzer container from mutating files inside
   the "worktree" — read-only intent was implicit.
3. The pre-receive-hook idea Team A floated for push-blocking was a
   dead end: analyzers don't ``git push``, they write into the
   directory; a hook on the mirror never fires.

This rewrite:

* uses ``git worktree add --detach <dst> <base_sha>`` against the
  bare mirror at ``_REPO_ROOT/<project_id>`` so worktrees share the
  object store,
* falls back to creating an empty directory when the mirror is not a
  git repo (development convenience, never the production path),
* exposes :func:`destroy_worktree` so plan teardown can call
  ``git worktree remove`` and free the lock entry,
* keeps :func:`resolve_in_worktree` for path-escape protection.

Read-only enforcement on the analyzer side is the responsibility of
``sandbox/runner.py`` — when the runner spawns an analyzer container
it now bind-mounts the worktree with ``ro`` and grants writable
``tmpfs`` under ``/scratch``. The platform itself still writes patches
into the worktree on the host filesystem (it has to, to call
``git apply``); read-only-ness applies to the analyzer subprocess.
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


def _mirror_path(project_id: uuid.UUID) -> Path:
    return _REPO_ROOT / str(project_id)


async def _run_git(*args: str, cwd: Path | None = None) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode or 0, stdout, stderr


async def _is_git_repo(path: Path) -> bool:
    if not path.exists():
        return False
    rc, _, _ = await _run_git("rev-parse", "--git-dir", cwd=path)
    return rc == 0


async def create_worktree(
    plan_id: uuid.UUID,
    project_id: uuid.UUID,
    base_sha: str | None = None,
) -> Path:
    """Materialise an isolated working copy for ``plan_id``.

    Idempotent: a second call with the same ``plan_id`` returns the
    existing worktree path so a retried Plan submission doesn't crash
    on "destination already exists".
    """
    mirror = _mirror_path(project_id)
    dst = worktree_path(plan_id)
    if dst.exists():
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)

    if await _is_git_repo(mirror):
        target = base_sha or "HEAD"
        rc, _, stderr = await _run_git(
            "-C", str(mirror), "worktree", "add", "--detach", str(dst), target
        )
        if rc != 0:
            raise RuntimeError(
                f"git worktree add failed (rc={rc}): {stderr.decode(errors='replace')}"
            )
        return dst

    # Fallback for environments that haven't yet mirrored the project
    # (dev shells, tests). Keep the previous behaviour so callers aren't
    # surprised: just create the directory.
    dst.mkdir(parents=True, exist_ok=True)
    return dst


async def destroy_worktree(plan_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Tear down a worktree created by :func:`create_worktree`.

    When the mirror is a real git repo we use ``git worktree remove
    --force`` so the lock entry under ``.git/worktrees`` is also
    cleared. Otherwise we fall back to a recursive ``rm``.
    """
    mirror = _mirror_path(project_id)
    dst = worktree_path(plan_id)
    if not dst.exists():
        return
    if await _is_git_repo(mirror):
        rc, _, _ = await _run_git(
            "-C", str(mirror), "worktree", "remove", "--force", str(dst)
        )
        if rc == 0:
            return
        # Worktree pointer may have been orphaned by an earlier crash;
        # `prune` cleans the metadata and we remove the directory below.
        await _run_git("-C", str(mirror), "worktree", "prune")
    if dst.exists():
        await asyncio.to_thread(shutil.rmtree, dst, ignore_errors=True)


async def compute_diff(plan_id: uuid.UUID) -> str:
    dst = worktree_path(plan_id)
    if not dst.exists():
        return ""
    # A fallback (no-mirror) worktree is just an empty directory and is
    # not a git repo; running ``git diff`` there prints the usage banner
    # to stderr. Match the original "empty diff" contract instead of
    # leaking the git CLI noise back to callers.
    if not await _is_git_repo(dst):
        return ""
    rc, stdout, stderr = await _run_git("diff", "--no-color", cwd=dst)
    if rc != 0:
        # Real git failure path — surface stderr so an operator can
        # see what broke. Empty stderr stays empty.
        return stderr.decode(errors="replace") if stderr else ""
    return stdout.decode("utf-8", errors="replace")


def resolve_in_worktree(plan_id: uuid.UUID, relative: str) -> Path:
    root = worktree_path(plan_id).resolve()
    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root) + "/") and candidate != root:
        raise ValueError("path_escapes_worktree")
    return candidate


# --- Analyzer mount helpers --------------------------------------------------


def analyzer_mount_args(plan_id: uuid.UUID) -> list[str]:
    """Docker arguments that expose the worktree to an analyzer.

    Used by ``sandbox/runner.py`` (or any future containerised
    analyzer launcher). The analyzer sees ``/work`` read-only and a
    writable ``/scratch`` tmpfs scoped to 512 MiB. Anything an
    analyzer needs to *persist* must come back through stdout per the
    analyzer contract — the writable tmpfs is for caches only.
    """
    dst = worktree_path(plan_id)
    return [
        "--read-only",
        "--mount",
        f"type=bind,src={dst},dst=/work,readonly",
        "--tmpfs",
        "/scratch:rw,size=512m",
    ]


__all__ = [
    "worktree_path",
    "create_worktree",
    "destroy_worktree",
    "compute_diff",
    "resolve_in_worktree",
    "analyzer_mount_args",
]
