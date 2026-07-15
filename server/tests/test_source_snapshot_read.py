from __future__ import annotations

import base64
import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.mcp.file_read import MAX_FILE_RESPONSE_BYTES, read_project_file
from app.mcp.server import _cap_response
from app.models.graph import AnalysisRun, GraphHead
from app.orchestrator.source_manifest import INDEX_CONTRACT_VERSION
from app.source_snapshot import SOURCE_SNAPSHOT_CONTRACT, normalize_project_path
from app.testing.graph_publication import published_run_fields


class _Result:
    def __init__(self, value) -> None:
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def first(self):
        return self.value

    def one_or_none(self):
        return self.value


class _FakeDB:
    def __init__(
        self,
        run,
        *,
        changed_generation: int | None = None,
        changed_overlay_generation: int | None = None,
        change_on_graph_head_read: int = 2,
    ) -> None:
        self.run = run
        self.changed_generation = changed_generation
        self.changed_overlay_generation = changed_overlay_generation
        self.change_on_graph_head_read = change_on_graph_head_read
        self.calls = 0
        self.graph_head_reads = 0

    async def execute(self, statement):
        self.calls += 1
        # Default reads first pin the ready GraphHead, load exactly that run,
        # then revalidate the head. Explicit historical reads select only the
        # requested AnalysisRun. Keep this fake query-aware so repeated reads
        # do not accidentally consume a positional response queue.
        selected_entities = [
            description.get("expr")
            for description in statement.column_descriptions
        ]
        if selected_entities == [GraphHead, AnalysisRun]:
            self.graph_head_reads += 1
            if self.run is None:
                return _Result(None)
            generation = 1
            overlay_generation = 0
            joined_run = self.run
            if (
                self.graph_head_reads >= self.change_on_graph_head_read
                and self.changed_generation is not None
            ):
                generation = self.changed_generation
                changed_fields = published_run_fields(
                    generation=generation,
                    published_at=self.run.completed_at,
                    authoritative_sources=self.run.graph_authoritative_sources,
                    stats={
                        key: value
                        for key, value in self.run.stats.items()
                        if key != "graph_publication"
                    },
                )
                joined_run = SimpleNamespace(
                    **{**self.run.__dict__, **changed_fields}
                )
            if (
                self.graph_head_reads >= self.change_on_graph_head_read
                and self.changed_overlay_generation is not None
            ):
                overlay_generation = self.changed_overlay_generation
            head = SimpleNamespace(
                project_id=self.run.project_id,
                current_run_id=self.run.id,
                generation=generation,
                overlay_generation=overlay_generation,
                published_at=self.run.completed_at,
                state="ready",
            )
            return _Result((head, joined_run))
        if AnalysisRun in selected_entities:
            return _Result(self.run)
        raise AssertionError(f"unexpected query columns: {selected_entities}")


@dataclass(frozen=True)
class _Repo:
    project_id: uuid.UUID
    run_id: uuid.UUID
    mirror_root: Path
    checkout: Path
    revision: str


def _git(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, *, bare_mirror: bool = False) -> _Repo:
    if shutil.which("git") is None:
        pytest.skip("git executable is required")
    project_id = uuid.uuid4()
    run_id = uuid.uuid4()
    mirror_root = tmp_path / "mirrors"
    mirror_root.mkdir()
    checkout = tmp_path / "seed" if bare_mirror else mirror_root / str(project_id)
    checkout.mkdir()
    _git("init", cwd=checkout)
    _git("config", "user.email", "snapshot@example.test", cwd=checkout)
    _git("config", "user.name", "Snapshot Test", cwd=checkout)
    (checkout / "src").mkdir()
    (checkout / "src" / "sample.py").write_text(
        "alpha = 1\n메시지 = 'analysis revision'\nomega = 3\n",
        encoding="utf-8",
    )
    (checkout / "giant.txt").write_text("x" + "🙂" * 100_000, encoding="utf-8")
    (checkout / "binary.bin").write_bytes(b"\xff\x00\n")
    _git("add", ".", cwd=checkout)
    _git("commit", "-m", "snapshot", cwd=checkout)
    revision = _git("rev-parse", "HEAD", cwd=checkout)
    if bare_mirror:
        _git("clone", "--bare", str(checkout), str(mirror_root / f"{project_id}.git"))
    return _Repo(project_id, run_id, mirror_root, checkout, revision)


