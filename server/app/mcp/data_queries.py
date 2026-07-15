"""Data-tool queries shared by MCP and HTTP surfaces."""

from __future__ import annotations

import re
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.graph_overlays import load_node_human_overlays, node_read_view
from app.models.graph import Node
from app.models.samples import DataSample
from app.sample_currentness import (
    CurrentSamplePolicy,
    current_sample_predicates,
    current_sample_revision_predicates,
    read_current_sample_policy,
    read_current_sample_revision,
    revalidate_current_sample_policy,
    sample_matches_current_policy,
)


async def _policies_still_current(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    policies: dict[str | None, CurrentSamplePolicy],
) -> bool:
    for component_id, expected in policies.items():
        if not await revalidate_current_sample_policy(
            session,
            project_id=project_id,
            component_id=component_id,
            expected=expected,
        ):
            return False
    return True


async def get_data_entity(
    session: AsyncSession, *, project_id: uuid.UUID, entity_id: str
) -> dict[str, Any] | None:
    sample_revision = await read_current_sample_revision(
        session, project_id=project_id
    )
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return None
    node_overlays = await load_node_human_overlays(
        session, project_id=project_id, node_ids=[node.id]
    )
    view = node_read_view(node, node_overlays.get(node.id))
    sample_policy = await read_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
    )
    latest = (
        await session.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
                *current_sample_predicates(sample_revision, sample_policy),
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if not await revalidate_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
        expected=sample_policy,
    ):
        latest = None
    return {
        "entity": view["data"],
        "certainty": view["certainty"],
        "source_certainty": view["source_certainty"],
        "effective_certainty": view["effective_certainty"],
        "confirmed": view["confirmed"],
        "sample_available": latest is not None,
        "is_sensitive": view["data"].get("is_sensitive", False),
    }


async def get_sample_data(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entity_id: str,
    limit: int = 10,
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    sample_revision = await read_current_sample_revision(
        session, project_id=project_id
    )
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return {"error": "entity_not_found"}
    node_overlays = await load_node_human_overlays(
        session, project_id=project_id, node_ids=[node.id]
    )
    view = node_read_view(node, node_overlays.get(node.id))
    if view["data"].get("is_sensitive"):
        return {"error": "sensitive_entity_sample_disallowed"}
    sample_policy = await read_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
    )
    sample = (
        await session.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
                *current_sample_predicates(sample_revision, sample_policy),
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample is None:
        return {"error": "no_sample_yet"}
    if not await revalidate_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
        expected=sample_policy,
    ):
        return {"error": "no_sample_yet"}
    rows = sample.sample_rows[:limit] if isinstance(sample.sample_rows, list) else []
    return {
        "columns": list(rows[0].keys()) if rows else [],
        "rows": rows,
        "row_count_estimate": sample.row_count_estimate,
        "sampled_at": sample.sampled_at,
        "masking_applied": sample.masking_applied,
        "source_run_id": str(sample.source_run_id),
        "source_git_sha": sample.source_git_sha,
        "source_graph_generation": sample.source_graph_generation,
        "source_overlay_generation": sample.source_overlay_generation,
        "source_project_db_present": sample.source_project_db_present,
        "source_project_db_id": (
            str(sample.source_project_db_id)
            if sample.source_project_db_id is not None
            else None
        ),
        "source_policy_hash": sample.source_policy_hash,
    }


async def get_column_stats(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    entity_id: str,
    column: str,
) -> dict[str, Any]:
    sample_revision = await read_current_sample_revision(
        session, project_id=project_id
    )
    node = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        return {"error": "no_stats"}
    node_overlays = await load_node_human_overlays(
        session, project_id=project_id, node_ids=[node.id]
    )
    view = node_read_view(node, node_overlays.get(node.id))
    if view["data"].get("is_sensitive"):
        return {"error": "no_stats"}
    sample_policy = await read_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
    )
    sample = (
        await session.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
                *current_sample_predicates(sample_revision, sample_policy),
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample is None or sample.column_stats is None:
        return {"error": "no_stats"}
    if not await revalidate_current_sample_policy(
        session,
        project_id=project_id,
        component_id=view["data"].get("component_id"),
        expected=sample_policy,
    ):
        return {"error": "no_stats"}
    return sample.column_stats.get(column, {"error": "column_not_in_stats"})


async def search_data(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    value_pattern: str,
    max_hits: int = 50,
) -> list[dict[str, Any]]:
    pattern = re.compile(value_pattern)
    sample_revision = await read_current_sample_revision(
        session, project_id=project_id
    )
    samples = (
        await session.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                *current_sample_revision_predicates(sample_revision),
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(200)
        )
    ).scalars().all()
    if not samples:
        return []

    entity_ids = {sample.data_entity_id for sample in samples}
    nodes = (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id.in_(entity_ids),
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalars().all()
    overlays = await load_node_human_overlays(
        session, project_id=project_id, node_ids=(node.id for node in nodes)
    )
    views = {
        node.id: node_read_view(node, overlays.get(node.id)) for node in nodes
    }
    policies: dict[str | None, CurrentSamplePolicy] = {}
    hits: list[dict[str, Any]] = []
    for sample in samples:
        view = views.get(sample.data_entity_id)
        if view is None or view["data"].get("is_sensitive"):
            continue
        component_id = view["data"].get("component_id")
        policy_key = component_id if isinstance(component_id, str) else None
        policy = policies.get(policy_key)
        if policy is None:
            policy = await read_current_sample_policy(
                session,
                project_id=project_id,
                component_id=component_id,
            )
            policies[policy_key] = policy
        if not sample_matches_current_policy(sample, policy):
            continue
        rows = sample.sample_rows or []
        for idx, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            for col, val in row.items():
                if val is None:
                    continue
                if pattern.search(str(val)):
                    hits.append(
                        {
                            "entity_id": sample.data_entity_id,
                            "column": col,
                            "sample_row_index": idx,
                            "masked_snippet": str(val)[:80],
                        }
                    )
                    if len(hits) >= max_hits:
                        return (
                            hits
                            if await _policies_still_current(
                                session,
                                project_id=project_id,
                                policies=policies,
                            )
                            else []
                        )
    if not await _policies_still_current(
        session,
        project_id=project_id,
        policies=policies,
    ):
        return []
    return hits
