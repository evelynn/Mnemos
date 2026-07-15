"""Webhook → ARQ enqueue + dedup + safe worker serialization (spec §1.5, §2.7).

These cover the pure-function pieces — the idempotency key derivation
and the fixed worker queue contract. The HTTP-side flow needs Postgres and
Redis and is covered by an integration test marked accordingly.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

_WEBHOOK_SECRET = "test-webhook-secret-1234"


def test_job_id_includes_before_so_force_push_isnt_swallowed():
    """A force-push that reuses an old `after` SHA must still requeue."""
    from app.api.webhooks import _job_id

    project = uuid.uuid4()
    ref = "refs/heads/main"
    forward = _job_id(project, "AAAA", "BBBB", ref)
    revert_of_revert = _job_id(project, "CCCC", "BBBB", ref)
    assert forward != revert_of_revert, (
        "if we keyed on `after` alone, ARQ would dedup these as one job"
    )


def test_job_id_is_stable_for_same_inputs():
    """ARQ's dedup window only works if our key is deterministic."""
    from app.api.webhooks import _job_id

    project = uuid.uuid4()
    a = _job_id(project, "x", "y", "refs/heads/main")
    b = _job_id(project, "x", "y", "refs/heads/main")
    assert a == b


def test_job_id_varies_per_project_and_ref():
    """Same SHAs on different branches → distinct jobs."""
    from app.api.webhooks import _job_id

    p1 = uuid.uuid4()
    p2 = uuid.uuid4()
    main = _job_id(p1, "x", "y", "refs/heads/main")
    dev = _job_id(p1, "x", "y", "refs/heads/dev")
    other_proj = _job_id(p2, "x", "y", "refs/heads/main")
    assert main != dev
    assert main != other_proj


def test_default_branch_ref_normalizes_project_branch():
    from app.api.webhooks import _default_branch_ref

    assert _default_branch_ref("main") == "refs/heads/main"
    assert _default_branch_ref(" refs/heads/trunk ") == "refs/heads/trunk"


def test_worker_uses_fixed_queue_with_safe_analysis_concurrency():
    """Dynamic queue names strand jobs; one fixed worker polls one fixed queue."""
    from app.orchestrator.jobs import WorkerSettings

    assert WorkerSettings.max_jobs == 1
    assert WorkerSettings.max_tries == 1
    assert WorkerSettings.retry_jobs is False
    assert WorkerSettings.allow_abort_jobs is True
    assert WorkerSettings.job_timeout >= 8 * 60 * 60


@pytest.mark.asyncio
async def test_enqueue_exception_terminalizes_webhook_run(monkeypatch):
    from app.api import webhooks

    project = SimpleNamespace(id=uuid.uuid4(), default_branch="main")
    payload = {
        "object_kind": "push",
        "before": "a" * 40,
        "after": "b" * 40,
        "ref": "refs/heads/main",
        "project": {"id": 123, "path_with_namespace": "team/repo"},
    }

    class _Request:
        async def json(self):
            return payload

    class _DB:
        def __init__(self):
            self.run = None
            self.commits = 0

        def add(self, row):
            self.run = row

        async def commit(self):
            self.commits += 1

        async def refresh(self, row):
            return None

    async def _secret(db):
        return _WEBHOOK_SECRET

    async def _project(db, body):
        return project

    async def _broken_queue():
        raise ConnectionError("redis unavailable")

    async def _audit(**kwargs):
        return None

    monkeypatch.setattr(webhooks, "_secret", _secret)
    monkeypatch.setattr(webhooks, "_resolve_project", _project)
    monkeypatch.setattr(webhooks, "get_queue", _broken_queue)
    monkeypatch.setattr(webhooks, "audit_record", _audit)
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(source_mirror_root="C:/mirrors"),
    )

    db = _DB()
    result = await webhooks.gitlab_webhook(
        _Request(),
        x_gitlab_token=_WEBHOOK_SECRET,
        x_gitlab_event="Push Hook",
        x_gitlab_event_uuid="event-1",
        db=db,
    )
    assert result["enqueued"] is False
    assert result["skip_reason"] == "analysis_enqueue_failed"
    assert db.run.status == "failed"
    assert db.run.completed_at is not None
    assert db.run.error_log == "analysis_enqueue_failed:ConnectionError"
    assert db.run.stats["webhook_ref"] == "refs/heads/main"
    assert db.run.stats["default_branch_ref"] == "refs/heads/main"
    assert db.commits == 2