def _run(repo: _Repo, *, triggered_by: str = "webhook:github", stats=None):
    completed_at = datetime.now(tz=timezone.utc)
    default_stats = {
        "source_manifest": {"contract_version": INDEX_CONTRACT_VERSION},
        "refresh": {"authoritative_root": True},
    }
    run_stats = dict(default_stats if stats is None else stats)
    publication_fields = published_run_fields(
        generation=1,
        published_at=completed_at,
        authoritative_sources=["ggoss-py"],
        stats=run_stats,
    )
    return SimpleNamespace(
        id=repo.run_id,
        project_id=repo.project_id,
        status="completed",
        triggered_by=triggered_by,
        git_sha=repo.revision,
        scope="full",
        started_at=completed_at,
        stats=publication_fields["stats"],
        graph_base_generation=publication_fields["graph_base_generation"],
        graph_authoritative_sources=publication_fields[
            "graph_authoritative_sources"
        ],
        graph_coverage_sealed_at=publication_fields[
            "graph_coverage_sealed_at"
        ],
        completed_at=completed_at,
        created_at=datetime.now(tz=timezone.utc),
    )


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("../secret.txt", "source_path_escape_rejected"),
        ("src/../../secret.txt", "source_path_escape_rejected"),
        ("/etc/passwd", "absolute_source_path_rejected"),
        (r"C:\Windows\system.ini", "absolute_source_path_rejected"),
        (r"\\server\share\file", "absolute_source_path_rejected"),
        ("x" * 4097, "invalid_source_path"),
    ],
)
async def test_path_boundary_rejects_absolute_and_parent_before_db(path, code):
    db = _FakeDB(None)
    result = await read_project_file(
        db,
        project_id=uuid.uuid4(),
        file_path=path,
        mirror_root="unused",
    )
    assert result["error"] == code
    assert result["path"] == path
    assert db.calls == 0


def test_relative_windows_path_is_normalized_once():
    assert normalize_project_path(r"src\package\module.py") == "src/package/module.py"


def test_generic_mcp_cap_is_hard_even_for_an_oversized_dict_key():
    capped = _cap_response({"k" * 10_000: ["v" * 100]}, max_bytes=128)
    assert len(capped.encode("utf-8")) <= 128


@pytest.mark.parametrize("bare_mirror", [False, True])
async def test_read_is_pinned_to_completed_run_not_mutable_checkout(tmp_path, bare_mirror):
    repo = _make_repo(tmp_path, bare_mirror=bare_mirror)
    # Move the checked-out file after analysis. A filesystem reader would now
    # lie; git-show at the run commit must still return the committed bytes.
    (repo.checkout / "src" / "sample.py").write_text(
        "drifted = True\n", encoding="utf-8"
    )
    db = _FakeDB(_run(repo))

    result = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/sample.py",
        start_line=2,
        end_line=2,
        run_id=repo.run_id,
        mirror_root=repo.mirror_root,
    )

    assert result["schema"] == "mnemos.source.file_window.v1"
    assert result["run_id"] == str(repo.run_id)
    assert result["revision"] == repo.revision
    assert result["path"] == "src/sample.py"
    assert result["range"] == {"start_line": 2, "end_line": 2}
    assert result["content"] == "메시지 = 'analysis revision'\n"
    assert result["truncated"] is True
    assert result["truncation"] == {
        "before": True,
        "after": True,
        "output_budget": False,
    }


