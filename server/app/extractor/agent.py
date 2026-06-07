"""L1-L3 summarizer that wraps the Anthropic SDK.

Deliberately minimal in Phase 1:
- Builds prompts from adjacent graph rows (not whole files — spec §2.6).
- Produces the structured schema required by §10.3 (summary/detailed/claims/
  open_questions).
- Runs synchronously per target; the caller schedules invocations per L1-L3
  wave to keep context windows tight.

Backend priority (highest to lowest):
1. ``ANTHROPIC_API_KEY`` set + ``anthropic`` SDK importable → direct API
2. ``claude_agent_sdk`` importable + not opted out → Claude Code subscription
   (PR-125 — Mnemos running inside Claude Code uses the operator's existing
   subscription instead of requiring a separate API key)
3. Deterministic stub — pipeline-safe placeholder for CI / air-gapped

Failure-mode visibility (PR-138 — audit follow-up)
-------------------------------------------------
Pre-PR-138 every silent fallback collapsed into ``model_used="stub"``,
so an operator inspecting a Summary row could not tell "no LLM was
configured" from "Claude SDK timed out after 60s". That made debug
expensive and the §2 "every L1~L3 truthfully labelled" promise
unverifiable. The stub path now records ``model_used`` _and_
``fallback_reason`` (an enum string), plus increments a Prometheus
counter ``mnemos_llm_fallback_total{from,reason}`` so silent timeouts
finally show up on the operations dashboard.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# PR-138 — distinguishable failure-mode labels. Every silent fallback
# stamps one of these onto ExtractorResult.fallback_reason so an
# operator inspecting summaries.model_used + the Prometheus counter
# can tell timeouts from missing-key from JSON-parse-error.
FALLBACK_NO_BACKEND = "no_backend"
FALLBACK_ANTHROPIC_IMPORT = "anthropic_import_error"
FALLBACK_ANTHROPIC_HTTP = "anthropic_http_error"
FALLBACK_ANTHROPIC_JSON = "anthropic_json_decode"
FALLBACK_AGENT_SDK_TIMEOUT = "agent_sdk_timeout"
FALLBACK_AGENT_SDK_ERROR = "agent_sdk_error"
FALLBACK_AGENT_SDK_JSON = "agent_sdk_json_decode"


def _record_fallback(from_backend: str, reason: str) -> None:
    """Tick the Prometheus counter so silent stub fallbacks become
    visible on the ops dashboard. Soft-fails if the metrics module
    isn't importable (CI / unit-test runs without app.obs)."""
    try:
        from app.obs.metrics import llm_fallback_total
        llm_fallback_total.labels(
            **{"from": from_backend, "reason": reason}
        ).inc()
    except Exception:  # noqa: BLE001
        pass


@dataclass
class ExtractorResult:
    summary: str
    detailed: str
    claims: list[dict[str, Any]]
    open_questions: list[str]
    model_used: str
    tokens_used: int | None
    # PR-138 — when the result came from the stub path, this field
    # explains why so the operator can act (rotate key, install SDK,
    # tune timeout). Empty string on the happy path.
    fallback_reason: str = ""


