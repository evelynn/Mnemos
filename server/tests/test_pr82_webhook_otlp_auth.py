"""PR-82 — fail-closed authentication on webhook + OTLP receivers.

The platform review found two open-default ingress paths:

* ``gitlab_webhook`` only validated the HMAC token *if* a secret was
  configured in ``PlatformSetting`` — a fresh install with the
  setting unset accepted forged GitLab events and enqueued analyses
  against any project id in the payload (§2.5);
* ``receive_traces`` (OTLP) had no auth at all and trusted the
  client-supplied ``X-Mnemos-Organization-Id`` header — anyone
  reachable on the OTLP port could poison ``runtime_observations``
  and lift edges to ``exercised=true`` for another tenant (§14.3).

Both now fail closed. Webhook: no secret configured → 503
``webhook_secret_not_configured``. OTLP: no tenant-bound token configuration →
503; wrong / missing Bearer → 401. The bearer determines the tenant.
"""

from __future__ import annotations

from pathlib import Path

_APP = Path(__file__).resolve().parents[1] / "app"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_webhook_rejects_when_secret_unset():
    body = _read(_APP / "api" / "webhooks.py")
    idx = body.find("async def gitlab_webhook(")
    slab = body[idx:idx + 1400]
    # The check is now unconditional ("if not expected" rejects),
    # not the old "if expected:" guard that opted in to validation.
    assert "if not expected:" in slab
    assert '"webhook_secret_not_configured"' in slab


def test_otlp_requires_bearer_token():
    body = _read(_APP / "runtime_receiver" / "router.py")
    assert "def _check_otlp_bearer(" in body
    idx = body.find("def _check_otlp_bearer(")
    slab = body[idx:idx + 1800]
    assert "MNEMOS_OTLP_ORG_TOKENS" in body
    assert "MNEMOS_OTLP_TOKEN" in body
    assert "hmac.compare_digest" in slab
    # Configuration validation lives in the strict identity parser, before the
    # constant-time bearer comparison helper.
    assert '"otlp_token_not_configured"' in body
    assert '"invalid_otlp_token"' in slab


def test_otlp_endpoint_calls_bearer_check():
    body = _read(_APP / "runtime_receiver" / "router.py")
    idx = body.find("async def receive_traces(")
    slab = body[idx:idx + 900]
    assert "_check_otlp_bearer(authorization, x_mnemos_organization_id)" in slab
    assert "_require_live_organization(db, org_id)" in slab
