"""Unit coverage for the read-only ProjectDB probe (spec §2.5, §2.8).

The platform never opens a connection to a project DB directly; the
probe runs as a short analyzer subprocess. These tests pin down:

* the §2.3 certainty four-tuple in every observation,
* the verdict mapping (``pass`` / ``fail_rw`` / ``fail_connect`` /
  ``deferred``),
* the ``is_acceptable()`` gate the API uses to refuse RW credentials,
* graceful handling when the analyzer is missing or speaks an older
  protocol.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.data_sampler.probe import (
    ProbeResult,
    deferred_result,
    parse_analyzer_response,
    probe_via_analyzer,
)


def test_parse_pass_records_certainty_four_tuple():
    """Every fact must carry value + observed_at + source + confidence."""
    payload = {
        "status": "pass",
        "connect_ok": True,
        "read_ok": True,
        "write_blocked": True,
        "grants": {"public": ["SELECT"]},
        "facts": [
            {
                "value": "INSERT denied on every schema",
                "source": "pg_has_table_privilege",
                "confidence": "verified",
            }
        ],
        "latency_ms": 12,
    }
    result = parse_analyzer_response(payload, source="analyzer:ggoss-sql-mssql")
    assert result.is_acceptable()
    assert result.status == "pass"
    assert result.grants_summary == {"public": ["SELECT"]}
    obs = result.observations
    assert len(obs) == 1
    assert obs[0].confidence == "verified"
    assert obs[0].source == "pg_has_table_privilege"
    assert isinstance(obs[0].observed_at, datetime)
    assert obs[0].observed_at.tzinfo is timezone.utc


def test_parse_fail_rw_blocks_registration():
    """write_blocked=False must make is_acceptable() return False."""
    payload = {
        "status": "fail_rw",
        "connect_ok": True,
        "read_ok": True,
        "write_blocked": False,
        "grants": {"public": ["SELECT", "INSERT", "UPDATE"]},
        "facts": [],
    }
    result = parse_analyzer_response(payload, source="t")
    assert not result.is_acceptable()
    assert result.write_blocked is False


def test_parse_fail_connect_when_status_unknown():
    """An unknown status string must default to fail_connect, not pass."""
    payload = {"status": "weird_new_status", "connect_ok": False}
    result = parse_analyzer_response(payload, source="t")
    assert result.status == "fail_connect"
    assert not result.is_acceptable()


def test_parse_coerces_invalid_confidence():
    """Unknown confidence values fall back to 'asserted' (fail-safe)."""
    payload = {
        "status": "pass",
        "connect_ok": True,
        "read_ok": True,
        "write_blocked": True,
        "grants": {},
        "facts": [{"value": "x", "source": "y", "confidence": "totally-bogus"}],
    }
    result = parse_analyzer_response(payload, source="t")
    assert result.observations[0].confidence == "asserted"


def test_deferred_result_is_not_acceptable():
    """A deferred probe is never enough to admit a registration."""
    r = deferred_result("analyzer missing")
    assert r.status == "deferred"
    assert r.write_blocked is None
    assert not r.is_acceptable()


def test_is_acceptable_requires_every_flag():
    """All three flags must be True for the probe to admit registration."""
    base = dict(
        status="pass",
        grants_summary={},
        observations=[],
        latency_ms=0,
    )
    assert ProbeResult(connect_ok=True, read_ok=True, write_blocked=True, **base).is_acceptable()
    assert not ProbeResult(connect_ok=False, read_ok=True, write_blocked=True, **base).is_acceptable()
    assert not ProbeResult(connect_ok=True, read_ok=False, write_blocked=True, **base).is_acceptable()
    assert not ProbeResult(connect_ok=True, read_ok=True, write_blocked=False, **base).is_acceptable()


@pytest.mark.asyncio
async def test_probe_via_analyzer_defers_when_binary_missing():
    """No analyzer for the kind → deferred, never an exception."""
    result = await probe_via_analyzer(
        "unknown_kind", "conn://x", binary_for=lambda kind: None
    )
    assert result.status == "deferred"
    assert "no analyzer" in (result.error or "")


def test_jsonable_roundtrip_keeps_certainty():
    """The audit payload must preserve the certainty four-tuple."""
    payload = {
        "status": "pass",
        "connect_ok": True,
        "read_ok": True,
        "write_blocked": True,
        "grants": {"app": ["SELECT"]},
        "facts": [{"value": 1, "source": "s", "confidence": "verified"}],
    }
    result = parse_analyzer_response(payload, source="t")
    j = result.as_jsonable()
    assert j["status"] == "pass"
    assert j["observations"][0]["confidence"] == "verified"
    assert j["observations"][0]["source"] == "s"
    assert "observed_at" in j["observations"][0]
