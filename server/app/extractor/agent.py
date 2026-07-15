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

import logging
import os
from dataclasses import dataclass
from typing import Any

from app.extractor.packing import (
    EvidencePromptTooLarge,
    MAX_EVIDENCE_PROMPT_CHARS,
    serialize_evidence,
)
from app.extractor.schema import (
    ExtractorSchemaError,
    normalize_extractor_payload,
    parse_json_object,
)

log = logging.getLogger(__name__)

# PR-138 — distinguishable failure-mode labels. Every silent fallback
# stamps one of these onto ExtractorResult.fallback_reason so an
# operator inspecting summaries.model_used + the Prometheus counter
# can tell timeouts from missing-key from JSON-parse-error.
FALLBACK_NO_BACKEND = "no_backend"
FALLBACK_ANTHROPIC_IMPORT = "anthropic_import_error"
FALLBACK_ANTHROPIC_HTTP = "anthropic_http_error"
FALLBACK_ANTHROPIC_JSON = "anthropic_json_decode"
FALLBACK_ANTHROPIC_SCHEMA = "anthropic_schema_invalid"
FALLBACK_AGENT_SDK_TIMEOUT = "agent_sdk_timeout"
FALLBACK_AGENT_SDK_ERROR = "agent_sdk_error"
FALLBACK_AGENT_SDK_JSON = "agent_sdk_json_decode"
FALLBACK_EVIDENCE_BUDGET = "evidence_budget_exceeded"


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
        self._pending_tokens: int | None = None

    async def summarize(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult:
        """Summarise ``target_id`` from a bounded list of evidence rows."""
        # Reset per-call so a previous summarize()'s reason doesn't
        # leak into this one.
        self._pending_reason = FALLBACK_NO_BACKEND
        self._pending_from = "none"
        self._pending_tokens = None

        # All provider paths share one complete-JSON evidence contract.  A
        # caller that bypasses the runner's packer still fails closed before
        # importing or invoking any paid backend.
        try:
            serialize_evidence(evidence)
        except EvidencePromptTooLarge:
            self._pending_from = "extractor"
            self._pending_reason = FALLBACK_EVIDENCE_BUDGET
            _record_fallback(self._pending_from, self._pending_reason)
            return self._stub(
                level,
                target_id,
                [],
                FALLBACK_EVIDENCE_BUDGET,
            )

        # Path 1: direct Anthropic SDK if operator provided a key.
        if self._api_key:
            result = await self._summarize_via_anthropic_sdk(level, target_id, evidence)
            if result is not None:
                return result
            # Once a remote API request was attempted, never spend again on a
            # second backend for the same target.  Import failure happens
            # before a request and may safely try the local subscription;
            # HTTP/JSON/schema failures become an explicit stub.
            if self._pending_reason != FALLBACK_ANTHROPIC_IMPORT:
                _record_fallback(self._pending_from, self._pending_reason)
                stub = self._stub(
                    level, target_id, evidence, self._pending_reason
                )
                stub.tokens_used = self._pending_tokens
                return stub

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
                log.warning("agent_sdk timeout: %s", type(exc).__name__)
                self._pending_from = "agent_sdk"
                self._pending_reason = FALLBACK_AGENT_SDK_TIMEOUT
                parsed = None
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_sdk path failed: %s", type(exc).__name__)
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
                # ``summarize_via_agent_sdk`` already returns this canonical
                # shape.  Re-normalizing is intentionally idempotent and
                # protects this public boundary from alternate adapters or
                # test doubles returning an unchecked dict.
                try:
                    canonical = normalize_extractor_payload(parsed)
                except ExtractorSchemaError as exc:
                    log.warning("agent_sdk schema rejected: %s", exc)
                    self._pending_from = "agent_sdk"
                    self._pending_reason = FALLBACK_AGENT_SDK_JSON
                    canonical = None
                if canonical is None:
                    parsed = None
                else:
                    parsed = canonical
            if parsed is not None:
                return ExtractorResult(
                    summary=parsed["summary"],
                    detailed=parsed["detailed"],
                    claims=parsed["claims"],
                    open_questions=parsed["open_questions"],
                    model_used=f"{self.model}:agent_sdk",
                    tokens_used=None,
                )

        # Path 3: stub fallback — record the reason that brought us
        # here so the operator can tell apart "no backend configured"
        # from "timed out".
        if self._pending_reason != FALLBACK_NO_BACKEND:
            _record_fallback(self._pending_from, self._pending_reason)
        stub = self._stub(level, target_id, evidence, self._pending_reason)
        if self._pending_reason != FALLBACK_NO_BACKEND:
            stub.tokens_used = self._pending_tokens
        return stub

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
            self._client = AsyncAnthropic(
                api_key=self._api_key,
                timeout=60.0,
                max_retries=0,
            )

        prompt = self._prompt(level, target_id, evidence)
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=(
                    "You are Mnemos's hierarchical summariser. Produce JSON "
                    "that matches the schema: {summary, detailed, claims: "
                    "[{claim, evidence: [{kind, edge_id|node_id, certainty}]}], "
                    "open_questions}. Never invent evidence IDs. Only "
                    "top-level node_id/edge_id fields in supplied evidence "
                    "rows are citable; IDs mentioned inside prose or preview "
                    "text are not evidence."
                ),
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001
            # Provider errors may embed request bodies or source snippets.
            log.warning("anthropic call failed: %s", type(exc).__name__)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_HTTP
            return None

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        self._pending_tokens = input_tokens + output_tokens
        # Anthropic returns tagged blocks. Tool/thinking/image blocks do not
        # expose text; ignore them and reject an envelope with no text rather
        # than raising or stringifying provider metadata.
        content = getattr(response, "content", None)
        text_blocks: list[str] = []
        if isinstance(content, (list, tuple)):
            for block in content:
                block_text = getattr(block, "text", None)
                block_type = getattr(block, "type", None)
                if (
                    isinstance(block_text, str)
                    and (
                        block_type == "text"
                        or not isinstance(block_type, str)
                    )
                ):
                    text_blocks.append(block_text)
        text = "\n".join(text_blocks)
        try:
            parsed = parse_json_object(text)
        except ExtractorSchemaError as exc:
            log.warning("anthropic: non-JSON response")
            log.debug("anthropic parse diagnostic: %s", exc)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_JSON
            return None

        try:
            parsed = normalize_extractor_payload(parsed)
        except ExtractorSchemaError as exc:
            # Do not store or partially salvage malformed model output.  The
            # diagnostic reports paths and sizes only; raw source/model text
            # is deliberately absent from logs.
            log.warning("anthropic schema rejected: %s", exc)
            self._pending_from = "anthropic"
            self._pending_reason = FALLBACK_ANTHROPIC_SCHEMA
            return None

        return ExtractorResult(
            summary=parsed["summary"],
            detailed=parsed["detailed"],
            claims=parsed["claims"],
            open_questions=parsed["open_questions"],
            model_used=self.model,
            tokens_used=self._pending_tokens,
        )

    @staticmethod
    def _prompt(level: int, target_id: str, evidence: list[dict[str, Any]]) -> str:
        evidence_json = serialize_evidence(
            evidence,
            max_chars=MAX_EVIDENCE_PROMPT_CHARS,
        )
        return (
            f"L{level} summarisation target: {target_id}\n"
            "Graph evidence (complete JSON):\n"
            f"{evidence_json}\n\n"
            "Cite only top-level node_id/edge_id fields from those rows. "
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
