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
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

_WORKTREE_ROOT = Path("/var/lib/mnemos/worktrees")
_REPO_ROOT = Path("/var/lib/mnemos/repos")
_CANONICAL_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")


class WorktreeRevisionError(RuntimeError):
    """A Plan worktree could not prove its immutable source commit."""


class WorktreeRevisionUnavailable(WorktreeRevisionError):
    """The requested published commit is unavailable in the mirror."""


class WorktreeRevisionMismatch(WorktreeRevisionError):
    """The materialised worktree HEAD differs from the authorised commit."""


class WorktreeDiffError(WorktreeRevisionError):
    """The exact tree that Git would commit could not be materialised."""


def worktree_path(plan_id: uuid.UUID) -> Path:
    return _WORKTREE_ROOT / str(plan_id)


def _mirror_path(project_id: uuid.UUID) -> Path:
    plain = _REPO_ROOT / str(project_id)
    suffixed = _REPO_ROOT / f"{project_id}.git"
    return plain if plain.exists() or not suffixed.exists() else suffixed


async def _run_git(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd) if cwd else None,
        env=env,
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


async def _resolved_head(path: Path) -> str | None:
    """Return the canonical worktree commit or None for an invalid tree."""

    if not await _is_git_repo(path):
        return None
    rc, stdout, _ = await _run_git(
        "rev-parse",
        "--verify",
        "HEAD^{commit}",
        cwd=path,
    )
    if rc != 0:
        return None
    resolved = stdout.decode("ascii", errors="ignore").strip()
    return resolved if _CANONICAL_COMMIT_RE.fullmatch(resolved) else None


async def _discard_failed_worktree(mirror: Path, dst: Path) -> None:
    """Remove only the plan-scoped destination after failed verification."""

    await _run_git(
        "-C",
        str(mirror),
        "worktree",
        "remove",
        "--force",
        str(dst),
    )
    if dst.exists():
        await asyncio.to_thread(shutil.rmtree, dst, ignore_errors=True)
    await _run_git("-C", str(mirror), "worktree", "prune")


async def verify_worktree_revision(
    plan_id: uuid.UUID,
    expected_sha: str,
) -> Path:
    """Prove the plan-scoped worktree is still at its authorised commit."""

    if not _CANONICAL_COMMIT_RE.fullmatch(expected_sha):
        raise WorktreeRevisionUnavailable(
            "expected SHA must be a canonical full lowercase Git commit"
        )
    dst = worktree_path(plan_id)
    if not dst.exists():
        raise WorktreeRevisionUnavailable("Plan worktree is missing")
    resolved = await _resolved_head(dst)
    if resolved != expected_sha:
        raise WorktreeRevisionMismatch(
            "worktree HEAD does not match the Plan source revision"
        )
    return dst


async def create_worktree(
    plan_id: uuid.UUID,
    project_id: uuid.UUID,
    base_sha: str | None = None,
) -> Path:
    """Materialise an isolated working copy for ``plan_id``.

    Idempotent: a second call with the same ``plan_id`` returns the existing
    path only after proving its HEAD still equals ``base_sha``. Passing a
    revision turns off the legacy empty-directory fallback: an authorised
    Plan must never drift to mirror HEAD or a non-Git directory.
    """
    mirror = _mirror_path(project_id)
    dst = worktree_path(plan_id)
    if base_sha is not None and not _CANONICAL_COMMIT_RE.fullmatch(base_sha):
        raise WorktreeRevisionUnavailable(
            "base_sha must be a canonical full lowercase Git commit"
        )
    if dst.exists():
        if base_sha is not None:
            try:
                await verify_worktree_revision(plan_id, base_sha)
            except WorktreeRevisionMismatch as exc:
                raise WorktreeRevisionMismatch(
                    "existing worktree HEAD does not match the Plan source revision"
                ) from exc
        return dst
    dst.parent.mkdir(parents=True, exist_ok=True)

    if await _is_git_repo(mirror):
        target = base_sha or "HEAD"
        if base_sha is not None:
            rc, stdout, _ = await _run_git(
                "-C",
                str(mirror),
                "rev-parse",
                "--verify",
                f"{base_sha}^{{commit}}",
            )
            resolved_target = stdout.decode("ascii", errors="ignore").strip()
            if rc != 0 or resolved_target != base_sha:
                raise WorktreeRevisionUnavailable(
                    "published source commit is unavailable in the project mirror"
                )
        rc, _, stderr = await _run_git(
            "-C", str(mirror), "worktree", "add", "--detach", str(dst), target
        )
        if rc != 0:
            raise RuntimeError(
                f"git worktree add failed (rc={rc}): {stderr.decode(errors='replace')}"
            )
        if base_sha is not None:
            try:
                await verify_worktree_revision(plan_id, base_sha)
            except WorktreeRevisionError:
                await _discard_failed_worktree(mirror, dst)
                raise WorktreeRevisionMismatch(
                    "created worktree HEAD does not match the Plan source revision"
                ) from None
        return dst

    if base_sha is not None:
        raise WorktreeRevisionUnavailable(
            "project mirror is unavailable; cannot verify the published source commit"
        )
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


