"""PR-104 — outbound webhook for new high-priority findings.

Without an outbound hook the platform is a silent oracle: it
knows about a P1 the moment the merge stage commits, but the
operator doesn't until they happen to tab to /findings. PR-104
adds a single env-configured webhook (``MNEMOS_NOTIFY_WEBHOOK_URL``)
that fires once per *newly-created* P1 finding.

Tests cover:
- envelope shape (Slack ``text`` field + structured fields)
- disabled-when-unset (empty URL = no httpx call)
- P1-only filter (P2 silently dropped)
- timeout / non-2xx / network error all bumped to the failures
  counter and never raised into the caller
- run_all integration point: ``session.new`` snapshot before commit
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# ─── envelope shape ────────────────────────────────────────────────


def _fake_finding(
    *,
    risk_score: int = 80,
    kind: str = "duplicate_endpoint",
    project_id: str = "11111111-1111-1111-1111-111111111111",
    finding_id: str = "22222222-2222-2222-2222-222222222222",
    subject_node_id: str | None = "graph:node:42",
    detail: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        risk_score=risk_score,
        kind=kind,
        project_id=project_id,
        id=finding_id,
        subject_node_id=subject_node_id,
        subject_edge_id=None,
        detail=detail or {"exposer_count": 3},
    )


def test_envelope_contains_priority_and_drilldown_url():
    from app.notify.outbound import _build_envelope

    env = _build_envelope(_fake_finding(), link_base="https://mnemos.example.com")
    assert env["priority"] == "P1"
    assert env["kind"] == "duplicate_endpoint"
    assert env["risk_score"] == 80
    assert env["subject"] == "graph:node:42"
    assert env["url"] == (
        "https://mnemos.example.com/findings"
        "?project=11111111-1111-1111-1111-111111111111"
        "&focus=22222222-2222-2222-2222-222222222222"
    )
    # Slack-compatible text line.
    assert "P1" in env["text"]
    assert "duplicate_endpoint" in env["text"]
    assert "graph:node:42" in env["text"]


def test_envelope_link_base_optional():
    from app.notify.outbound import _build_envelope

    env = _build_envelope(_fake_finding(), link_base="")
    # Relative URL still resolves on the platform's own host.
    assert env["url"].startswith("/findings?project=")


def test_envelope_handles_missing_subject():
    from app.notify.outbound import _build_envelope

    f = _fake_finding(subject_node_id=None)
    env = _build_envelope(f, link_base="")
    assert env["subject"] is None
    # No " — " between kind and url when subject is absent.
    assert "duplicate_endpoint  " in env["text"]


# ─── notify_new_findings end-to-end ──────────────────────────────────


@pytest.mark.asyncio
async def test_notify_disabled_when_url_unset(monkeypatch):
    from app.notify import outbound

    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(
        notify_webhook_url="", notify_link_base=""
    ))
    # Even with P1 findings, no POST happens.
    sent = await outbound.notify_new_findings([_fake_finding()])
    assert sent == 0


@pytest.mark.asyncio
async def test_notify_filters_to_p1_only(monkeypatch):
    from app.notify import outbound

    posted: list[dict] = []

    class FakeResp:
        status_code = 200

    class FakeClient:
        def __init__(self, *a, **kw):
            self.timeout = kw.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            posted.append({"url": url, "body": json})
            return FakeResp()

    monkeypatch.setattr(outbound.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(
        notify_webhook_url="https://hook.example/in", notify_link_base="https://m"
    ))

    findings = [
        _fake_finding(risk_score=80),   # P1 — fires
        _fake_finding(risk_score=60),   # P2 — dropped
        _fake_finding(risk_score=10),   # P4 — dropped
        _fake_finding(risk_score=99),   # P1 — fires
    ]
    sent = await outbound.notify_new_findings(findings)
    assert sent == 2
    assert all(p["url"] == "https://hook.example/in" for p in posted)
    # Both posted envelopes are P1.
    assert all(p["body"]["priority"] == "P1" for p in posted)


@pytest.mark.asyncio
async def test_notify_non_2xx_bumps_failure_counter(monkeypatch):
    from app.notify import outbound

    class FakeResp:
        status_code = 503

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def post(self, url, json): return FakeResp()

    bumped: list[str] = []
    monkeypatch.setattr(outbound, "_bump_failure", lambda kind: bumped.append(kind))
    monkeypatch.setattr(outbound.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(
        notify_webhook_url="https://hook.example/in", notify_link_base=""
    ))
    sent = await outbound.notify_new_findings([_fake_finding()])
    assert sent == 0  # 503 doesn't count
    assert bumped == ["duplicate_endpoint"]


@pytest.mark.asyncio
async def test_notify_network_error_is_swallowed(monkeypatch):
    """A flaky receiver must NEVER raise into the merge stage —
    that would roll the finding commit back."""
    from app.notify import outbound
    import httpx

    class FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def post(self, url, json):
            raise httpx.ConnectError("connection refused")

    bumped: list[str] = []
    monkeypatch.setattr(outbound, "_bump_failure", lambda kind: bumped.append(kind))
    monkeypatch.setattr(outbound.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(
        notify_webhook_url="https://hook.example/in", notify_link_base=""
    ))

    sent = await outbound.notify_new_findings([_fake_finding()])
    assert sent == 0
    assert bumped == ["duplicate_endpoint"]


@pytest.mark.asyncio
async def test_notify_empty_findings_list_is_noop(monkeypatch):
    """Common shape — most run_all invocations create zero new
    findings. Must not even build an httpx client."""
    from app.notify import outbound

    client_made: list[bool] = []

    class FakeClient:
        def __init__(self, *a, **kw):
            client_made.append(True)
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False
        async def post(self, url, json): return SimpleNamespace(status_code=200)

    monkeypatch.setattr(outbound.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(outbound, "get_settings", lambda: SimpleNamespace(
        notify_webhook_url="https://hook.example/in", notify_link_base=""
    ))

    assert await outbound.notify_new_findings([]) == 0
    # Filter ran before the client was constructed.
    assert client_made == []


# ─── run_all integration point ───────────────────────────────────


def test_run_all_snapshots_session_new_before_commit():
    """Source-level guard: ``run_all`` must read ``session.new``
    BEFORE ``await session.commit()``. After commit, the session's
    new-set is empty — reading it afterwards silently sends no
    notifications and the bug is invisible."""
    body = (
        Path(__file__).resolve().parents[1] / "app" / "merge" / "findings.py"
    ).read_text(encoding="utf-8")
    idx = body.find("async def run_all(")
    slab = body[idx:idx + 3000]
    snap = slab.find("session.new")
    commit = slab.find("await session.commit()")
    assert snap > 0, "run_all must snapshot session.new"
    assert commit > snap, "snapshot must run BEFORE commit"
    # Notifier is called only after commit (so it sees persisted rows).
    notify = slab.find("notify_new_findings(new_findings)")
    assert notify > commit


def test_run_all_filters_to_finding_instances_only():
    """``session.new`` can contain anything any caller added —
    upstream merge layers add Edge/Node rows in the same session.
    The snapshot must isinstance-check to Finding."""
    body = (
        Path(__file__).resolve().parents[1] / "app" / "merge" / "findings.py"
    ).read_text(encoding="utf-8")
    assert "isinstance(o, Finding)" in body


def test_notifier_errors_dont_break_run_all():
    """Source-level guard: the notifier call site must be wrapped
    so a broken ``app.notify`` import or a config-related raise
    never propagates into the merge stage's caller."""
    body = (
        Path(__file__).resolve().parents[1] / "app" / "merge" / "findings.py"
    ).read_text(encoding="utf-8")
    idx = body.find("notify_new_findings(new_findings)")
    slab = body[max(0, idx - 300):idx + 200]
    assert "try:" in slab
    # The wrapping except must be broad — anything from a missing
    # dependency to an httpx version skew is fair game.
    assert "except Exception" in slab


def test_settings_expose_notifier_env():
    from app.config import Settings

    # Pydantic field defaults must allow the platform to boot without
    # the operator setting anything (notifier opt-in by config).
    fields = Settings.model_fields
    assert "notify_webhook_url" in fields
    assert "notify_link_base" in fields
    assert fields["notify_webhook_url"].default == ""


def test_metrics_counter_registered():
    from app.obs.metrics import notify_failures_total

    # Labelled by finding kind so the dashboard can show "which
    # detector keeps tripping the webhook".
    assert "kind" in notify_failures_total._labelnames
