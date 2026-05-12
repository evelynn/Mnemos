"""Worktree management — git-backed isolation (spec §2.5, §7.3)."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.sandbox import worktree as wt


def _has_git() -> bool:
    return shutil.which("git") is not None


def _init_mirror(tmp_path: Path, project_id: uuid.UUID) -> Path:
    """Build a tiny seed repo whose mirror lives where the platform looks."""
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    # Disable any inherited gpg-signing so the test does not depend on
    # the surrounding environment having keys configured.
    subprocess.run(["git", "-C", str(src), "config", "commit.gpgsign", "false"], check=True)
    (src / "README.md").write_text("hello\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)

    mirror = tmp_path / "repos" / str(project_id)
    mirror.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(mirror)], check=True
    )
    return mirror


@pytest.fixture
def patched_roots(tmp_path, monkeypatch):
    """Point _WORKTREE_ROOT / _REPO_ROOT at a temp tree."""
    wroot = tmp_path / "worktrees"
    rroot = tmp_path / "repos"
    wroot.mkdir()
    rroot.mkdir()
    monkeypatch.setattr(wt, "_WORKTREE_ROOT", wroot)
    monkeypatch.setattr(wt, "_REPO_ROOT", rroot)
    return tmp_path


def test_resolve_in_worktree_rejects_path_escape(patched_roots):
    pid = uuid.uuid4()
    with pytest.raises(ValueError):
        wt.resolve_in_worktree(pid, "../etc/passwd")


def test_analyzer_mount_args_marks_readonly(patched_roots):
    """The docker arg list must combine --read-only with a :ro bind."""
    pid = uuid.uuid4()
    args = wt.analyzer_mount_args(pid)
    assert "--read-only" in args
    flat = " ".join(args)
    assert "readonly" in flat
    assert "tmpfs" in flat


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_git(), reason="git CLI not available")
async def test_create_worktree_uses_real_git_when_mirror_exists(patched_roots, tmp_path):
    """A real bare mirror → ``git worktree add`` populates the dst path."""
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    # Recreate the mirror at the patched _REPO_ROOT location.
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    # Disable any inherited gpg-signing so the test does not depend on
    # the surrounding environment having keys configured.
    subprocess.run(["git", "-C", str(src), "config", "commit.gpgsign", "false"], check=True)
    (src / "README.md").write_text("hi\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(
        [
            "git",
            "clone",
            "--bare",
            "-q",
            str(src),
            str(wt._REPO_ROOT / str(project_id)),
        ],
        check=True,
    )

    dst = await wt.create_worktree(plan_id, project_id)
    assert dst.exists()
    assert (dst / "README.md").read_text() == "hi\n"

    # And a re-create returns the same dir without erroring.
    dst2 = await wt.create_worktree(plan_id, project_id)
    assert dst2 == dst

    # Destroy frees the lock entry.
    await wt.destroy_worktree(plan_id, project_id)
    assert not dst.exists()


@pytest.mark.asyncio
async def test_create_worktree_falls_back_when_no_mirror(patched_roots):
    """No mirror → create an empty directory (dev / test convenience)."""
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    dst = await wt.create_worktree(plan_id, project_id)
    assert dst.exists()
    assert list(dst.iterdir()) == []
    await wt.destroy_worktree(plan_id, project_id)
    assert not dst.exists()


@pytest.mark.asyncio
async def test_compute_diff_empty_on_clean_tree(patched_roots):
    """A worktree with no edits returns an empty diff."""
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    await wt.create_worktree(plan_id, project_id)
    assert (await wt.compute_diff(plan_id)) == ""