@pytest.mark.asyncio
async def test_non_default_branch_never_creates_or_queues_run(monkeypatch):
    from app.api import webhooks

    project = SimpleNamespace(id=uuid.uuid4(), default_branch="main")
    captured_audit = {}

    class _Request:
        async def json(self):
            return {
                "object_kind": "push",
                "before": "a" * 40,
                "after": "b" * 40,
                "ref": "refs/heads/feature/load-spike",
                "project": {"id": 123, "path_with_namespace": "team/repo"},
            }

    class _DB:
        def add(self, row):
            raise AssertionError("feature-branch push created an AnalysisRun")

    async def _secret(db):
        return _WEBHOOK_SECRET

    async def _project(db, body):
        return project

    async def _queue():
        raise AssertionError("feature-branch push reached the queue")

    async def _audit(**kwargs):
        captured_audit.update(kwargs)

    monkeypatch.setattr(webhooks, "_secret", _secret)
    monkeypatch.setattr(webhooks, "_resolve_project", _project)
    monkeypatch.setattr(webhooks, "get_queue", _queue)
    monkeypatch.setattr(webhooks, "audit_record", _audit)

    result = await webhooks.gitlab_webhook(
        _Request(),
        x_gitlab_token=_WEBHOOK_SECRET,
        x_gitlab_event="Push Hook",
        db=_DB(),
    )
    assert result["skip_reason"] == "non_default_branch"
    assert result["enqueued"] is False
    assert captured_audit["action"] == "webhook.skipped"
    assert captured_audit["details"]["ref"] == "refs/heads/feature/load-spike"
    assert captured_audit["details"]["default_branch_ref"] == "refs/heads/main"


@pytest.mark.integration
async def test_push_event_enqueues_and_audits(
    http_client, db_session, monkeypatch, tmp_path
):
    """A push webhook produces an AnalysisRun row plus an audit record."""
    import json

    from sqlalchemy import select

    from app.models.audit import AuditLog
    from app.models.auth import PlatformSetting
    from app.models.graph import AnalysisRun
    from app.models.projects import Project
    from app.api import webhooks

    # Patch the handler's lookup seam, not one particular cached Settings
    # instance. Earlier Docker-free tests deliberately clear/recreate the
    # settings cache while switching DB backends, so mutating the object that
    # happened to be cached here made this integration test order-dependent.
    monkeypatch.setattr(
        webhooks,
        "get_settings",
        lambda: SimpleNamespace(source_mirror_root=str(tmp_path)),
    )

    # The receiver is fail-closed (no secret -> 503). Seed the secret +
    # the project and FLUSH only — the request handler shares this exact
    # session via the get_session override, so it sees the flushed rows
    # without a commit. (Committing restarts the savepoint and the rows
    # then read back as missing on asyncpg, which broke _secret /
    # _resolve_project here even though a committed User row was visible.)
    db_session.add(
        PlatformSetting(
            key="gitlab_webhook_secret", value={"secret": _WEBHOOK_SECRET}
        )
    )

    project = Project(
        name=f"hook-{uuid.uuid4().hex[:6]}",
        gitlab_project_id=1234567,
        gitlab_url="http://gitlab/x",
        default_branch="main",
        languages=["python"],
    )
    db_session.add(project)
    await db_session.flush()

    payload = {
        "object_kind": "push",
        "before": "a" * 40,
        "after": "b" * 40,
        "ref": "refs/heads/main",
        "project": {
            "id": 1234567,
            "path_with_namespace": "team/repo",
        },
    }
    r = await http_client.post(
        "/webhooks/gitlab",
        content=json.dumps(payload),
        headers={
            "content-type": "application/json",
            "x-gitlab-event": "Push Hook",
            "x-gitlab-token": _WEBHOOK_SECRET,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["enqueued"] is True, body

    runs = (
        await db_session.execute(
            select(AnalysisRun).where(AnalysisRun.project_id == project.id)
        )
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].scope == "incremental"
    assert runs[0].git_sha == "b" * 40
    assert runs[0].triggered_by.startswith("webhook:gitlab:")
    assert runs[0].stats["webhook_ref"] == "refs/heads/main"
    assert runs[0].stats["default_branch_ref"] == "refs/heads/main"

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "webhook.received")
        )
    ).scalars().all()
    assert any(
        a.details
        and a.details.get("enqueued")
        and a.details.get("ref") == "refs/heads/main"
        and a.details.get("default_branch_ref") == "refs/heads/main"
        for a in audits
    )


