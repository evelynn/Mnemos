"""Deterministic logical identity for graph-derived findings.

Finding rows are mutable lifecycle records, while bitemporal ``Edge.id``
values identify one physical source version.  This module owns the canonical
boundary between detector subjects and the database uniqueness key so a
re-published edge version resolves to the same finding.
"""

from __future__ import annotations

import hashlib
import uuid

FINDING_IDENTITY_CONTRACT = "mnemos.finding.identity.v1"


class FindingIdentityError(ValueError):
    """The detector did not provide exactly one valid logical subject."""


def _part(path: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise FindingIdentityError(
            f"{path}: expected a non-empty string for finding identity"
        )
    return f"{len(value.encode('utf-8'))}:{value}"


def canonical_finding_identity(
    *,
    kind: str,
    subject_node_id: str | None = None,
    subject_edge: tuple[str, str, str] | None = None,
    legacy_id: uuid.UUID | str | None = None,
) -> str:
    """Return the one canonical, length-prefixed identity payload.

    ``legacy_id`` exists only for migration/backfill rows whose original
    logical subject cannot be reconstructed.  Runtime detectors must provide
    a node id or the edge tuple ``(source_id, target_id, edge_kind)``.
    """

    finding_kind = _part("kind", kind)
    supplied = sum(
        value is not None
        for value in (subject_node_id, subject_edge, legacy_id)
    )
    if supplied != 1:
        raise FindingIdentityError(
            "subject: expected exactly one of subject_node_id, "
            "subject_edge, or legacy_id"
        )
    if subject_node_id is not None:
        return (
            f"{FINDING_IDENTITY_CONTRACT}|kind={finding_kind}"
            f"|node={_part('subject_node_id', subject_node_id)}"
        )
    if subject_edge is not None:
        if not isinstance(subject_edge, tuple) or len(subject_edge) != 3:
            raise FindingIdentityError(
                "subject_edge: expected (source_id, target_id, edge_kind)"
            )
        source_id, target_id, edge_kind = subject_edge
        return (
            f"{FINDING_IDENTITY_CONTRACT}|kind={finding_kind}"
            f"|edge_source={_part('subject_edge.source_id', source_id)}"
            f"|edge_target={_part('subject_edge.target_id', target_id)}"
            f"|edge_kind={_part('subject_edge.kind', edge_kind)}"
        )
    return (
        f"{FINDING_IDENTITY_CONTRACT}|kind={finding_kind}"
        f"|legacy_id={_part('legacy_id', str(legacy_id))}"
    )


def finding_identity_key(
    *,
    kind: str,
    subject_node_id: str | None = None,
    subject_edge: tuple[str, str, str] | None = None,
    legacy_id: uuid.UUID | str | None = None,
) -> str:
    """Return lowercase SHA-256 hex for the canonical logical subject."""

    canonical = canonical_finding_identity(
        kind=kind,
        subject_node_id=subject_node_id,
        subject_edge=subject_edge,
        legacy_id=legacy_id,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
