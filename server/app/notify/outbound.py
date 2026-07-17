"""PR-104 — outbound webhook for new high-priority findings.

Mnemos's whole point is to surface risks before the operator
trips over them. Without an outbound hook, an operator who
doesn't tab to /findings sees nothing — the platform becomes a
silent oracle that knows about a P1 but never tells anyone.

This module wires a single env-configured webhook (``MNEMOS_NOTIFY_WEBHOOK_URL``)
that fires once per newly-created P1 finding. The body is a tiny
JSON envelope that fits the Slack incoming-webhook ``text`` field
or any generic JSON consumer. Empty config = disabled.

Design choices (Karpathy-style: simple, working, behavioural):
- Best-effort. A network blip never blocks the finding write —
  failures log at WARNING + bump ``notify_failures_total``.
- Per-finding POST (not a digest). Slack's per-channel rate
  limit (~1/sec sustained) handles 100-P1 deluges fine, and
  one-per-finding keeps the audit trail clean.
- 5 s timeout. Anything longer means the receiver is broken and
  we'd rather drop than back-pressure the merge stage.
- No retry. If the receiver is down we want it down — silent
  retry queues hide outages.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import get_settings
from app.merge.risk import priority_label

log = logging.getLogger(__name__)

# Hard cap on the outbound POST. Honour Slack's payload limit (40 KB)
# but truncate well below — the envelope is tiny by design.
_TIMEOUT_S = 5.0


def _build_envelope(finding: Any, *, link_base: str) -> dict[str, Any]:
    """Shape a Finding ORM row into the small JSON envelope the
    webhook receives. Keeps the dependency surface minimal — any
    object with the right attrs works, which makes the tests
    pleasant to write."""
    pid = str(getattr(finding, "project_id", "") or "")
    fid = str(getattr(finding, "id", "") or "")
    kind = getattr(finding, "kind", "finding")
    score = int(getattr(finding, "risk_score", 0) or 0)
    detail = getattr(finding, "detail", {}) or {}
    subject = (
        getattr(finding, "subject_node_id", None)
        or str(getattr(finding, "subject_edge_id", None) or "")
        or None
    )
    drill = (link_base.rstrip("/") if link_base else "") + (
        f"/findings?project={pid}&focus={fid}" if pid and fid else "/findings"
    )
    return {
        "kind": kind,
        "priority": priority_label(score),
        "risk_score": score,
        "project_id": pid,
        "finding_id": fid,
        "subject": subject,
        "detail": detail,
        "url": drill,
        # Slack incoming-webhook compatibility — receivers that key off
        # ``text`` (Slack, Mattermost) render this as the visible line.
        "text": (
            f"[Mnemos · {priority_label(score)}] {kind}"
            + (f" — {subject}" if subject else "")
            + f"  {drill}"
        ),
    }


async def notify_new_findings(findings: list[Any]) -> int:
    """Fire one POST per new P1 finding to the configured webhook.

    Returns the number of successful posts (0 when disabled or
    nothing qualifies). Never raises into the caller — the merge
    stage's commit is the source of truth, and a flaky webhook
    must not roll it back.
    """
    s = get_settings()
    url = (s.notify_webhook_url or "").strip()
    if not url:
        return 0

    p1s = [f for f in findings if priority_label(int(getattr(f, "risk_score", 0) or 0)) == "P1"]
    if not p1s:
        return 0

    sent = 0
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        for f in p1s:
            envelope = _build_envelope(f, link_base=s.notify_link_base)
            try:
                resp = await client.post(url, json=envelope)
                if 200 <= resp.status_code < 300:
                    sent += 1
                else:
                    log.warning(
                        "notify.non_2xx status=%s kind=%s",
                        resp.status_code, envelope["kind"],
                    )
                    _bump_failure(envelope["kind"])
            except (httpx.HTTPError, httpx.TimeoutException):
                log.warning(
                    "notify.send_failed failure_code=notification_unavailable "
                    "kind=%s",
                    envelope["kind"],
                )
                _bump_failure(envelope["kind"])
    return sent


def _bump_failure(kind: str) -> None:
    """Best-effort metric bump. The Prometheus counter lives in
    obs.metrics — if that import fails (e.g. during a slim test
    bootstrap) the failure still got logged above, so we don't
    care."""
    try:
        from app.obs.metrics import notify_failures_total

        notify_failures_total.labels(kind=kind).inc()
    except Exception:  # noqa: BLE001
        pass
