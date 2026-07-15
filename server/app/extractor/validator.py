"""Claim validator (spec §10.4).

Model-authored claim text is optional narration.  Evidence identity and
certainty are deterministic graph facts: references must be current, belong
to the project, and (when a producer scope is supplied) occur in the exact
evidence pack shown to the model.  Accepted certainty is always copied from
the database, never trusted from model output.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.extractor.schema import (
    MAX_CLAIMS,
    ExtractorSchemaError,
    normalize_extractor_payload,
)
from app.extractor.agent_flow import (
    FLOW_LEVEL,
    FlowContractError,
    FlowSummaryContent,
    normalize_flow_summary_content,
)
from app.graph_overlays import effective_certainty
from app.models.graph import Edge, Node
from app.models.overlays import GraphEdgeHumanOverlay, GraphNodeHumanOverlay

_MAX_SUMMARIES_PER_REVALIDATION = 50
# Keep every ``IN`` lookup below SQLite's historical 999-variable ceiling
# (one additional bind is used by ``project_id``).  PostgreSQL benefits too:
# a consumer page at its hard cap can legitimately contain tens of
# thousands of distinct evidence IDs, which must not become one giant bind
# list even though the outer response itself is bounded.
_REFERENCE_QUERY_BATCH = 900
_SUMMARY_SLOT_KEY = "__mnemos_summary_slot"
_CLAIM_SLOT_KEY = "__mnemos_claim_slot"


def _reference_batches[T](values: set[T]) -> list[list[T]]:
    ordered = sorted(values, key=str)
    return [
        ordered[offset : offset + _REFERENCE_QUERY_BATCH]
        for offset in range(0, len(ordered), _REFERENCE_QUERY_BATCH)
    ]


async def validate_claims(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    claims: Any,
    provided_evidence: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Partition claims by shape and current-project graph grounding.

    Malformed model output is data, not an application exception.  Invalid
    claim/evidence shapes are rejected without touching the database.  Valid
    references are then checked in bounded node/edge batches, with every
    lookup scoped to ``project_id``.  ``provided_evidence=None`` keeps the
    DB-only behaviour used by deterministic safety reviewers; extractor callers
    pass their concrete pack to prevent citations outside model input.
    """

    accepted: list[dict[str, Any]] = []
    rejected: list[Any] = []
    if not isinstance(claims, list):
        return accepted, [claims]

    allowed_nodes: set[str] | None = None
    allowed_edges: set[uuid.UUID] | None = None
    if provided_evidence is not None:
        allowed_nodes = set()
        allowed_edges = set()
        for supplied in provided_evidence:
            if not isinstance(supplied, Mapping):
                continue
            if supplied.get("kind") == "node":
                supplied_id = supplied.get("node_id")
                if isinstance(supplied_id, str) and supplied_id.strip():
                    allowed_nodes.add(supplied_id)
            elif supplied.get("kind") == "edge":
                try:
                    allowed_edges.add(uuid.UUID(str(supplied.get("edge_id"))))
                except (ValueError, TypeError, AttributeError):
                    continue

    # Keep references beside copies of their evidence items.  The producer
    # payload is never mutated when DB certainty replaces the provisional
    # model value.
    structurally_valid: list[
        tuple[
            dict[str, Any],
            list[tuple[str, str | uuid.UUID, dict[str, Any]]],
        ]
    ] = []
    node_ids: set[str] = set()
    edge_ids: set[uuid.UUID] = set()

    for claim in claims:
        if not isinstance(claim, Mapping):
            rejected.append(claim)
            continue
        claim_text = claim.get("claim")
        evidence = claim.get("evidence")
        if (
            not isinstance(claim_text, str)
            or not claim_text.strip()
            or not isinstance(evidence, list)
            or not evidence
        ):
            rejected.append(claim)
            continue

        references: list[
            tuple[str, str | uuid.UUID, dict[str, Any]]
        ] = []
        malformed = False
        for ev in evidence:
            if not isinstance(ev, Mapping):
                malformed = True
                break
            kind = ev.get("kind")
            certainty = ev.get("certainty")
            if certainty not in {"verified", "asserted", "inferred"}:
                malformed = True
                break
            if kind == "node":
                node_id = ev.get("node_id")
                if (
                    not isinstance(node_id, str)
                    or not node_id.strip()
                    or node_id != node_id.strip()
                    or "edge_id" in ev
                ):
                    malformed = True
                    break
                if allowed_nodes is not None and node_id not in allowed_nodes:
                    malformed = True
                    break
                references.append(("node", node_id, dict(ev)))
                node_ids.add(node_id)
            elif kind == "edge":
                raw = ev.get("edge_id")
                try:
                    edge_id = uuid.UUID(str(raw))
                except (ValueError, TypeError):
                    malformed = True
                    break
                if "node_id" in ev:
                    malformed = True
                    break
                if allowed_edges is not None and edge_id not in allowed_edges:
                    malformed = True
                    break
                references.append(("edge", edge_id, dict(ev)))
                edge_ids.add(edge_id)
            else:
                malformed = True
                break
        if malformed or not references:
            rejected.append(claim)
            continue
        structurally_valid.append((dict(claim), references))

    node_certainties: dict[str, str] = {}
    for node_batch in _reference_batches(node_ids):
        rows = await session.execute(
            select(
                Node.id,
                Node.certainty,
                GraphNodeHumanOverlay.action,
            )
            .outerjoin(
                GraphNodeHumanOverlay,
                and_(
                    GraphNodeHumanOverlay.project_id == Node.project_id,
                    GraphNodeHumanOverlay.node_id == Node.id,
                ),
            )
            .where(
                Node.project_id == project_id,
                Node.id.in_(node_batch),
                Node.valid_to.is_(None),
            )
        )
        for row in rows.all():
            node_id, source_certainty = row[0], row[1]
            action = row[2] if len(row) > 2 else None
            node_certainties[node_id] = effective_certainty(
                source_certainty, action
            )

    edge_certainties: dict[uuid.UUID, str] = {}
    for edge_batch in _reference_batches(edge_ids):
        rows = await session.execute(
            select(
                Edge.id,
                Edge.certainty,
                GraphEdgeHumanOverlay.action,
            )
            .outerjoin(
                GraphEdgeHumanOverlay,
                and_(
                    GraphEdgeHumanOverlay.project_id == Edge.project_id,
                    GraphEdgeHumanOverlay.source_id == Edge.source_id,
                    GraphEdgeHumanOverlay.target_id == Edge.target_id,
                    GraphEdgeHumanOverlay.kind == Edge.kind,
                ),
            )
            .where(
                Edge.project_id == project_id,
                Edge.id.in_(edge_batch),
                Edge.valid_to.is_(None),
            )
        )
        for row in rows.all():
            edge_id, source_certainty = row[0], row[1]
            action = row[2] if len(row) > 2 else None
            edge_certainties[edge_id] = effective_certainty(
                source_certainty, action
            )

    for claim, references in structurally_valid:
        normalized_evidence: list[dict[str, Any]] = []
        for kind, reference, original_evidence in references:
            db_certainty = (
                node_certainties.get(reference)
                if kind == "node"
                else edge_certainties.get(reference)
            )
            if db_certainty not in {"verified", "asserted", "inferred"}:
                break
            normalized = dict(original_evidence)
            normalized["certainty"] = db_certainty
            normalized_evidence.append(normalized)
        else:
            normalized_claim = dict(claim)
            normalized_claim["evidence"] = normalized_evidence
            accepted.append(normalized_claim)
            continue
        rejected.append(claim)
    return accepted, rejected


