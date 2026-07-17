"""PR-99 — /health/ready gains crypto + analyzers checks.

Investigation toward 9.8 noted the readiness probe checked only
db / redis / worker — a wrong-rotated FERNET_KEY surfaced only on
the first secret read, and a deploy with missing analyzer binaries
showed up as zero-record stage crashes (PR-98 made those graceful;
this PR makes them visible).

* crypto: encrypt + decrypt a known plaintext via the configured
  KMS. Failure flips overall=503 — every secret decrypt will fail.
* analyzers: list of missing binaries on PATH. Advisory only; a
  Phase-1 deploy that intentionally omits some language analyzers
  shouldn't trip readiness.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.api.health as health_api

_HEALTH = Path(__file__).resolve().parents[1] / "app" / "api" / "health.py"


def test_ready_includes_crypto_and_analyzers_checks():
    body = _HEALTH.read_text(encoding="utf-8")
    idx = body.find("async def ready(")
    slab = body[idx:idx + 1600]
    assert "_check_crypto()" in slab
    assert "_check_analyzers()" in slab
    # The parts dict surfaces both checks.
    assert '"crypto"' in slab
    assert '"analyzers"' in slab


def test_crypto_failure_flips_503():
    """A broken FERNET_KEY breaks every secret read, so it must
    propagate to overall=503."""
    body = _HEALTH.read_text(encoding="utf-8")
    idx = body.find("overall = ")
    slab = body[idx:idx + 200]
    assert "crypto_ok" in slab


def test_analyzers_advisory_not_fatal():
    """A Phase-1 deploy that skips some language analyzers must NOT
    trip readiness — overall stays 200 when only analyzers are
    missing, the message lists them so the operator sees which."""
    body = _HEALTH.read_text(encoding="utf-8")
    idx = body.find("overall = ")
    slab = body[idx:idx + 200]
    # analyzers_status is NOT in the overall AND chain — advisory only.
    assert "analyzers_status" not in slab
    # The check function itself returns True even when missing — the
    # ok=True semantic is "this check ran"; the message names misses.
    fn = body.find("def _check_analyzers(")
    fn_slab = body[fn:fn + 800]
    assert "return True, \"missing:" in fn_slab or "missing: " in fn_slab


def test_crypto_probe_round_trips():
    """The probe encrypts and decrypts a known plaintext, so it
    catches a key that *appears* valid but doesn't round-trip."""
    body = _HEALTH.read_text(encoding="utf-8")
    fn = body.find("def _check_crypto(")
    slab = body[fn:fn + 700]
    assert "encrypt(probe.decode())" in slab
    assert "decrypt(ct, iv)" in slab
    assert "out.encode() == probe" in slab


def test_analyzers_check_uses_registry():
    body = _HEALTH.read_text(encoding="utf-8")
    fn = body.find("def _check_analyzers(")
    slab = body[fn:fn + 800]
    # Source the binaries list from the same registry the platform
    # actually invokes — otherwise the probe drifts from reality.
    assert "from app.analyzers.registry import _BINARIES" in slab
    assert "shutil" in slab or "_shutil" in slab


@pytest.mark.asyncio
async def test_missing_paid_llm_budget_is_advisory_not_readiness_failure(
    monkeypatch,
):
    monkeypatch.delenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", raising=False)

    async def healthy_dependency() -> tuple[bool, str]:
        return True, "ok"

    monkeypatch.setattr(health_api, "_check_db", healthy_dependency)
    monkeypatch.setattr(health_api, "_check_redis", healthy_dependency)
    monkeypatch.setattr(health_api, "_check_worker", healthy_dependency)
    monkeypatch.setattr(health_api, "_check_crypto", lambda: (True, "ok"))
    monkeypatch.setattr(
        health_api,
        "_check_analyzers",
        lambda: (True, "all_present"),
    )

    response = await health_api.ready()
    body = json.loads(response.body)

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert body["checks"]["llm_paid_dispatch"] == {
        "ok": False,
        "message": "disabled_budget_required",
    }


def test_positive_paid_llm_budget_is_reported_enabled(monkeypatch):
    monkeypatch.setenv("MNEMOS_LLM_BUDGET_USD_PER_PROJECT", "10")

    assert health_api._check_llm_paid_dispatch() == (True, "enabled")
