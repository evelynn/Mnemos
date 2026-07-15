"""Exact graph/source/policy predicates for persisted data samples.

Samples are policy decisions made against a graph revision, the immutable
source commit that produced it, and the effective masking/sensitive-table
policy.  Every reader uses this module so a ProjectDB replacement, update, or
deletion cannot leave rows captured under an older policy visible.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import false, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.data_sampler.masking import (
    MASKING_ENGINE_POLICY_VERSION,
    MaskingEngine,
)
from app.data_sampler.project_db import resolve_project_db
from app.graph_publication import GraphReadStamp, read_graph_stamp
from app.models.graph import AnalysisRun
from app.models.samples import DataSample

if TYPE_CHECKING:
    from app.models.projects import ProjectDB

_CANONICAL_GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")


@dataclass(frozen=True)
class CurrentSampleRevision:
    """Canonical revision a DataSample must match to be readable."""

    stamp: GraphReadStamp
    git_sha: str | None


@dataclass(frozen=True)
class CurrentSamplePolicy:
    """Canonical effective policy a DataSample must match to be readable."""

    project_db_present: bool
    project_db_id: uuid.UUID | None
    policy_hash: str
    effective_masking_rules: dict[str, tuple[str, ...]]
    effective_sensitive_tables: tuple[str, ...]


def _effective_masking_rules(raw: Any) -> dict[str, tuple[str, ...]]:
    """Return the rules the masking engine actually accepted.

    Rule ordering and duplicates do not change ``MaskingEngine`` decisions,
    so canonical provenance sorts and de-duplicates the compiled patterns.
    Invalid regexes are omitted by the engine and therefore omitted here too.
    """

    engine = MaskingEngine.from_project_db(raw if isinstance(raw, dict) else None)
    return {
        "full": tuple(sorted({pattern.pattern for pattern in engine.extra_full_mask})),
        "partial": tuple(
            sorted({pattern.pattern for pattern in engine.extra_partial_mask})
        ),
    }


def _effective_sensitive_tables(raw: Any) -> tuple[str, ...]:
    """Canonicalise the case-insensitive set enforced by policy lookup."""

    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(
        sorted(
            {
                value.lower()
                for value in raw
                if isinstance(value, str) and value
            }
        )
    )


def sample_policy_for_project_db(
    project_db: ProjectDB | None,
) -> CurrentSamplePolicy:
    """Build a stable SHA-256 stamp for one effective sample policy.

    A missing binding is an explicit policy state, not missing provenance.
    The nullable historical ID is kept separately on ``DataSample`` so a
    deleted/replaced binding cannot accidentally match a newly absent one.
    """

    present = project_db is not None
    project_db_id = project_db.id if project_db is not None else None
    masking_rules = _effective_masking_rules(
        project_db.masking_rules if project_db is not None else None
    )
    sensitive_tables = _effective_sensitive_tables(
        project_db.sensitive_tables if project_db is not None else None
    )
    payload = {
        "schema": "mnemos.data-sample-policy.v1",
        "default_masking_engine": MASKING_ENGINE_POLICY_VERSION,
        "project_db": {
            "present": present,
            "id": str(project_db_id) if project_db_id is not None else None,
        },
        "masking_rules": {
            "full": list(masking_rules["full"]),
            "partial": list(masking_rules["partial"]),
        },
        "sensitive_tables": list(sensitive_tables),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return CurrentSamplePolicy(
        project_db_present=present,
        project_db_id=project_db_id,
        policy_hash=hashlib.sha256(canonical).hexdigest(),
        effective_masking_rules=masking_rules,
        effective_sensitive_tables=sensitive_tables,
    )


async def read_current_sample_policy(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: object,
) -> CurrentSamplePolicy:
    """Resolve and stamp the current binding for an effective component ID."""

    project_db = None
    if isinstance(component_id, str) and component_id:
        project_db = await resolve_project_db(session, project_id, component_id)
    return sample_policy_for_project_db(project_db)


async def revalidate_current_sample_policy(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    component_id: object,
    expected: CurrentSamplePolicy,
) -> bool:
    """Re-read policy state and require canonical dataclass equality.

    ``resolve_project_db`` uses ``populate_existing`` so this second read
    refreshes an identity already cached by an ``expire_on_commit=False``
    session.  It also detects the absent-to-created case because absence is a
    first-class policy state rather than a missing lock target.
    """

    current = await read_current_sample_policy(
        session,
        project_id=project_id,
        component_id=component_id,
    )
    return current == expected


async def read_current_sample_revision(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
) -> CurrentSampleRevision:
    """Read the validated graph stamp and its canonical source commit.

    Non-Git/manual publications can still serve graph reads, but they cannot
    authorise a persisted sample.  Represent those with ``git_sha=None`` so
    the shared predicate below becomes an explicit SQL false condition.
    """

    stamp = await read_graph_stamp(session, project_id=project_id)
    git_sha = (
        await session.execute(
            select(AnalysisRun.git_sha).where(
                AnalysisRun.id == stamp.current_run_id,
                AnalysisRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    canonical_sha = (
        git_sha
        if isinstance(git_sha, str) and _CANONICAL_GIT_SHA.fullmatch(git_sha)
        else None
    )
    return CurrentSampleRevision(stamp=stamp, git_sha=canonical_sha)


def current_sample_predicates(
    revision: CurrentSampleRevision,
    policy: CurrentSamplePolicy,
) -> tuple[ColumnElement[bool], ...]:
    """Return the fail-closed graph/source/policy predicate set."""

    return (
        *current_sample_revision_predicates(revision),
        DataSample.source_project_db_present == policy.project_db_present,
        (
            DataSample.source_project_db_id.is_(None)
            if policy.project_db_id is None
            else DataSample.source_project_db_id == policy.project_db_id
        ),
        DataSample.source_policy_hash == policy.policy_hash,
    )


def current_sample_revision_predicates(
    revision: CurrentSampleRevision,
) -> tuple[ColumnElement[bool], ...]:
    """Return graph/source predicates for policy-checked candidate scans."""

    if revision.git_sha is None:
        return (false(),)
    stamp = revision.stamp
    return (
        DataSample.source_run_id == stamp.current_run_id,
        DataSample.source_git_sha == revision.git_sha,
        DataSample.source_graph_generation == stamp.generation,
        DataSample.source_overlay_generation == stamp.overlay_generation,
    )


def sample_matches_current_policy(
    sample: DataSample,
    policy: CurrentSamplePolicy,
) -> bool:
    """Exact in-memory equivalent used by bounded multi-entity searches."""

    return (
        sample.source_project_db_present is policy.project_db_present
        and sample.source_project_db_id == policy.project_db_id
        and sample.source_policy_hash == policy.policy_hash
    )


__all__ = [
    "CurrentSampleRevision",
    "CurrentSamplePolicy",
    "current_sample_predicates",
    "current_sample_revision_predicates",
    "read_current_sample_policy",
    "read_current_sample_revision",
    "revalidate_current_sample_policy",
    "sample_matches_current_policy",
    "sample_policy_for_project_db",
]
