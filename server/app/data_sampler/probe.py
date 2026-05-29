"""Read-only credential probe for ProjectDB registration (spec §2.5, §2.8).

The platform does not open DB connections directly — only analyzer
subprocesses do (spec §7.3 / §7.4 isolation). The probe therefore runs
as a short-lived analyzer invocation with the new ``db_probe`` verb;
the analyzer reports back, in metadata terms wherever possible
(``HAS_TABLE_PRIVILEGE``, ``user_tab_privs``, ``fn_my_permissions``…),
whether the supplied credential could write anything.

We refuse to leave a write attempt in the live redo log: the contract
forbids the analyzer from issuing ``INSERT``/``UPDATE``/``DELETE`` even
into a "throwaway" row. If metadata cannot rule out write access the
analyzer may consult an operator-provisioned isolated schema, but never
the user's working tables.

Result shape follows §2.3 — every observation carries the four-tuple
``(value, observed_at, source, confidence)`` so the audit log can tell
"the DB itself told us this" from "we inferred this".
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# How long we wait for an analyzer to answer before giving up. The
# analyzer is allowed to be slow (Oracle SQL*Net handshake can take a
# few hundred milliseconds) but a hung connection must not block the
# registration request.
PROBE_TIMEOUT_SEC = 15

# How long a successful probe is considered current. After this window
# the orchestrator re-probes before kicking off a new analysis run.
PROBE_CACHE_TTL_SEC = 24 * 60 * 60

Certainty = Literal["verified", "asserted", "inferred"]
ProbeStatus = Literal["pass", "fail_rw", "fail_connect", "deferred"]


@dataclass
class CertaintyObservation:
    """A single fact with its provenance — §2.3 four-tuple."""

    value: Any
    observed_at: datetime
    source: str
    confidence: Certainty

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "observed_at": self.observed_at.isoformat(),
            "source": self.source,
            "confidence": self.confidence,
        }


@dataclass
class ProbeResult:
    """Outcome of probing a ProjectDB binding.

    ``status`` is the executive verdict the API uses. ``observations``
    is the audit trail of the individual facts that produced it.
    """

    status: ProbeStatus
    connect_ok: bool | None
    read_ok: bool | None
    write_blocked: bool | None
    grants_summary: dict[str, list[str]]
    observations: list[CertaintyObservation] = field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None

    def as_jsonable(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "connect_ok": self.connect_ok,
            "read_ok": self.read_ok,
            "write_blocked": self.write_blocked,
            "grants_summary": self.grants_summary,
            "observations": [o.as_jsonable() for o in self.observations],
            "latency_ms": self.latency_ms,
            "error": self.error,
        }

    def is_acceptable(self) -> bool:
        """True iff registration may proceed.

        A ``deferred`` probe (analyzer too old to support ``db_probe``)
        is *not* acceptable for new registrations — operators must
        upgrade the analyzer or supply credentials whose read-only
        nature has already been verified out of band.
        """
        return (
            self.status == "pass"
            and self.connect_ok is True
            and self.read_ok is True
            and self.write_blocked is True
        )


def parse_analyzer_response(payload: dict[str, Any], *, source: str) -> ProbeResult:
    """Translate an analyzer's ``db_probe`` JSON envelope into a ProbeResult.

    Analyzers emit a single JSON object on stdout in this shape::

        {
          "status": "pass" | "fail_rw" | "fail_connect",
          "connect_ok": bool,
          "read_ok": bool,
          "write_blocked": bool,
          "grants": {"<schema>": ["SELECT", "INSERT", ...], ...},
          "facts": [
              {"value": ..., "source": "pg_has_role", "confidence": "verified"},
              ...
          ]
        }

    Older analyzers that don't implement the verb exit with code 2 and
    print ``{"error": "unsupported_verb"}`` — the caller maps this to
    ``status='deferred'`` before calling us.
    """
    now = datetime.now(tz=timezone.utc)
    raw_status = payload.get("status", "fail_connect")
    if raw_status not in ("pass", "fail_rw", "fail_connect"):
        raw_status = "fail_connect"

    observations: list[CertaintyObservation] = []
    for fact in payload.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        confidence = fact.get("confidence", "asserted")
        if confidence not in ("verified", "asserted", "inferred"):
            confidence = "asserted"
        observations.append(
            CertaintyObservation(
                value=fact.get("value"),
                observed_at=now,
                source=fact.get("source", source),
                confidence=confidence,
            )
        )

    grants = payload.get("grants") or {}
    if not isinstance(grants, dict):
        grants = {}

    return ProbeResult(
        status=raw_status,
        connect_ok=bool(payload.get("connect_ok")) if "connect_ok" in payload else None,
        read_ok=bool(payload.get("read_ok")) if "read_ok" in payload else None,
        write_blocked=(
            bool(payload.get("write_blocked")) if "write_blocked" in payload else None
        ),
        grants_summary={k: list(v) for k, v in grants.items() if isinstance(v, list)},
        observations=observations,
        latency_ms=int(payload.get("latency_ms", 0)),
    )


def deferred_result(reason: str) -> ProbeResult:
    """Sentinel when the analyzer doesn't (yet) implement ``db_probe``."""
    return ProbeResult(
        status="deferred",
        connect_ok=None,
        read_ok=None,
        write_blocked=None,
        grants_summary={},
        observations=[],
        latency_ms=0,
        error=reason,
    )


