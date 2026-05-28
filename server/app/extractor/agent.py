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
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class ExtractorResult:
    summary: str
    detailed: str
    claims: list[dict[str, Any]]
    open_questions: list[str]
    model_used: str
    tokens_used: int | None


class Extractor:
    def __init__(self, model: str = "claude-sonnet-4-6") -> None:
        self.model = model
        self._client = None
        self._api_key = os.environ.get("ANTHROPIC_API_KEY")

    async def summarize(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult:
        """Summarise ``target_id`` from a bounded list of evidence rows."""
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
            except Exception as exc:  # noqa: BLE001
                log.warning("agent_sdk path failed: %s: %s",
                            exc.__class__.__name__, exc)
                parsed = None
            if parsed is not None:
                return ExtractorResult(
                    summary=parsed.get("summary", ""),
                    detailed=parsed.get("detailed", ""),
                    claims=parsed.get("claims", []) or [],
                    open_questions=parsed.get("open_questions", []) or [],
                    model_used=f"{self.model}:agent_sdk",
                    tokens_used=None,
                )

        # Path 3: stub fallback.
        return self._stub(level, target_id, evidence)

    async def _summarize_via_anthropic_sdk(
        self, level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult | None:
        """Direct Anthropic SDK call. Returns None on import/parse
        failure so the caller can try the next backend."""
        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError:
            log.warning("anthropic SDK missing; trying agent_sdk")
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
            return None

        text = response.content[0].text if response.content else "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("anthropic: non-JSON response")
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
        level: int, target_id: str, evidence: list[dict[str, Any]]
    ) -> ExtractorResult:
        summary = (
            f"[stub L{level}] {target_id} summarised from {len(evidence)} "
            "evidence rows."
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
            model_used="stub",
            tokens_used=0,
        )
