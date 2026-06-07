"""Webhook → ARQ enqueue + dedup + per-branch serialization (spec §1.5, §2.7).

These cover the pure-function pieces — the idempotency key derivation
and the per-branch queue name. The HTTP-side flow needs Postgres and
Redis and is covered by an integration test marked accordingly.
"""

from __future__ import annotations

import uuid

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


def test_queue_name_serializes_per_branch():
    """One in-flight ingest per (project, branch) — the queue name encodes that."""
    from app.api.webhooks import _queue_name

    project = uuid.uuid4()
    assert _queue_name(project, "refs/heads/main") != _queue_name(project, "refs/heads/dev")
    # And the format is what the worker expects to subscribe to.
    assert _queue_name(project, "refs/heads/main").startswith(f"ingest:{project}:")


@pytest.mark.integration
async def test_push_event_enqueues_and_audits(http_client, db_session):
    """A push webhook produces an AnalysisRun row plus an audit record."""
    import json

    from sqlalchemy import select

    from app.models.audit import AuditLog
    from app.models.auth import PlatformSetting
    from app.models.graph import AnalysisRun
    from app.models.projects import Project

    # The receiver is fail-closed: a webhook secret MUST be configured or
    # it 503s. Seed the legacy PlatformSetting secret and present the
    # matching token.
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
    await db_session.commit()

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
    assert body["enqueued"] is True

    runs = (
        await db_session.execute(
            select(AnalysisRun).where(AnalysisRun.project_id == project.id)
        )
    ).scalars().all()
    assert len(runs) == 1
    assert runs[0].scope == "incremental"
    assert runs[0].git_sha == "b" * 40
    assert runs[0].triggered_by.startswith("webhook:gitlab:")

    audits = (
        await db_session.execute(
            select(AuditLog).where(AuditLog.action == "webhook.received")
        )
    ).scalars().all()
    assert any(a.details and a.details.get("enqueued") for a in audits)


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
    await db_session.commit()

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
