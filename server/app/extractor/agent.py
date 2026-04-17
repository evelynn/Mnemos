"""L1-L3 summarizer that wraps the Anthropic SDK.

Deliberately minimal in Phase 1:
- Builds prompts from adjacent graph rows (not whole files — spec §2.6).
- Produces the structured schema required by §10.3 (summary/detailed/claims/
  open_questions).
- Runs synchronously per target; the caller schedules invocations per L1-L3
  wave to keep context windows tight.

When ``ANTHROPIC_API_KEY`` isn't set, the extractor falls back to a
deterministic stub so the rest of the pipeline can be exercised in CI.
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
        if not self._api_key:
            return self._stub(level, target_id, evidence)

        try:
            from anthropic import AsyncAnthropic  # type: ignore
        except ImportError:
            log.warning("anthropic SDK missing; using stub summariser")
            return self._stub(level, target_id, evidence)

        if self._client is None:
            self._client = AsyncAnthropic(api_key=self._api_key)

        prompt = self._prompt(level, target_id, evidence)
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=(
                "You are Mnemos's hierarchical summariser. Produce JSON that "
                "matches the schema: {summary, detailed, claims: [{claim, "
                "evidence: [{kind, edge_id|node_id, certainty}]}], "
                "open_questions}. Never invent evidence IDs."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text if response.content else "{}"
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            log.warning("extractor: non-JSON response; stubbing")
            return self._stub(level, target_id, evidence)

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
