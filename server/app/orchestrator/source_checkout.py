"""Safe source-path resolution for manual and webhook analysis runs."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.config import get_settings
from app.orchestrator.source_binding import (
    ProjectSourceBindingError,
    resolve_project_source_path,
)

log = logging.getLogger(__name__)

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class SourceCheckoutError(RuntimeError):
    """The requested source snapshot cannot be proven or materialised."""


def _is_link_or_junction(path: Path) -> bool:
    try:
        is_junction = getattr(path, "is_junction", None)
        return path.is_symlink() or bool(is_junction and is_junction())
    except OSError:
        return True


async def _git(*args: str, timeout_s: float = 60.0) -> tuple[int, str]:
    executable = shutil.which("git")
    if executable is None:
        raise SourceCheckoutError("source_checkout_git_unavailable")
    proc = await asyncio.create_subprocess_exec(
        executable,
        "--no-optional-locks",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise SourceCheckoutError("source_checkout_git_timeout") from exc
    detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
    return int(proc.returncode or 0), detail[-1000:]


@dataclass
class PreparedSource:
    path: Path
    mirror: Path | None = None
    authoritative_root: bool = True
    cleanup_path: Path | None = None
    revision: str | None = None
    source_ref: str | None = None
    root_id: str | None = None
    source_kind: str = "manual_directory"
    mutable: bool = False
    tree_prefix: str = ""
    repository_locator: dict[str, str] | None = None

    async def cleanup(self) -> None:
        if self.mirror is None:
            return
        target = self.cleanup_path or self.path
        code, _detail = await _git(
            "-C", str(self.mirror), "worktree", "remove", "--force", str(target)
        )
        if code != 0:
            # Never recursively delete a path after a failed Git operation.
            # Leaving a visible stale worktree is safer than guessing that the
            # computed directory is disposable.
            log.error("source_worktree_cleanup_failed git_exit_code=%s", code)


async def prepare_source(
    source_path: str,
    *,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    git_sha: str,
    source_ref: str | None = None,
    mirror_root: str | Path | None = None,
    worktree_root: str | Path | None = None,
    allowed_root: str | Path | None = None,
    project_roots_json: str | None = None,
) -> PreparedSource:
    """Resolve a manual directory or create an exact-SHA detached worktree.

    Empty ``source_path`` is reserved for webhooks.  It is accepted only when
    an operator has configured and populated a mirror.  This prevents the old
    failure mode where ``Path("")`` became ``.`` and the worker analyzed its
    own application directory while reporting the target project as fresh.
    """

    if source_path.strip():
        settings = get_settings()
        try:
            binding = resolve_project_source_path(
                project_id,
                source_path,
                allowed_root=allowed_root,
                project_roots_json=project_roots_json,
            )
        except ProjectSourceBindingError as exc:
            raise SourceCheckoutError(str(exc)) from exc
        resolved_allowed_root = binding.allowed_root
        project_root = binding.project_root
        path = binding.source_path
        authoritative_root = True
        try:
            code, git_root = await _git(
                "-C", str(path), "rev-parse", "--show-toplevel", timeout_s=15.0
            )
            if code == 0 and git_root:
                repo_root = Path(git_root).resolve(strict=True)
                if (
                    repo_root != project_root
                    and project_root not in repo_root.parents
                ):
                    # A linked Git worktree can live under /work while its
                    # repository metadata/root points elsewhere.  Do not let
                    # that bypass the source-root boundary.
                    raise SourceCheckoutError("source_repository_outside_project_root")
                authoritative_root = repo_root == path
                dirty_code, dirty = await _git(
                    "-C", str(repo_root), "status", "--porcelain", timeout_s=30.0
                )
                if dirty_code != 0:
                    raise SourceCheckoutError("manual_git_status_failed")
                if dirty:
                    raise SourceCheckoutError("manual_git_worktree_dirty")
                revision_expr = (git_sha or "HEAD").strip()
                if revision_expr.startswith("-") or len(revision_expr) > 200:
                    raise SourceCheckoutError("manual_git_revision_invalid")
                rev_code, revision = await _git(
                    "-C",
                    str(repo_root),
                    "rev-parse",
                    "--verify",
                    f"{revision_expr}^{{commit}}",
                    timeout_s=30.0,
                )
                if rev_code != 0 or not _COMMIT_RE.fullmatch(revision):
                    raise SourceCheckoutError("manual_git_revision_unavailable")

                canonical_source_ref = (source_ref or "").strip()
                if not canonical_source_ref:
                    if revision_expr == "HEAD":
                        ref_code, discovered_ref = await _git(
                            "-C", str(repo_root), "symbolic-ref", "-q", "HEAD"
                        )
                    else:
                        ref_code, discovered_ref = await _git(
                            "-C",
                            str(repo_root),
                            "rev-parse",
                            "--symbolic-full-name",
                            revision_expr,
                        )
                    if (
                        ref_code != 0
                        or not discovered_ref
                        or not discovered_ref.startswith("refs/")
                    ):
                        raise SourceCheckoutError(
                            "manual_git_ref_required_for_detached_revision"
                        )
                    canonical_source_ref = discovered_ref
                elif not canonical_source_ref.startswith("refs/"):
                    canonical_source_ref = f"refs/heads/{canonical_source_ref}"
                ref_code, _ = await _git(
                    "-C",
                    str(repo_root),
                    "merge-base",
                    "--is-ancestor",
                    revision,
                    canonical_source_ref,
                )
                if ref_code != 0:
                    raise SourceCheckoutError("manual_git_revision_not_on_ref")

                configured = str(
                    worktree_root
                    if worktree_root is not None
                    else settings.source_worktree_root
                ).strip()
                worktrees = (
                    Path(configured).expanduser().resolve()
                    if configured
                    else repo_root.parent / ".mnemos-worktrees"
                )
                worktrees.mkdir(parents=True, exist_ok=True)
                checkout_root = (worktrees / str(run_id)).resolve()
                if checkout_root.parent != worktrees.resolve():
                    raise SourceCheckoutError("source_worktree_path_escape")
                if checkout_root.exists():
                    raise SourceCheckoutError("source_worktree_already_exists")

                # Never register a worktree in the operator's source repo.
                # Compose mounts /work read-only, and even on a writable host a
                # source-analysis job must not create .git/worktrees metadata.
                # Clone/fetch into a project-private bare mirror first; all
                # required Git writes and the detached worktree registration
                # then stay beneath Mnemos-controlled storage.
                origin_code, origin = await _git(
                    "-C", str(repo_root), "config", "--get", "remote.origin.url"
                )
                identity = origin if origin_code == 0 and origin else str(repo_root)
                identity_digest = hashlib.sha256(identity.encode()).hexdigest()
                configured_mirrors = str(
                    mirror_root
                    if mirror_root is not None
                    else settings.source_mirror_root
                ).strip()
                mirror_base_lexical = (
                    Path(configured_mirrors).expanduser()
                    if configured_mirrors
                    else worktrees / ".mirrors"
                )
                if _is_link_or_junction(mirror_base_lexical):
                    raise SourceCheckoutError("source_manual_mirror_root_invalid")
                try:
                    mirror_base_lexical.mkdir(parents=True, exist_ok=True)
                    if _is_link_or_junction(mirror_base_lexical):
                        raise SourceCheckoutError(
                            "source_manual_mirror_root_invalid"
                        )
                    mirror_base = mirror_base_lexical.resolve(strict=True)
                except SourceCheckoutError:
                    raise
                except OSError as exc:
                    raise SourceCheckoutError(
                        "source_manual_mirror_root_unavailable"
                    ) from exc
                manual_lexical = mirror_base / ".manual"
                if _is_link_or_junction(manual_lexical):
                    raise SourceCheckoutError("source_manual_mirror_root_invalid")
                try:
                    manual_lexical.mkdir(parents=False, exist_ok=True)
                    if _is_link_or_junction(manual_lexical):
                        raise SourceCheckoutError(
                            "source_manual_mirror_root_invalid"
                        )
                    private_parent = manual_lexical.resolve(strict=True)
                except SourceCheckoutError:
                    raise
                except OSError as exc:
                    raise SourceCheckoutError(
                        "source_manual_mirror_root_unavailable"
                    ) from exc
                if private_parent.parent != mirror_base:
                    raise SourceCheckoutError("source_manual_mirror_path_escape")
                mirror_key = hashlib.sha256(
                    f"{project_id}\0{identity}".encode()
                ).hexdigest()[:24]
                private_mirror = private_parent / f"{mirror_key}.git"
                if _is_link_or_junction(private_mirror):
                    raise SourceCheckoutError("source_manual_mirror_invalid")
                private_mirror = private_mirror.resolve()
                if private_mirror.parent != private_parent:
                    raise SourceCheckoutError("source_manual_mirror_path_escape")
                if private_mirror.exists():
                    if not private_mirror.is_dir():
                        raise SourceCheckoutError("source_manual_mirror_invalid")
                    bare_code, bare = await _git(
                        "-C", str(private_mirror), "rev-parse", "--is-bare-repository"
                    )
                    if bare_code != 0 or bare != "true":
                        raise SourceCheckoutError("source_manual_mirror_invalid")
                    fetch_code, _detail = await _git(
                        "-C",
                        str(private_mirror),
                        "fetch",
                        "--force",
                        "--prune",
                        "--no-tags",
                        str(repo_root),
                        "+refs/*:refs/*",
                        timeout_s=120.0,
                    )
                    if fetch_code != 0:
                        # Git's lock/ref failure is fail-closed. Do not surface
                        # raw stderr: a configured remote can contain credentials.
                        raise SourceCheckoutError("source_manual_mirror_fetch_failed")
                else:
                    clone_code, _detail = await _git(
                        "clone",
                        "--mirror",
                        "--no-local",
                        str(repo_root),
                        str(private_mirror),
                        timeout_s=120.0,
                    )
                    if clone_code != 0:
                        raise SourceCheckoutError("source_manual_mirror_create_failed")
                mirrored_code, _ = await _git(
                    "-C", str(private_mirror), "cat-file", "-e", f"{revision}^{{commit}}"
                )
                if mirrored_code != 0:
                    raise SourceCheckoutError("source_manual_mirror_revision_unavailable")
                add_code, _detail = await _git(
                    "-C",
                    str(private_mirror),
                    "worktree",
                    "add",
                    "--detach",
                    str(checkout_root),
                    revision,
                    timeout_s=120.0,
                )
                if add_code != 0:
                    raise SourceCheckoutError("source_worktree_create_failed")
                relative = path.relative_to(repo_root)
                root_id = "git:" + identity_digest
                relative_repository = repo_root.relative_to(resolved_allowed_root)
                repository_locator = {
                    "kind": "allowed_root_relative",
                    "path": relative_repository.as_posix(),
                }
                return PreparedSource(
                    path=checkout_root / relative,
                    mirror=private_mirror,
                    authoritative_root=authoritative_root,
                    cleanup_path=checkout_root,
                    revision=revision,
                    source_ref=canonical_source_ref,
                    root_id=root_id,
                    source_kind="manual_git",
                    mutable=False,
                    tree_prefix=relative.as_posix() if relative != Path(".") else "",
                    repository_locator=repository_locator,
                )
        except SourceCheckoutError as exc:
            if str(exc) != "source_checkout_git_unavailable":
                raise
        if git_sha.strip() not in {"", "HEAD"}:
            raise SourceCheckoutError("manual_non_git_revision_unsupported")
        root_id = "directory:" + hashlib.sha256(str(path).encode()).hexdigest()
        return PreparedSource(
            path=path,
            authoritative_root=authoritative_root,
            revision=None,
            source_ref=source_ref,
            root_id=root_id,
            source_kind="manual_directory",
            mutable=True,
        )

    settings = get_settings()
    configured_mirror_root = str(
        mirror_root if mirror_root is not None else settings.source_mirror_root
    ).strip()
    if not configured_mirror_root:
        raise SourceCheckoutError("webhook_source_mirror_not_configured")
    if not _COMMIT_RE.fullmatch(git_sha or ""):
        raise SourceCheckoutError("webhook_source_revision_invalid")

    root = Path(configured_mirror_root).expanduser().resolve(strict=True)
    candidates = (root / str(project_id), root / f"{project_id}.git")
    mirror = next((candidate for candidate in candidates if candidate.exists()), None)
    if mirror is None:
        raise SourceCheckoutError("webhook_source_mirror_missing")

    code, _detail = await _git(
        "-C", str(mirror), "cat-file", "-e", f"{git_sha}^{{commit}}"
    )
    if code != 0:
        raise SourceCheckoutError("webhook_source_revision_unavailable")
    code, canonical_revision = await _git(
        "-C", str(mirror), "rev-parse", "--verify", f"{git_sha}^{{commit}}"
    )
    if code != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", canonical_revision):
        raise SourceCheckoutError("webhook_source_revision_unavailable")

    configured_worktree_root = str(
        worktree_root if worktree_root is not None else settings.source_worktree_root
    ).strip()
    worktrees = (
        Path(configured_worktree_root).expanduser().resolve()
        if configured_worktree_root
        else root / ".mnemos-worktrees"
    )
    worktrees.mkdir(parents=True, exist_ok=True)
    target = (worktrees / str(run_id)).resolve()
    if target.parent != worktrees.resolve():
        raise SourceCheckoutError("source_worktree_path_escape")
    if target.exists():
        raise SourceCheckoutError("source_worktree_already_exists")

    code, _detail = await _git(
        "-C", str(mirror), "worktree", "add", "--detach", str(target),
        canonical_revision,
        timeout_s=120.0,
    )
    if code != 0:
        raise SourceCheckoutError("source_worktree_create_failed")
    return PreparedSource(
        path=target,
        mirror=mirror,
        authoritative_root=True,
        cleanup_path=target,
        revision=canonical_revision,
        source_ref=source_ref,
        root_id=f"git-mirror:{project_id}",
        source_kind="git_mirror",
        mutable=False,
        tree_prefix="",
    )