@dataclass(frozen=True)
class SummaryClaimView:
    """Validated current-consumer view of one persisted narrative.

    Generic L1-L3 claims are re-grounded against the current graph.  L4 flow
    sections are a separate contract-discriminated hypothesis payload: they
    remain visible through ``flow``/``sections`` but never masquerade as
    evidence-bearing claims.
    """

    claims: list[dict[str, Any]]
    grounding_status: str
    flow: dict[str, Any] | None = None
    source_snapshot: dict[str, Any] | None = None
    sections: list[dict[str, Any]] | None = None
    contract_error: str | None = None


def _visible_summary_claims(summary: Any) -> list[dict[str, Any]]:
    """Return bounded canonical claims, excluding legacy/control metadata."""

    raw_claims = getattr(summary, "claims", None)
    if not isinstance(raw_claims, list):
        return []
    claims: list[dict[str, Any]] = []
    for claim in raw_claims[:MAX_CLAIMS]:
        if not isinstance(claim, Mapping) or claim.get("claim") == "_evidence_hash":
            continue
        try:
            normalized = normalize_extractor_payload(
                {
                    "summary": "Persisted claim revalidation.",
                    "detailed": "Persisted claim revalidation.",
                    "claims": [dict(claim)],
                    "open_questions": [],
                }
            )
        except ExtractorSchemaError:
            continue
        claims.extend(normalized["claims"])
        if len(claims) >= MAX_CLAIMS:
            break
    return claims


