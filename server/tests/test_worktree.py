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
    (src / "delete-me.txt").write_text("remove me\n")
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


def test_resolve_in_worktree_accepts_platform_native_child(patched_roots):
    pid = uuid.uuid4()
    expected = (wt.worktree_path(pid) / "src" / "main.py").resolve()

    assert wt.resolve_in_worktree(pid, "src/main.py") == expected


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
async def test_revision_bound_worktree_refuses_missing_mirror(patched_roots):
    """A source-authorised Plan must never fall back to an empty directory."""

    with pytest.raises(wt.WorktreeRevisionUnavailable, match="mirror"):
        await wt.create_worktree(
            uuid.uuid4(),
            uuid.uuid4(),
            base_sha="a" * 40,
        )


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_git(), reason="git CLI not available")
async def test_revision_bound_worktree_verifies_new_and_existing_head(
    patched_roots,
    tmp_path,
):
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    src = tmp_path / "revision-src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(src), "config", "user.name", "t"], check=True)
    subprocess.run(
        ["git", "-C", str(src), "config", "commit.gpgsign", "false"],
        check=True,
    )
    tracked = src / "tracked.txt"
    tracked.write_text("one\n")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "one"], check=True)
    first_sha = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    tracked.write_text("two\n")
    subprocess.run(["git", "-C", str(src), "commit", "-qam", "two"], check=True)
    second_sha = subprocess.check_output(
        ["git", "-C", str(src), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    mirror = wt._REPO_ROOT / str(project_id)
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(mirror)],
        check=True,
    )

    dst = await wt.create_worktree(plan_id, project_id, base_sha=first_sha)
    resolved = subprocess.check_output(
        ["git", "-C", str(dst), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    assert resolved == first_sha
    assert (dst / "tracked.txt").read_text() == "one\n"

    # Idempotency is conditional on the existing worktree retaining the same
    # authorised commit; a different requested/current revision is rejected.
    assert await wt.create_worktree(
        plan_id,
        project_id,
        base_sha=first_sha,
    ) == dst
    with pytest.raises(wt.WorktreeRevisionMismatch, match="existing worktree"):
        await wt.create_worktree(
            plan_id,
            project_id,
            base_sha=second_sha,
        )

    await wt.destroy_worktree(plan_id, project_id)


@pytest.mark.asyncio
async def test_compute_diff_empty_on_clean_tree(patched_roots):
    """A worktree with no edits returns an empty diff."""
    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    await wt.create_worktree(plan_id, project_id)
    assert (await wt.compute_diff(plan_id)) == ""


@pytest.mark.asyncio
@pytest.mark.skipif(not _has_git(), reason="git CLI not available")
async def test_compute_diff_captures_exact_git_add_all_payload(
    patched_roots,
    tmp_path,
):
    """Gate B sees staged, unstaged, untracked, deleted, and binary content."""

    project_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    mirror = _init_mirror(tmp_path, project_id)
    base_sha = subprocess.check_output(
        ["git", "-C", str(mirror), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    dst = await wt.create_worktree(plan_id, project_id, base_sha=base_sha)

    readme = dst / "README.md"
    readme.write_text("staged intermediate\n")
    subprocess.run(["git", "-C", str(dst), "add", "README.md"], check=True)
    readme.write_text("final worktree content\n")
    (dst / "new-file.txt").write_text("untracked payload\n")
    (dst / "new-binary.bin").write_bytes(b"\x00\x01\x02binary\xff")
    (dst / "delete-me.txt").unlink()

    real_index_before = subprocess.check_output(
        ["git", "-C", str(dst), "diff", "--cached", "--no-color"],
    )
    diff = await wt.compute_diff(plan_id)
    real_index_after = subprocess.check_output(
        ["git", "-C", str(dst), "diff", "--cached", "--no-color"],
    )

    assert real_index_after == real_index_before
    assert "final worktree content" in diff
    assert "staged intermediate" not in diff
    assert "new file mode" in diff
    assert "new-file.txt" in diff
    assert "untracked payload" in diff
    assert "new-binary.bin" in diff
    assert "GIT binary patch" in diff
    assert "deleted file mode" in diff
    assert "delete-me.txt" in diff

    subprocess.run(["git", "-C", str(dst), "add", "-A"], check=True)
    assert await wt.canonical_staged_diff(dst) == diff
    subprocess.run(
        [
            "git",
            "-C",
            str(dst),
            "-c",
            "user.email=mnemos@local",
            "-c",
            "user.name=Mnemos",
            "commit",
            "-q",
            "-m",
            "candidate",
        ],
        check=True,
    )
    assert await wt.canonical_committed_diff(dst) == diff