_CANONICAL_DIFF_ARGS = (
    "--no-color",
    "--binary",
    "--full-index",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--diff-algorithm=myers",
    "--unified=3",
    "--inter-hunk-context=0",
)


def _git_failure(operation: str, stderr: bytes) -> WorktreeDiffError:
    detail = stderr.decode("utf-8", errors="replace").strip()[:400]
    suffix = f": {detail}" if detail else ""
    return WorktreeDiffError(f"{operation} failed{suffix}")


def _decode_canonical_diff(stdout: bytes) -> str:
    try:
        return stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        # DiffSubmission.diff is persisted as database text. Replacing invalid
        # bytes would let two distinct commit payloads collapse to the same
        # reviewed string, so unsupported output must fail closed.
        raise WorktreeDiffError(
            "canonical Git diff is not valid UTF-8"
        ) from exc


async def canonical_staged_diff(
    worktree: Path,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Return the canonical patch represented by the selected Git index."""

    rc, stdout, stderr = await _run_git(
        "diff",
        "--cached",
        *_CANONICAL_DIFF_ARGS,
        "HEAD",
        "--",
        cwd=worktree,
        env=env,
    )
    if rc != 0:
        raise _git_failure("git diff --cached", stderr)
    return _decode_canonical_diff(stdout)


async def canonical_committed_diff(
    worktree: Path,
    *,
    commit: str = "HEAD",
) -> str:
    """Return the canonical patch stored by one non-root commit."""

    rc, stdout, stderr = await _run_git(
        "diff",
        *_CANONICAL_DIFF_ARGS,
        f"{commit}^",
        commit,
        "--",
        cwd=worktree,
    )
    if rc != 0:
        raise _git_failure("git diff of committed payload", stderr)
    return _decode_canonical_diff(stdout)


async def canonical_worktree_diff(worktree: Path) -> str:
    """Snapshot everything ``git add -A`` would commit without touching index.

    A plain ``git diff`` omits untracked and staged-only changes. A private
    index seeded from ``HEAD`` and populated with ``git add -A`` gives Gate B
    the exact tracked, untracked, deleted, mode, and binary payload that the MR
    path will stage later, while preserving the Plan worktree's real index.
    """

    descriptor, raw_path = tempfile.mkstemp(prefix="mnemos-diff-index-")
    os.close(descriptor)
    index_path = Path(raw_path)
    # Git expects either a valid index or a path that does not yet exist.
    index_path.unlink()
    lock_path = Path(f"{index_path}.lock")
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(index_path)
    try:
        rc, _, stderr = await _run_git("read-tree", "HEAD", cwd=worktree, env=env)
        if rc != 0:
            raise _git_failure("git read-tree HEAD", stderr)
        rc, _, stderr = await _run_git("add", "-A", "--", cwd=worktree, env=env)
        if rc != 0:
            raise _git_failure("git add -A", stderr)
        return await canonical_staged_diff(worktree, env=env)
    finally:
        index_path.unlink(missing_ok=True)
        lock_path.unlink(missing_ok=True)


async def compute_diff(plan_id: uuid.UUID) -> str:
    dst = worktree_path(plan_id)
    if not dst.exists():
        return ""
    # A fallback (no-mirror) worktree is just an empty directory and is not a
    # Git repo. Preserve the local-development empty-diff contract.
    if not await _is_git_repo(dst):
        return ""
    return await canonical_worktree_diff(dst)


def resolve_in_worktree(plan_id: uuid.UUID, relative: str) -> Path:
    root = worktree_path(plan_id).resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
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
    "canonical_committed_diff",
    "canonical_staged_diff",
    "canonical_worktree_diff",
    "resolve_in_worktree",
    "verify_worktree_revision",
    "analyzer_mount_args",
    "WorktreeRevisionError",
    "WorktreeRevisionMismatch",
    "WorktreeRevisionUnavailable",
    "WorktreeDiffError",
]