async def test_giant_multibyte_line_is_utf8_and_stays_below_both_caps(tmp_path):
    repo = _make_repo(tmp_path)
    result = await read_project_file(
        _FakeDB(_run(repo)),
        project_id=repo.project_id,
        file_path="giant.txt",
        mirror_root=repo.mirror_root,
    )

    assert result["encoding"] == "utf-8"
    assert "�" not in result["content"]
    assert result["truncated"] is True
    assert result["truncation"]["output_budget"] is True
    assert len(json.dumps(result, default=str).encode("utf-8")) <= MAX_FILE_RESPONSE_BYTES
    capped = _cap_response(result)
    assert len(capped.encode("utf-8")) < 50 * 1024
    assert json.loads(capped)["revision"] == repo.revision


async def test_binary_window_uses_real_base64_without_whole_file_read(tmp_path):
    repo = _make_repo(tmp_path)
    result = await read_project_file(
        _FakeDB(_run(repo)),
        project_id=repo.project_id,
        file_path="binary.bin",
        mirror_root=repo.mirror_root,
    )
    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content_base64"]) == b"\xff\x00\n"
    assert result["truncated"] is False


async def test_legacy_manual_run_reports_unavailable_and_required_stats(tmp_path):
    repo = _make_repo(tmp_path)
    result = await read_project_file(
        _FakeDB(_run(repo, triggered_by="user:operator")),
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )
    assert result["error"] == "source_snapshot_unavailable"
    assert "drift" in result["reason"]
    required = result["required_stats_contract"]
    assert required["stats_path"] == "analysis_run.stats.source_snapshot"
    assert required["git_commit"]["contract_version"] == SOURCE_SNAPSHOT_CONTRACT
    assert required["git_commit"]["tree_prefix"]
    assert required["non_git"]["kind"] == "content_archive"


async def test_pre_contract_webhook_is_not_assumed_to_match_its_sha(tmp_path):
    repo = _make_repo(tmp_path)
    result = await read_project_file(
        _FakeDB(_run(repo, stats={})),
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )
    assert result["error"] == "source_snapshot_unavailable"
    assert result["required_stats_contract"]["git_commit"]["immutable"] is True


@pytest.mark.parametrize("staged_status", ["running", "failed", "cancelled"])
async def test_default_read_keeps_published_source_during_unpublished_attempt(
    tmp_path, staged_status
):
    repo = _make_repo(tmp_path)
    db = _FakeDB(_run(repo))
    result = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    # A running/failed/cancelled staged attempt never owns GraphHead, so its
    # status cannot poison the last successfully published source snapshot.
    assert staged_status in {"running", "failed", "cancelled"}
    assert "error" not in result
    assert result["run_id"] == str(repo.run_id)
    assert result["revision"] == repo.revision
    assert db.graph_head_reads == 3


