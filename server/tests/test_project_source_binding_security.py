"""Tenant binding regressions for manual source analysis."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import analysis
from app.orchestrator import source_checkout
from app.orchestrator.source_binding import (
    ProjectSourceBindingError,
    resolve_project_source_path,
)
from app.orchestrator.source_checkout import SourceCheckoutError, prepare_source


def test_single_repository_dot_and_owned_subtree_resolve_canonically(tmp_path) -> None:
    project_id = uuid.uuid4()
    subtree = tmp_path / "src" / "service"
    subtree.mkdir(parents=True)
    binding = resolve_project_source_path(
        project_id,
        str(subtree),
        allowed_root=tmp_path,
        project_roots_json=json.dumps({str(project_id): "."}),
    )
    assert binding.project_root == tmp_path.resolve(strict=True)
    assert binding.source_path == subtree.resolve(strict=True)


def test_lexical_parent_alias_is_rejected_even_when_target_is_owned(tmp_path) -> None:
    project_id = uuid.uuid4()
    source = tmp_path / "repo" / "src"
    source.mkdir(parents=True)
    aliased = source / ".." / "src"
    with pytest.raises(ProjectSourceBindingError, match="source_path_parent_rejected"):
        resolve_project_source_path(
            project_id,
            str(aliased),
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "repo"}),
        )


def test_nul_source_path_fails_closed(tmp_path) -> None:
    project_id = uuid.uuid4()
    with pytest.raises(ProjectSourceBindingError, match="source_path_must_be_absolute"):
        resolve_project_source_path(
            project_id,
            f"{tmp_path}\x00suffix",
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "."}),
        )


@pytest.mark.parametrize(
    ("raw_mapping", "error"),
    [
        ("", "source_project_roots_not_configured"),
        ("not-json", "source_project_roots_invalid_json"),
        ("{}", "source_project_roots_invalid_json"),
        ('{"not-a-uuid":"repo"}', "source_project_roots_invalid_project_id"),
    ],
)
def test_missing_and_malformed_mappings_fail_closed(
    tmp_path, raw_mapping, error
) -> None:
    project_id = uuid.uuid4()
    with pytest.raises(ProjectSourceBindingError, match=error):
        resolve_project_source_path(
            project_id,
            str(tmp_path),
            allowed_root=tmp_path,
            project_roots_json=raw_mapping,
        )


@pytest.mark.parametrize(
    "mapped",
    ["", "../tenant-b", "/etc", "C:\\secret", "\\root-relative"],
)
def test_empty_parent_and_absolute_mapping_values_are_rejected(tmp_path, mapped) -> None:
    project_id = uuid.uuid4()
    with pytest.raises(ProjectSourceBindingError):
        resolve_project_source_path(
            project_id,
            str(tmp_path),
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): mapped}),
        )


def test_duplicate_semantic_project_uuid_is_rejected(tmp_path) -> None:
    project_id = uuid.uuid4()
    raw = json.dumps(
        {
            str(project_id): ".",
            str(project_id).upper(): ".",
        }
    )
    with pytest.raises(
        ProjectSourceBindingError,
        match="source_project_roots_duplicate_project_id",
    ):
        resolve_project_source_path(
            project_id,
            str(tmp_path),
            allowed_root=tmp_path,
            project_roots_json=raw,
        )


def test_duplicate_raw_project_key_is_rejected(tmp_path) -> None:
    project_id = uuid.uuid4()
    raw = f'{{"{project_id}":".","{project_id}":"."}}'
    with pytest.raises(
        ProjectSourceBindingError,
        match="source_project_roots_duplicate_key",
    ):
        resolve_project_source_path(
            project_id,
            str(tmp_path),
            allowed_root=tmp_path,
            project_roots_json=raw,
        )


def test_symlink_or_junction_mapping_is_rejected(tmp_path) -> None:
    project_id = uuid.uuid4()
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(ProjectSourceBindingError, match="source_path_link_rejected"):
        resolve_project_source_path(
            project_id,
            str(real),
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "alias"}),
        )


def test_symlink_or_junction_in_requested_source_is_rejected(tmp_path) -> None:
    project_id = uuid.uuid4()
    project_root = tmp_path / "repo"
    real = project_root / "real"
    real.mkdir(parents=True)
    alias = project_root / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")
    with pytest.raises(ProjectSourceBindingError, match="source_path_link_rejected"):
        resolve_project_source_path(
            project_id,
            str(alias),
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_id): "repo"}),
        )


def test_windows_case_alias_resolves_to_same_bound_root(tmp_path) -> None:
    if os.name != "nt":
        pytest.skip("Windows path case semantics")
    project_id = uuid.uuid4()
    repo = tmp_path / "CaseSensitiveName"
    repo.mkdir()
    alternate_case = str(repo).swapcase()
    binding = resolve_project_source_path(
        project_id,
        alternate_case,
        allowed_root=tmp_path,
        project_roots_json=json.dumps({str(project_id): repo.name.swapcase()}),
    )
    assert binding.source_path == repo.resolve(strict=True)


class _ProjectResult:
    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id

    def scalar_one_or_none(self):
        return SimpleNamespace(id=self.project_id)


class _DB:
    def __init__(self, project_id: uuid.UUID) -> None:
        self.project_id = project_id
        self.added = []
        self.commits = 0

    async def execute(self, _statement):
        return _ProjectResult(self.project_id)

    def add(self, row) -> None:
        self.added.append(row)

    async def commit(self) -> None:
        self.commits += 1

    async def refresh(self, row) -> None:
        row.created_at = datetime.now(tz=timezone.utc)


@pytest.mark.asyncio
async def test_project_a_cannot_enqueue_project_b_path_or_create_run(
    tmp_path, monkeypatch
) -> None:
    project_a = uuid.uuid4()
    project_b = uuid.uuid4()
    root_a = tmp_path / "tenant-a"
    root_b = tmp_path / "tenant-b"
    root_a.mkdir()
    root_b.mkdir()
    monkeypatch.setattr(analysis._settings, "source_allowed_root", str(tmp_path))
    monkeypatch.setattr(
        analysis._settings,
        "source_project_roots",
        json.dumps({str(project_a): "tenant-a", str(project_b): "tenant-b"}),
    )
    queue_called = False

    async def _queue():
        nonlocal queue_called
        queue_called = True
        raise AssertionError("queue must not be opened")

    monkeypatch.setattr(analysis, "get_queue", _queue)
    db = _DB(project_a)
    with pytest.raises(HTTPException) as exc_info:
        await analysis.trigger_analysis(
            project_a,
            analysis.AnalysisTriggerRequest(source_path=str(root_b)),
            SimpleNamespace(id=uuid.uuid4()),
            db,
        )
    assert (exc_info.value.status_code, exc_info.value.detail) == (
        400,
        "source_path_outside_project_root",
    )
    assert db.added == []
    assert db.commits == 0
    assert queue_called is False


@pytest.mark.asyncio
async def test_owned_subtree_enqueues_only_canonical_path(tmp_path, monkeypatch) -> None:
    project_id = uuid.uuid4()
    project_root = tmp_path / "tenant-a"
    subtree = project_root / "src"
    subtree.mkdir(parents=True)
    monkeypatch.setattr(analysis._settings, "source_allowed_root", str(tmp_path))
    monkeypatch.setattr(
        analysis._settings,
        "source_project_roots",
        json.dumps({str(project_id): "tenant-a"}),
    )
    enqueued: list[tuple] = []

    class _Queue:
        async def enqueue_job(self, *args, **kwargs):
            enqueued.append((*args, kwargs))
            return object()

    async def _queue():
        return _Queue()

    async def _audit(**_kwargs):
        return None

    monkeypatch.setattr(analysis, "get_queue", _queue)
    monkeypatch.setattr(analysis, "audit_record", _audit)
    db = _DB(project_id)
    result = await analysis.trigger_analysis(
        project_id,
        analysis.AnalysisTriggerRequest(source_path=str(subtree / ".")),
        SimpleNamespace(id=uuid.uuid4()),
        db,
    )
    assert result.status == "queued"
    assert len(db.added) == 1
    assert db.commits == 1
    assert enqueued[0][3] == str(subtree.resolve(strict=True))


@pytest.mark.asyncio
async def test_worker_rejects_forged_queue_before_any_git_read(
    tmp_path, monkeypatch
) -> None:
    project_a = uuid.uuid4()
    root_a = tmp_path / "tenant-a"
    root_b = tmp_path / "tenant-b"
    root_a.mkdir()
    root_b.mkdir()
    git_called = False

    async def _git_must_not_run(*_args, **_kwargs):
        nonlocal git_called
        git_called = True
        raise AssertionError("Git must not run for an unbound path")

    monkeypatch.setattr(source_checkout, "_git", _git_must_not_run)
    with pytest.raises(SourceCheckoutError, match="source_path_outside_project_root"):
        await prepare_source(
            str(root_b),
            project_id=project_a,
            run_id=uuid.uuid4(),
            git_sha="HEAD",
            allowed_root=tmp_path,
            project_roots_json=json.dumps({str(project_a): "tenant-a"}),
        )
    assert git_called is False
