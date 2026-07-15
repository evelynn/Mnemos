from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid

import pytest

from app.orchestrator import source_checkout as source_checkout_mod
from app.orchestrator.source_checkout import (
    PreparedSource,
    SourceCheckoutError,
    prepare_source,
)


@pytest.mark.asyncio
async def test_manual_source_must_be_an_existing_directory(tmp_path):
    project_id = uuid.uuid4()
    project_roots = json.dumps({str(project_id): "."})
    prepared = await prepare_source(
        str(tmp_path),
        project_id=project_id,
        run_id=uuid.uuid4(),
        git_sha="HEAD",
        allowed_root=tmp_path,
        project_roots_json=project_roots,
    )
    assert prepared.path == tmp_path.resolve()
    await prepared.cleanup()

    with pytest.raises(SourceCheckoutError, match="source_path_not_found"):
        await prepare_source(
            str(tmp_path / "missing"),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=tmp_path,
            project_roots_json=project_roots,
        )


@pytest.mark.asyncio
async def test_manual_source_respects_configured_allowed_root(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    project_id = uuid.uuid4()
    with pytest.raises(SourceCheckoutError, match="source_path_outside_project_root"):
        await prepare_source(
            str(outside),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=allowed,
            project_roots_json=json.dumps({str(project_id): "."}),
        )


@pytest.mark.asyncio
async def test_empty_source_never_falls_back_to_worker_cwd(tmp_path):
    with pytest.raises(SourceCheckoutError, match="webhook_source_mirror_not_configured"):
        await prepare_source(
            "",
            project_id=uuid.uuid4(),
            run_id=uuid.uuid4(),
            git_sha="a" * 40,
            mirror_root="",
        )


@pytest.mark.asyncio
async def test_exact_revision_worktree_is_created_and_removed(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    project_id = uuid.uuid4()
    repository = tmp_path / "mirrors" / str(project_id)
    repository.mkdir(parents=True)
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        [git, "config", "user.email", "mnemos-test@example.invalid"],
        cwd=repository,
        check=True,
    )
    subprocess.run(
        [git, "config", "user.name", "Mnemos Test"], cwd=repository, check=True
    )
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "service.py"], cwd=repository, check=True)
    subprocess.run([git, "commit", "-m", "fixture"], cwd=repository, check=True, capture_output=True)
    sha = subprocess.check_output([git, "rev-parse", "HEAD"], cwd=repository, text=True).strip()

    prepared = await prepare_source(
        "",
        project_id=project_id,
        run_id=uuid.uuid4(),
        git_sha=sha,
        mirror_root=repository.parent,
        worktree_root=tmp_path / "worktrees",
    )
    assert (prepared.path / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    target = prepared.path
    await prepared.cleanup()
    assert not target.exists()


@pytest.mark.asyncio
async def test_webhook_short_sha_is_persisted_as_full_commit_id(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    project_id = uuid.uuid4()
    repository = tmp_path / "mirrors" / str(project_id)
    repository.mkdir(parents=True)
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "service.py"], cwd=repository, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Mnemos Test",
            "-c",
            "user.email=mnemos@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    full_sha = subprocess.check_output(
        [git, "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    prepared = await prepare_source(
        "",
        project_id=project_id,
        run_id=uuid.uuid4(),
        git_sha=full_sha[:8],
        mirror_root=repository.parent,
        worktree_root=tmp_path / "worktrees",
    )
    try:
        assert prepared.revision == full_sha
        assert len(prepared.revision) >= 40
    finally:
        await prepared.cleanup()


@pytest.mark.asyncio
async def test_webhook_git_failures_do_not_expose_raw_details(tmp_path, monkeypatch):
    project_id = uuid.uuid4()
    mirror_root = tmp_path / "mirrors"
    mirror = mirror_root / str(project_id)
    mirror.mkdir(parents=True)
    secret = "https://operator:super-secret@example.invalid/private-host-path"

    async def _missing_revision(*_args, **_kwargs):
        return 1, secret

    monkeypatch.setattr(source_checkout_mod, "_git", _missing_revision)
    with pytest.raises(SourceCheckoutError) as unavailable:
        await prepare_source(
            "",
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="a" * 40,
            mirror_root=mirror_root,
            worktree_root=tmp_path / "worktrees",
        )
    assert str(unavailable.value) == "webhook_source_revision_unavailable"
    assert "super-secret" not in str(unavailable.value)
    assert "private-host-path" not in str(unavailable.value)

    async def _worktree_failure(*args, **_kwargs):
        if "cat-file" in args:
            return 0, ""
        if "rev-parse" in args:
            return 0, "a" * 40
        return 1, secret

    monkeypatch.setattr(source_checkout_mod, "_git", _worktree_failure)
    with pytest.raises(SourceCheckoutError) as create_failed:
        await prepare_source(
            "",
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="a" * 40,
            mirror_root=mirror_root,
            worktree_root=tmp_path / "worktrees",
        )
    assert str(create_failed.value) == "source_worktree_create_failed"
    assert "super-secret" not in str(create_failed.value)
    assert "private-host-path" not in str(create_failed.value)


@pytest.mark.asyncio
async def test_cleanup_failure_log_is_structured_and_secret_safe(
    tmp_path, monkeypatch, caplog
):
    secret = "https://operator:super-secret@example.invalid/private-host-path"

    async def _cleanup_failure(*_args, **_kwargs):
        return 1, secret

    monkeypatch.setattr(source_checkout_mod, "_git", _cleanup_failure)
    prepared = PreparedSource(
        path=tmp_path / "private-host-path",
        mirror=tmp_path / "mirror",
        cleanup_path=tmp_path / "private-host-path",
    )
    caplog.set_level(logging.ERROR, logger=source_checkout_mod.__name__)

    await prepared.cleanup()

    assert "source_worktree_cleanup_failed" in caplog.text
    assert "super-secret" not in caplog.text
    assert "private-host-path" not in caplog.text


@pytest.mark.asyncio
async def test_manual_detached_commit_requires_explicit_ref(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Mnemos Test",
            "-c",
            "user.email=mnemos@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        [git, "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    project_id = uuid.uuid4()

    with pytest.raises(
        SourceCheckoutError, match="manual_git_ref_required_for_detached_revision"
    ):
        await prepare_source(
            str(repository),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha=sha,
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "repo"}),
            worktree_root=tmp_path / "worktrees",
        )