async def test_default_read_discards_result_when_graph_head_changes(tmp_path):
    repo = _make_repo(tmp_path)
    result = await read_project_file(
        _FakeDB(_run(repo), changed_generation=2),
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    assert result["error"] == "current_graph_snapshot_changed_retry"
    assert result["run_id"] == str(repo.run_id)
    assert result["revision"] == repo.revision
    assert "retry" in result["reason"]


async def test_default_read_revalidates_after_git_stream_finishes(tmp_path):
    repo = _make_repo(tmp_path)
    db = _FakeDB(
        _run(repo),
        changed_generation=2,
        change_on_graph_head_read=3,
    )
    result = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    assert result["error"] == "current_graph_snapshot_changed_retry"
    assert db.graph_head_reads == 3


async def test_default_read_discards_post_io_overlay_revision_change(tmp_path):
    repo = _make_repo(tmp_path)
    db = _FakeDB(
        _run(repo),
        changed_overlay_generation=1,
        change_on_graph_head_read=3,
    )
    result = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    assert result["error"] == "current_graph_snapshot_changed_retry"
    assert db.graph_head_reads == 3


async def test_explicit_historical_run_bypasses_current_graph_mixed_guard(tmp_path):
    repo = _make_repo(tmp_path)
    db = _FakeDB(_run(repo))
    result = await read_project_file(
        db,
        project_id=repo.project_id,
        run_id=repo.run_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    assert "error" not in result
    assert result["revision"] == repo.revision
    # Explicit historical reads select only the requested completed run; the
    # current GraphHead probe is intentionally not issued.
    assert db.calls == 1


async def test_explicit_historical_run_rejects_bool_generation_receipt(tmp_path):
    repo = _make_repo(tmp_path)
    run = _run(repo)
    run.stats = {
        **run.stats,
        "graph_publication": {
            **run.stats["graph_publication"],
            "generation": True,
        },
    }

    result = await read_project_file(
        _FakeDB(run),
        project_id=repo.project_id,
        run_id=repo.run_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )

    assert result["error"] == "published_analysis_run_not_found"


async def test_manual_run_with_canonical_git_provenance_is_readable(tmp_path):
    repo = _make_repo(tmp_path)
    provenance = {
        "source_snapshot": {
            "contract_version": SOURCE_SNAPSHOT_CONTRACT,
            "kind": "git_commit",
            "revision": repo.revision,
            "repository_key": str(repo.project_id),
            "tree_prefix": "",
            "immutable": True,
        }
    }
    result = await read_project_file(
        _FakeDB(_run(repo, triggered_by="user:operator", stats=provenance)),
        project_id=repo.project_id,
        file_path="src/sample.py",
        start_line=1,
        end_line=1,
        mirror_root=repo.mirror_root,
    )
    assert "error" not in result
    assert result["content"] == "alpha = 1\n"


async def test_manual_run_reads_allowed_root_locator_without_project_mirror(tmp_path):
    repo = _make_repo(tmp_path)
    relative_repository = repo.checkout.relative_to(tmp_path).as_posix()
    provenance = {
        "source_snapshot": {
            "contract_version": SOURCE_SNAPSHOT_CONTRACT,
            "kind": "git_commit",
            "revision": repo.revision,
            "repository_key": str(repo.project_id),
            "repository_locator": {
                "kind": "allowed_root_relative",
                "path": relative_repository,
            },
            "tree_prefix": "",
            "immutable": True,
        }
    }
    result = await read_project_file(
        _FakeDB(_run(repo, triggered_by="user:operator", stats=provenance)),
        project_id=repo.project_id,
        file_path="src/sample.py",
        start_line=1,
        end_line=1,
        mirror_root=tmp_path / "missing-mirrors",
        allowed_root=tmp_path,
    )
    assert "error" not in result
    assert result["revision"] == repo.revision
    assert result["content"] == "alpha = 1\n"


async def test_symbolic_revision_is_never_resolved_against_moving_head(tmp_path):
    repo = _make_repo(tmp_path)
    run = _run(repo)
    run.git_sha = "HEAD"
    result = await read_project_file(
        _FakeDB(run),
        project_id=repo.project_id,
        file_path="src/sample.py",
        mirror_root=repo.mirror_root,
    )
    assert result["error"] == "source_snapshot_unavailable"
    assert result["revision"] == "HEAD"
    assert "unpinned" in result["reason"]


async def test_missing_path_and_bad_range_return_actionable_envelopes(tmp_path):
    repo = _make_repo(tmp_path)
    db = _FakeDB(_run(repo))
    missing = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/missing.py",
        mirror_root=repo.mirror_root,
    )
    assert missing["error"] == "source_path_not_found"
    assert missing["run_id"] == str(repo.run_id)
    assert missing["revision"] == repo.revision

    invalid = await read_project_file(
        db,
        project_id=repo.project_id,
        file_path="src/sample.py",
        start_line=10,
        end_line=9,
        mirror_root=repo.mirror_root,
    )
    assert invalid["error"] == "invalid_line_range"
    assert invalid["range"] == {"start_line": 10, "end_line": 9}
