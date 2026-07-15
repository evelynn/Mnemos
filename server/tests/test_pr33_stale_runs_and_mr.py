"""PR-33 — stale analysis_runs auto-reset cron + GitLab MR mock test.

Two gaps the 11th-round "거대 시스템 분석 가능성" audit named on the
readiness doc:

1. ``stale analysis_runs.status='running'`` rows needed manual SQL
   to reset — no cron auto-resets them when a worker crashes.
2. ``create_mr_from_worktree`` is fully implemented but had no
   integration test against a live or mock GitLab.

Both close here.
"""

from __future__ import annotations

import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stale analysis_runs cron — static asserts on the cron handler shape
# ---------------------------------------------------------------------------


_CRON = Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "cron_jobs.py"
_JOBS = Path(__file__).resolve().parents[1] / "app" / "orchestrator" / "jobs.py"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_stale_run_cron_function_exists():
    body = _read(_CRON)
    assert "async def _reset_stale_runs(" in body
    assert "async def run_reset_stale_runs(" in body


def test_stale_run_cron_uses_advisory_lock():
    """The new entry runs through the same advisory-lock helper as
    the existing crons so multi-worker deployments still get
    exactly-one execution."""
    body = _read(_CRON)
    # The entry function calls with_advisory_lock with the canonical
    # job name.
    assert 'with_advisory_lock(\n        SessionLocal_factory(), "reset_stale_runs"' in body \
        or '"reset_stale_runs"' in body


def test_stale_run_cron_covers_running_and_expired_queued_rows():
    body = _read(_CRON)
    assert "status = 'running'" in body
    assert "started_at < :cutoff" in body
    assert 'AnalysisRun.status == "queued"' in body
    assert "AnalysisRun.created_at < queued_cutoff" in body
    assert "JobStatus.complete" in body
    assert "JobStatus.not_found" in body


def test_stale_run_cron_sets_error_log():
    body = _read(_CRON)
    # The error log must explain what happened so an operator
    # debugging a wedged-then-failed run knows it was the sweep,
    # not the analyzer itself.
    assert "[reset_stale_runs]" in body
    assert "heartbeat lost" in body
    assert "queued job expired before worker pickup" in body


def test_stale_run_cutoff_is_longer_than_worker_timeout():
    body = _read(_CRON)
    assert "_STALE_RUN_AFTER_SEC" in body
    assert "24 * 60 * 60" in body


def test_stale_run_cron_registered_on_arq_schedule():
    body = _read(_JOBS)
    assert "run_reset_stale_runs" in body
    # Every 15 minutes — the spec'd cadence.
    assert "cron(run_reset_stale_runs" in body


def test_stale_run_cron_exported():
    body = _read(_CRON)
    assert '"run_reset_stale_runs"' in body