@pytest.mark.asyncio
async def test_manual_revision_must_belong_to_declared_ref(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    repository = tmp_path / "repo"
    repository.mkdir()
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=repository, check=True)
    identity = [
        "-c",
        "user.name=Mnemos Test",
        "-c",
        "user.email=mnemos@example.invalid",
    ]
    subprocess.run(
        [git, *identity, "commit", "-m", "base"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [git, "branch", "stale-ref"], cwd=repository, check=True, capture_output=True
    )
    (repository / "service.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=repository, check=True)
    subprocess.run(
        [git, *identity, "commit", "-m", "new head"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    sha = subprocess.check_output(
        [git, "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    project_id = uuid.uuid4()

    with pytest.raises(
        SourceCheckoutError, match="manual_git_revision_not_on_ref"
    ):
        await prepare_source(
            str(repository),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha=sha,
            source_ref="refs/heads/stale-ref",
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "repo"}),
            worktree_root=tmp_path / "worktrees",
        )


@pytest.mark.asyncio
async def test_git_subtree_is_not_authoritative_for_deletion_sweep(tmp_path):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    repository = tmp_path / "repo"
    subtree = repository / "service"
    subtree.mkdir(parents=True)
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    (subtree / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Mnemos Test",
            "-c",
            "user.email=mnemos@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )

    project_id = uuid.uuid4()
    prepared = await prepare_source(
        str(subtree),
        project_id=project_id,
        run_id=uuid.uuid4(),
        git_sha="HEAD",
        allowed_root=tmp_path,
        project_roots_json=json.dumps({str(project_id): "repo"}),
    )
    assert prepared.authoritative_root is False
    assert prepared.path.name == "service"
    assert prepared.repository_locator == {
        "kind": "allowed_root_relative",
        "path": "repo",
    }
    await prepared.cleanup()


@pytest.mark.asyncio
async def test_manual_git_uses_private_mirror_without_source_metadata_writes(
    tmp_path, monkeypatch
):
    """A read-only source bind must not receive ``git worktree`` metadata."""

    git = shutil.which("git")
    if git is None:
        pytest.skip("git unavailable")
    repository = tmp_path / "source" / "repo"
    repository.mkdir(parents=True)
    subprocess.run([git, "init"], cwd=repository, check=True, capture_output=True)
    (repository / "service.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run([git, "add", "."], cwd=repository, check=True)
    subprocess.run(
        [
            git,
            "-c",
            "user.name=Mnemos Test",
            "-c",
            "user.email=mnemos@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    revision = subprocess.check_output(
        [git, "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()

    original_git = source_checkout_mod._git

    async def _reject_source_worktree_metadata(*args, **kwargs):  # noqa: ANN002, ANN003
        if (
            len(args) >= 4
            and args[0:2] == ("-C", str(repository.resolve()))
            and args[2:4] == ("worktree", "add")
        ):
            raise AssertionError("manual source repository received worktree metadata")
        return await original_git(*args, **kwargs)

    monkeypatch.setattr(source_checkout_mod, "_git", _reject_source_worktree_metadata)
    mirror_root = tmp_path / "private-mirrors"
    worktree_root = tmp_path / "private-worktrees"
    project_id = uuid.uuid4()
    prepared = await prepare_source(
        str(repository),
        project_id=project_id,
        run_id=uuid.uuid4(),
        git_sha="HEAD",
        allowed_root=tmp_path / "source",
        project_roots_json=json.dumps({str(project_id): "repo"}),
        mirror_root=mirror_root,
        worktree_root=worktree_root,
    )
    try:
        assert prepared.mirror is not None
        assert prepared.mirror != repository.resolve()
        assert prepared.mirror.is_relative_to(mirror_root.resolve())
        assert prepared.path.is_relative_to(worktree_root.resolve())
        assert prepared.revision == revision
        assert (prepared.path / "service.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert not (repository / ".git" / "worktrees").exists()
    finally:
        await prepared.cleanup()

    private_mirror = prepared.mirror
    assert private_mirror is not None

    async def _locked_fetch(*args, **kwargs):  # noqa: ANN002, ANN003
        if args[0:3] == ("-C", str(private_mirror), "fetch"):
            return 1, "https://operator:super-secret@example.invalid index.lock"
        return await _reject_source_worktree_metadata(*args, **kwargs)

    monkeypatch.setattr(source_checkout_mod, "_git", _locked_fetch)
    with pytest.raises(SourceCheckoutError) as locked:
        await prepare_source(
            str(repository),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=tmp_path / "source",
            project_roots_json=json.dumps({str(project_id): "repo"}),
            mirror_root=mirror_root,
            worktree_root=worktree_root,
        )
    assert str(locked.value) == "source_manual_mirror_fetch_failed"
    assert "super-secret" not in str(locked.value)

    monkeypatch.setattr(source_checkout_mod, "_git", _reject_source_worktree_metadata)
    real_link_check = source_checkout_mod._is_link_or_junction
    escaped_mirror_root = tmp_path / "escaped-mirrors"
    expected_manual = escaped_mirror_root.resolve() / ".manual"

    def _manual_directory_is_link(path):  # noqa: ANN001
        return path == expected_manual or real_link_check(path)

    monkeypatch.setattr(
        source_checkout_mod, "_is_link_or_junction", _manual_directory_is_link
    )
    with pytest.raises(SourceCheckoutError, match="source_manual_mirror_root_invalid"):
        await prepare_source(
            str(repository),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=tmp_path / "source",
            project_roots_json=json.dumps({str(project_id): "repo"}),
            mirror_root=escaped_mirror_root,
            worktree_root=worktree_root,
        )
    monkeypatch.setattr(source_checkout_mod, "_is_link_or_junction", real_link_check)

    (repository / "service.py").write_text("DIRTY = True\n", encoding="utf-8")
    with pytest.raises(SourceCheckoutError, match="manual_git_worktree_dirty"):
        await prepare_source(
            str(repository),
            project_id=project_id,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=tmp_path / "source",
            project_roots_json=json.dumps({str(project_id): "repo"}),
            mirror_root=mirror_root,
            worktree_root=worktree_root,
        )