async def probe_via_analyzer(
    kind: str,
    conn_ref: str,
    *,
    binary_for: Any | None = None,
    timeout: float = PROBE_TIMEOUT_SEC,
) -> ProbeResult:
    """Spawn the matching analyzer with ``db_probe`` and parse its output.

    ``binary_for`` defaults to ``app.analyzers.registry.binary_for`` but
    is parameterised so tests can inject a stub without monkey-patching.
    """
    if binary_for is None:
        from app.analyzers.registry import binary_for as _resolver

        binary_for = _resolver

    binary = binary_for(kind)
    if binary is None:
        return deferred_result(f"no analyzer for kind={kind!r}")

    started = datetime.now(tz=timezone.utc)
    # Reuse the runner's allowlist so the probe subprocess sees the same
    # carefully-curated env as a normal analyzer run (spec §2.8;
    # Team B 2nd-round finding A).
    from app.analyzers.runner import _build_env

    proc_env = _build_env({"MNEMOS_DB_CONN": conn_ref})
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "db_probe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proc_env,
        )
    except FileNotFoundError:
        return deferred_result(f"analyzer binary not found: {binary}")

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return ProbeResult(
            status="fail_connect",
            connect_ok=False,
            read_ok=None,
            write_blocked=None,
            grants_summary={},
            error=f"analyzer timed out after {timeout}s",
            latency_ms=int(timeout * 1000),
        )

    elapsed_ms = int((datetime.now(tz=timezone.utc) - started).total_seconds() * 1000)

    if proc.returncode == 2:
        # Convention: exit-code 2 means "I don't know this verb."
        return deferred_result("analyzer does not implement db_probe verb")

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return ProbeResult(
            status="fail_connect",
            connect_ok=False,
            read_ok=None,
            write_blocked=None,
            grants_summary={},
            error=f"invalid analyzer payload: {exc}; stderr={stderr[:200]!r}",
            latency_ms=elapsed_ms,
        )

    if not isinstance(payload, dict):
        return ProbeResult(
            status="fail_connect",
            connect_ok=False,
            read_ok=None,
            write_blocked=None,
            grants_summary={},
            error="analyzer payload must be a JSON object",
            latency_ms=elapsed_ms,
        )

    result = parse_analyzer_response(payload, source=f"analyzer:{binary}")
    result.latency_ms = elapsed_ms
    return result


__all__ = [
    "ProbeResult",
    "ProbeStatus",
    "Certainty",
    "CertaintyObservation",
    "PROBE_TIMEOUT_SEC",
    "PROBE_CACHE_TTL_SEC",
    "parse_analyzer_response",
    "deferred_result",
    "probe_via_analyzer",
    # for tests
    "dataclasses",
]