class Extractor:
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.model = model
        self._client = None
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")
        # Tracks why this Extractor instance is about to fall back so
        # _stub() can stamp the right reason without weaving an extra
        # arg through every callsite.
        self._pending_reason = FALLBACK_NO_BACKEND
        self._pending_from = "none"

    async def summarize(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult:
        """Summarise ``target_id`` from a bounded list of evidence rows."""
        # Reset per-call so a previous summarize()'s reason doesn't
        # leak into this one.
        self._pending_reason = FALLBACK_NO_BACKEND
        self._pending_from = "none"

        # Path 1: direct Anthropic SDK if operator provided a key.
        if self._api_key:
            result = await self._summarize_via_anthropic_sdk(level, target_id, evidence)
            if result is not None:
                return result
            # SDK path returned None (ImportError / JSON fail) — try path 2.

        # Path 2 (PR-125): Claude Agent SDK uses the Claude Code subscription.
        # No API key needed. Activates whenever Mnemos runs in an env that
        # has the SDK installed (e.g. alongside Claude Code).
        from app.extractor.agent_sdk import (
            is_agent_sdk_available,
            summarize_via_agent_sdk,
        )
        if is_agent_sdk_available():
            try:
                parsed = await summarize_via_agent_sdk(
                    model=self.model,
                    level=level,
                    target_id=target_id,
                    evidence=evidence,
                )
            except TimeoutError as exc:
                log.warning("agent_sdk timeout: %s", exc)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_TIMEOUT
                parsed = None
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_sdk path failed: %s: %s",
                            exc.__class__.__name__, exc)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_ERROR
                parsed = None
            if parsed is None and self._pending_reason == FALLBACK_NO_BACKEND:
                # is_agent_sdk_available() was True, summarize_via_
                # agent_sdk() returned None without raising — that's
                # the "parser saw bad JSON" path.
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_JSON
            if parsed is not None:
                return ExtractorResult(
                    summary=parsed.get("summary", ""),
                    detailed=parsed.get("detailed", ""),
                    claims=parsed.get("claims", []) or [],
                    open_questions=parsed.get("open_questions", []) or [],
                    model_used=f"{self.model}:agent_sdk",
                    tokens_used=None,
                )

        # Path 3: stub fallback — record the reason that brought us
        # here so the operator can tell apart "no backend configured"
        # from "timed out".
        if self._pending_reason != FALLBACK_NO_BACKEND:
            _record_fallback(self._pending_from, self._pending_reason)
        return self._stub(level, target_id, evidence, self._pending_reason)

    async def _summarize_via_anthropic_sdk(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult | None:
        """Direct Anthropic SDK call. Returns None on import/parse
        failure so the caller can try the next backend."""
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError:
            log.warning("anthropic SDK missing; trying agent_sdk")
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_IMPORT
            return None

        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._api_key)

        prompt = self._prompt(level, target_id, evidence)
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=(
                    "You are Mnemos's hierarchical summariser. Produce JSON "
                    "that matches the schema: {summary, detailed, claims: "
                    "[{claim, evidence: [{kind, edge_id|node_id, certainty}]}], "
                    "open_questions}. Never invent evidence IDs."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("anthropic call failed: %s", exc)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_HTTP
            return None

        text = response.content[0].text if response.content else "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("anthropic: non-JSON response")
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_JSON
            return None

        return ExtractorResult(
            summary=parsed.get("summary", ""),
            detailed=parsed.get("detailed", ""),
            claims=parsed.get("claims", []),
            open_questions=parsed.get("open_questions", []),
            model_used=self.model,
            tokens_used=getattr(response.usage, "output_tokens", None),
        )

    @staticmethod
    def _prompt(level: int, target_id: str, evidence: list[dict[str, Any]]) -> str:
        return (
            f"L{level} summarisation target: {target_id}\n"
            "Graph evidence (truncated):\n"
            f"{json.dumps(evidence, default=str)[:6000]}\n\n"
            "Return strict JSON."
        )

    @staticmethod
    def _stub(
        level: int, target_id: str, evidence: list[dict[str, Any]],
        reason: str = FALLBACK_NO_BACKEND,
    ) -> ExtractorResult:
        # PR-138 — distinguishable model_used per fallback reason so
        # ``SELECT model_used FROM summaries`` answers "why did this
        # row miss the LLM?" without grepping logs.
        model_label = "stub" if reason == FALLBACK_NO_BACKEND \
            else f"stub:{reason}"
        summary = (
            f"[{model_label} L{level}] {target_id} summarised from "
            f"{len(evidence)} evidence rows."
        )
        claims = [
            {
                "claim": f"{target_id} is referenced in graph",
                "evidence": [
                    {"kind": "node", "node_id": target_id, "certainty": "asserted"}
                ],
            }
        ]
        return ExtractorResult(
            summary=summary,
            detailed=summary,
            claims=claims,
            open_questions=[],
            model_used=model_label,
            tokens_used=0,
            fallback_reason=reason,
        )