# ---------------------------------------------------------------------------
# Stale analysis_runs cron — happy-path against a fake session
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Stand-in for AsyncSession that records the UPDATE it sees."""

    def __init__(self, returning):
        self.returning = returning
        self.committed = False
        self.executed = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(self.returning)

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_reset_stale_runs_flips_old_running_rows():
    from app.orchestrator.cron_jobs import _reset_stale_runs

    pretend_resetting_ids = [(uuid.uuid4(),), (uuid.uuid4(),)]
    session = _FakeSession(returning=pretend_resetting_ids)
    out = await _reset_stale_runs(session)  # type: ignore[arg-type]
    assert out == {"reset": 2}
    assert session.committed is True
    # The UPDATE statement carried the right WHERE / SET shape.
    sql_text = session.executed[0][0]
    assert "UPDATE analysis_runs" in sql_text
    assert "status = 'failed'" in sql_text
    assert "status = 'running'" in sql_text


@pytest.mark.asyncio
async def test_reset_stale_runs_returns_zero_when_nothing_to_reset():
    from app.orchestrator.cron_jobs import _reset_stale_runs

    session = _FakeSession(returning=[])
    out = await _reset_stale_runs(session)  # type: ignore[arg-type]
    assert out == {"reset": 0}
    assert session.committed is True


@pytest.mark.asyncio
async def test_reset_stale_runs_uses_twenty_four_hour_cutoff():
    from app.orchestrator.cron_jobs import _reset_stale_runs

    session = _FakeSession(returning=[])
    before = datetime.now(tz=timezone.utc)
    await _reset_stale_runs(session)  # type: ignore[arg-type]
    after = datetime.now(tz=timezone.utc)
    # The bound parameter must be roughly ``now - 24h``.  The worker hard
    # timeout is eight hours, so a six-hour sweep could fail a live run while
    # it was still publishing graph facts.
    cutoff = session.executed[0][1]["cutoff"]
    assert before - timedelta(hours=24, seconds=2) <= cutoff <= after - timedelta(
        hours=24, seconds=-2
    )
    from app.orchestrator.cron_jobs import _STALE_QUEUED_AFTER_SEC

    assert _STALE_QUEUED_AFTER_SEC == 25 * 60 * 60


# ---------------------------------------------------------------------------
# GitLab MR creation — mock the python-gitlab SDK + the git subprocess
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_mr_returns_not_configured_without_settings():
    """No base_url / token / project ref → the handler must short-
    circuit with a clear message, not try to spawn git and hit a
    cryptic error."""
    from app.gitlab_client.mr import create_mr_from_worktree

    with patch(
        "app.gitlab_client.mr._get_setting", new=AsyncMock(return_value=None)
    ):
        result = await create_mr_from_worktree(
            session=MagicMock(),
            project_id=uuid.uuid4(),
            worktree=Path("/tmp/nonexistent"),
            plan_title="x",
            task_id="t1",
            description="d",
        )
    assert result.ok is False
    assert "gitlab_not_configured" in result.message


@pytest.mark.asyncio
async def test_create_mr_happy_path_with_mock_gitlab():
    """With settings present, every git step succeeds, and the SDK
    returns an MR with iid + web_url, the handler reports ok=True
    and surfaces both values to the caller."""
    from app.gitlab_client import mr as mr_mod

    settings = {
        "gitlab_base_url": "https://gitlab.example.com",
        "gitlab_token": "glpat-xxxxxxxx",
        f"gitlab_project:{uuid.uuid4().hex}": "group/repo",
    }

    async def fake_get_setting(session, key):
        # Project ref is keyed by the actual project_id at call time,
        # so resolve it from any key starting with the prefix.
        for k, v in settings.items():
            if k == key:
                return v
            if key.startswith("gitlab_project:") and k.startswith("gitlab_project:"):
                return v
        return None

    fake_mr = SimpleNamespace(
        iid=4242, web_url="https://gitlab.example.com/group/repo/-/merge_requests/4242"
    )
    fake_project = SimpleNamespace(
        mergerequests=SimpleNamespace(
            create=MagicMock(return_value=fake_mr),
        ),
    )
    fake_gl = SimpleNamespace(
        projects=SimpleNamespace(get=MagicMock(return_value=fake_project)),
    )

    fake_subproc = AsyncMock()
    # Each ``await proc.communicate()`` returns (stdout, stderr).
    fake_subproc.communicate = AsyncMock(return_value=(b"ok\n", b""))
    fake_subproc.returncode = 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_subproc

    with patch.object(mr_mod, "_get_setting", new=fake_get_setting), \
         patch(
            "app.gitlab_client.mr.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_create_subprocess_exec),
         ), \
         patch("gitlab.Gitlab", return_value=fake_gl):
        result = await mr_mod.create_mr_from_worktree(
            session=MagicMock(),
            project_id=uuid.uuid4(),
            worktree=Path("/tmp/wt"),
            plan_title="Add break-glass token",
            task_id="task-7",
            description="ultrareview clean",
        )

    assert result.ok is True
    assert result.iid == 4242
    assert result.url == "https://gitlab.example.com/group/repo/-/merge_requests/4242"
    assert result.message == "created"


@pytest.mark.asyncio
async def test_create_mr_rejects_late_index_payload_change_before_commit(
    tmp_path: Path,
):
    """The final staged snapshot must still equal the Gate-B patch."""

    from app.gitlab_client import mr as mr_mod

    worktree = tmp_path / "worktree"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(worktree)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "t"],
        check=True,
    )
    (worktree / "tracked.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "base"],
        check=True,
    )
    base_sha = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    (worktree / "late.txt").write_text("late unreviewed content\n")

    async def configured(_session, key):
        return {
            "gitlab_base_url": "https://gitlab.example.com",
            "gitlab_token": "glpat-test",
        }.get(key) or "group/repo"

    with patch.object(mr_mod, "_get_setting", new=configured):
        with pytest.raises(mr_mod.MRPayloadChanged, match="differs"):
            await mr_mod.create_mr_from_worktree(
                session=MagicMock(),
                project_id=uuid.uuid4(),
                worktree=worktree,
                plan_title="payload fence",
                task_id="task-1",
                description="must not commit",
                expected_diff="reviewed payload that is now stale",
            )

    assert subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
    ).strip() == base_sha


@pytest.mark.asyncio
async def test_create_mr_rejects_hook_changed_commit_before_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A commit-hook rewrite is reset to the authorised parent, never pushed."""

    from app.gitlab_client import mr as mr_mod
    from app.sandbox.worktree import canonical_worktree_diff

    worktree = tmp_path / "hook-worktree"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(worktree)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@t"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "t"],
        check=True,
    )
    tracked = worktree / "tracked.txt"
    tracked.write_text("base\n")
    subprocess.run(["git", "-C", str(worktree), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-q", "-m", "base"],
        check=True,
    )
    base_sha = subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    tracked.write_text("reviewed\n")
    expected_diff = await canonical_worktree_diff(worktree)

    async def configured(_session, key):
        return {
            "gitlab_base_url": "https://gitlab.example.com",
            "gitlab_token": "glpat-test",
        }.get(key) or "group/repo"

    async def hook_changed_payload(_worktree):
        return expected_diff + "hook-added-content"

    monkeypatch.setattr(mr_mod, "_get_setting", configured)
    monkeypatch.setattr(mr_mod, "canonical_committed_diff", hook_changed_payload)
    with pytest.raises(mr_mod.MRPayloadChanged, match="committed tree"):
        await mr_mod.create_mr_from_worktree(
            session=MagicMock(),
            project_id=uuid.uuid4(),
            worktree=worktree,
            plan_title="hook payload fence",
            task_id="task-2",
            description="must not push",
            expected_diff=expected_diff,
        )

    assert subprocess.check_output(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        text=True,
    ).strip() == base_sha
    assert tracked.read_text() == "reviewed\n"


@pytest.mark.asyncio
async def test_create_mr_surfaces_git_step_failure_clearly():
    """When a git step fails the handler must say *which* step and
    include the captured output so an operator can diagnose without
    rerunning the failing job."""
    from app.gitlab_client import mr as mr_mod

    async def fake_get_setting(session, key):
        return {
            "gitlab_base_url": "https://gitlab.example.com",
            "gitlab_token": "glpat-xxxxxxxx",
        }.get(key) or "group/repo"

    fake_subproc = AsyncMock()
    fake_subproc.communicate = AsyncMock(
        return_value=(b"fatal: not a git repository\n", b"")
    )
    fake_subproc.returncode = 128

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_subproc

    with patch.object(mr_mod, "_get_setting", new=fake_get_setting), \
         patch(
            "app.gitlab_client.mr.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_create_subprocess_exec),
         ):
        result = await mr_mod.create_mr_from_worktree(
            session=MagicMock(),
            project_id=uuid.uuid4(),
            worktree=Path("/tmp/wt"),
            plan_title="x",
            task_id="t1",
            description="d",
        )

    assert result.ok is False
    assert "git_step_failed" in result.message
    # The git output is preserved (truncated to 400 chars) so the
    # operator can read the actual failure reason.
    assert "not a git repository" in result.message


@pytest.mark.asyncio
async def test_create_mr_surfaces_gitlab_api_failure():
    """python-gitlab raising an exception (network, auth, project
    not found) must surface as ok=False with the cause in the
    message — never a 500 to the operator."""
    from app.gitlab_client import mr as mr_mod

    async def fake_get_setting(session, key):
        return {
            "gitlab_base_url": "https://gitlab.example.com",
            "gitlab_token": "glpat-xxxxxxxx",
        }.get(key) or "group/repo"

    fake_subproc = AsyncMock()
    fake_subproc.communicate = AsyncMock(return_value=(b"ok\n", b""))
    fake_subproc.returncode = 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        return fake_subproc

    class _GitlabAuthError(Exception):
        pass

    with patch.object(mr_mod, "_get_setting", new=fake_get_setting), \
         patch(
            "app.gitlab_client.mr.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=fake_create_subprocess_exec),
         ), \
         patch("gitlab.Gitlab", side_effect=_GitlabAuthError("401 Unauthorized")):
        result = await mr_mod.create_mr_from_worktree(
            session=MagicMock(),
            project_id=uuid.uuid4(),
            worktree=Path("/tmp/wt"),
            plan_title="x",
            task_id="t1",
            description="d",
        )

    assert result.ok is False
    assert "gitlab_api_failed" in result.message
    assert "401" in result.message