def _visible_flow_summary(
    summary: Any,
) -> tuple[FlowSummaryContent | None, str | None]:
    """Return one strict L4 view and a safe diagnostic for invalid content."""

    if getattr(summary, "level", None) != FLOW_LEVEL:
        return None, None
    try:
        return (
            normalize_flow_summary_content(
                getattr(summary, "claims", None),
                summary=getattr(summary, "summary", None),
                detailed=getattr(summary, "detailed", None),
                open_questions=getattr(summary, "open_questions", None),
            ),
            None,
        )
    except FlowContractError as exc:
        # FlowContractError reports only static paths and type/size shapes.
        return None, str(exc)


def _summary_is_fallback(summary: Any) -> bool:
    model_used = getattr(summary, "model_used", "") or ""
    return bool(
        getattr(summary, "fallback_reason", None)
        or str(model_used).startswith("stub")
    )


async def current_summary_claim_views(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    summaries: list[Any],
) -> list[SummaryClaimView]:
    """Revalidate bounded persisted claims against one project's current graph.

    The caller first bounds Summary rows.  This helper then flattens their
    claims into one call to :func:`validate_claims`.  Current node/edge IDs are
    fetched in deterministic bounded batches, avoiding both an N+1 loop and a
    single bind list large enough to exceed SQLite/PostgreSQL driver limits.
    Private slot fields are deterministic metadata added to copies and removed
    before anything is returned to a consumer.

    A non-fallback generic narrative with no surviving grounded claim is
    ``stale``: its prose remains visible for compatibility, but it is
    explicitly not authoritative after the graph changed.  A valid L4 flow is
    instead ``hypothesis`` because its bounded source provenance is intact but
    its model-authored step/flag prose has never carried graph evidence.  A
    malformed L4 persistence envelope is ``invalid`` with a safe contract
    diagnostic rather than being mislabeled as evidence staleness.
    """

    if len(summaries) > _MAX_SUMMARIES_PER_REVALIDATION:
        raise ValueError(
            f"summary revalidation is bounded to {_MAX_SUMMARIES_PER_REVALIDATION} rows"
        )

    tagged_claims: list[dict[str, Any]] = []
    flow_results = [_visible_flow_summary(summary) for summary in summaries]
    for summary_slot, summary in enumerate(summaries):
        for claim_slot, claim in enumerate(_visible_summary_claims(summary)):
            tagged_claims.append(
                {
                    **claim,
                    _SUMMARY_SLOT_KEY: summary_slot,
                    _CLAIM_SLOT_KEY: claim_slot,
                }
            )

    accepted, _rejected = await validate_claims(
        session,
        project_id=project_id,
        claims=tagged_claims,
        provided_evidence=None,
    )
    grouped: list[list[tuple[int, dict[str, Any]]]] = [
        [] for _summary in summaries
    ]
    for claim in accepted:
        summary_slot = claim.pop(_SUMMARY_SLOT_KEY, None)
        claim_slot = claim.pop(_CLAIM_SLOT_KEY, None)
        if (
            isinstance(summary_slot, int)
            and not isinstance(summary_slot, bool)
            and 0 <= summary_slot < len(grouped)
            and isinstance(claim_slot, int)
            and not isinstance(claim_slot, bool)
        ):
            grouped[summary_slot].append((claim_slot, claim))

    views: list[SummaryClaimView] = []
    for summary_slot, summary in enumerate(summaries):
        claims = [
            claim
            for _claim_slot, claim in sorted(
                grouped[summary_slot], key=lambda item: item[0]
            )
        ]
        flow_content, flow_error = flow_results[summary_slot]
        if _summary_is_fallback(summary):
            status = "fallback"
        elif flow_content is not None:
            # L4 steps/flags are source-window-bounded model hypotheses.  They
            # currently have no graph/line evidence references, so ``grounded``
            # would be false and ``stale`` would incorrectly imply evidence was
            # once present and later disappeared.
            status = "hypothesis"
        elif flow_error is not None:
            status = "invalid"
        elif claims:
            status = "grounded"
        else:
            status = "stale"
        views.append(
            SummaryClaimView(
                claims=claims,
                grounding_status=status,
                flow=flow_content.flow if flow_content is not None else None,
                source_snapshot=(
                    flow_content.source_snapshot
                    if flow_content is not None
                    else None
                ),
                sections=(
                    flow_content.sections if flow_content is not None else None
                ),
                contract_error=flow_error,
            )
        )
    return views