@pytest.mark.integration
async def test_feature_branch_push_is_audited_but_not_enqueued(
    http_client, db_session
):
    import json

    from sqlalchemy import select

    from app.models.audit import AuditLog
    from app.models.auth import PlatformSetting
    from app.models.graph import AnalysisRun
    from app.models.projects import Project

    db_session.add(
        PlatformSetting(
            key="gitlab_webhook_secret", value={"secret": _WEBHOOK_SECRET}
        )
    )
    project = Project(
        name=f"hook-branch-{uuid.uuid4().hex[:6]}",
        gitlab_project_id=7654321,
        gitlab_url="http://gitlab/branch",
        default_branch="main",
        languages=["python"],
    )
    db_session.add(project)
    await db_session.flush()

    ref = "refs/heads/feature/high-churn"
    response = await http_client.post(
        "/webhooks/gitlab",
        content=json.dumps(
            {
                "object_kind": "push",
                "before": "c" * 40,
                "after": "d" * 40,
                "ref": ref,
                "project": {
                    "id": project.gitlab_project_id,
                    "path_with_namespace": "team/repo",
                },
            }
        ),
        headers={
            "content-type": "application/json",
            "x-gitlab-event": "Push Hook",
            "x-gitlab-token": _WEBHOOK_SECRET,
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "status": "received",
        "enqueued": False,
        "skip_reason": "non_default_branch",
    }
    assert (
        await db_session.execute(
            select(AnalysisRun).where(AnalysisRun.project_id == project.id)
        )
    ).scalars().all() == []
    audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "webhook.skipped")
            .order_by(AuditLog.occurred_at.desc())
        )
    ).scalars().first()
    assert audit is not None
    assert audit.details["ref"] == ref
    assert audit.details["default_branch_ref"] == "refs/heads/main"
    assert audit.details["skip_reason"] == "non_default_branch"


@pytest.mark.integration
async def test_merge_request_event_does_not_enqueue(http_client, db_session):
    """MR open/merge events are eventually represented as push events on the
    target branch; we deliberately do not run a preview analysis from MR
    webhooks in Phase 1."""
    import json

    from sqlalchemy import select

    from app.models.auth import PlatformSetting
    from app.models.graph import AnalysisRun
    from app.models.projects import Project

    # See the push test: seed + flush only, the handler shares this session.
    db_session.add(
        PlatformSetting(
            key="gitlab_webhook_secret", value={"secret": _WEBHOOK_SECRET}
        )
    )

    project = Project(
        name=f"hook-mr-{uuid.uuid4().hex[:6]}",
        gitlab_project_id=2222222,
        gitlab_url="http://gitlab/y",
        default_branch="main",
        languages=["python"],
    )
    db_session.add(project)
    await db_session.flush()

    r = await http_client.post(
        "/webhooks/gitlab",
        content=json.dumps(
            {
                "object_kind": "merge_request",
                "project": {"id": 2222222},
                "object_attributes": {"action": "open"},
            }
        ),
        headers={
            "content-type": "application/json",
            "x-gitlab-token": _WEBHOOK_SECRET,
        },
    )
    assert r.status_code == 200
    assert r.json()["enqueued"] is False

    runs = (
        await db_session.execute(
            select(AnalysisRun).where(AnalysisRun.project_id == project.id)
        )
    ).scalars().all()
    assert runs == []
