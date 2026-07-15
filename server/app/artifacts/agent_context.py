"""Machine-readable context artifacts for coding agents.

Mnemos stores the full source-analysis graph in the database. Claude Code and
Codex should not receive that whole graph in one prompt; they should receive a
small project index, then ask for bounded task packs as they work. This module
builds those two artifacts from the current graph snapshot.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.finding_currentness import current_findings_select
from app.graph_overlays import (
    edge_identity,
    edge_read_view,
    effective_certainty,
    load_edge_overlays,
    load_node_human_overlays,
    node_read_view,
)
from app.graph_publication import (
    GRAPH_HEAD_READY,
    GraphGenerationChanged,
    GraphHeadMissing,
    GraphHeadNeedsRebuild,
    GraphPublicationInvariantError,
    read_graph_stamp,
    revalidate_graph_stamp,
)
from app.mcp.queries import project_root_prefix, relative_source_path
from app.models.findings import Finding, Summary
from app.models.graph import AnalysisRun, Edge, GraphHead, Node
from app.models.overlays import GraphEdgeHumanOverlay, GraphNodeHumanOverlay
from app.models.projects import Project
from app.models.stages import AnalysisStage

SCHEMA_PROJECT_INDEX = "mnemos.agent.project_index.v1"
SCHEMA_TASK_PACK = "mnemos.agent.task_context_pack.v1"

# Keep the artifact itself within the MCP transport's independent 50 KiB
# response ceiling.  The MCP layer remains a last-resort transport guard;
# these builders must already return a useful, explicitly truncated source
# reference instead of relying on that generic guard to replace the payload.
DEFAULT_AGENT_CONTEXT_MAX_BYTES = 50 * 1024
_MIN_AGENT_CONTEXT_MAX_BYTES = 4 * 1024

_CERTAINTY_ORDER = ("verified", "asserted", "inferred")
_RAW_DATA_KEYS = {
    "body",
    "blob",
    "bytes",
    "code",
    "content",
    "content_base64",
    "file_text",
    "full_text",
    "payload",
    "raw",
    "snippet",
    "source_text",
}
_MAX_DATA_DEPTH = 4
_MAX_DATA_KEYS = 80
_MAX_DATA_KEY_CHARS = 160
_MAX_DATA_LIST_ITEMS = 50
_MAX_DATA_STRING_CHARS = 512
_MAX_SIGNATURE_CHARS = 240
_MAX_SUMMARY_CHARS = 1_200
_MAX_DETAILED_CHARS = 4_000
_MAX_SUMMARY_CLAIMS = 20
_MAX_OPEN_QUESTIONS = 12
_MAX_CURRENT_SUMMARIES = 8
_MAX_TRUNCATION_PATHS = 24
_LOW_SIGNAL_PATH_SEGMENTS = {
    "tests",
    "test",
    "__tests__",
    "vendored",
    "vendor",
    "third_party",
    "thirdparty",
    "node_modules",
    "build",
    "dist",
    "coverage",
}
_SUPPORT_PATH_SEGMENTS = {"tools", "scripts", "fixtures", "examples", "docs"}
_MCP_WORKFLOWS: dict[str, list[str]] = {
    "orient": ["get_project_index", "search_symbols", "get_module_summary"],
    "modify_symbol": [
        "get_task_context_pack",
        "get_symbol",
        "find_callers",
        "find_callees",
        "get_data_access",
        "impact_analysis",
    ],
    "modify_contract": [
        "get_task_context_pack",
        "get_contract",
        "find_runtime_path",
        "impact_analysis",
    ],
    "modify_data_access": [
        "get_task_context_pack",
        "get_data_access",
        "get_data_entity",
        "get_column_stats",
    ],
    "fix_finding": [
        "list_findings",
        "get_task_context_pack",
        "submit_plan",
        "run_in_sandbox",
        "submit_diff",
    ],
}


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else None


def _measure(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"type": "str", "chars": len(value)}
    if isinstance(value, bytes):
        return {"type": "bytes", "bytes": len(value)}
    if isinstance(value, list):
        return {"type": "list", "items": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "keys": len(value)}
    return {"type": type(value).__name__}


def _is_raw_key(key: str | None) -> bool:
    if key is None:
        return False
    normalized = key.lower()
    return (
        normalized in _RAW_DATA_KEYS
        or normalized.endswith("_raw")
        or normalized.endswith("_content")
        or normalized.endswith("_blob")
    )


def _bounded_value(value: Any, *, key: str | None = None, depth: int = 0) -> Any:
    """Project arbitrary graph metadata into an agent-safe, bounded shape."""
    if key in {"signature", "excerpt"}:
        return _signature_ref(value)
    if _is_raw_key(key):
        return {"omitted": "raw_payload", **_measure(value)}
    if depth >= _MAX_DATA_DEPTH:
        return {"omitted": "max_depth", **_measure(value)}
    if isinstance(value, str):
        if len(value) > _MAX_DATA_STRING_CHARS:
            return {"omitted": "large_string", "chars": len(value)}
        return value
    if isinstance(value, bytes):
        return {"omitted": "bytes", "bytes": len(value)}
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= _MAX_DATA_KEYS:
                break
            safe_key = str(child_key)
            if len(safe_key) > _MAX_DATA_KEY_CHARS:
                safe_key = (
                    f"{safe_key[:120]}... "
                    f"[truncated key chars={len(safe_key)} index={index}]"
                )
            out[safe_key] = _bounded_value(
                child_value, key=safe_key, depth=depth + 1
            )
        if len(value) > _MAX_DATA_KEYS:
            out["__truncated_keys__"] = len(value) - _MAX_DATA_KEYS
        return out
    if isinstance(value, list):
        out = [
            _bounded_value(item, key=key, depth=depth + 1)
            for item in value[:_MAX_DATA_LIST_ITEMS]
        ]
        if len(value) > _MAX_DATA_LIST_ITEMS:
            out.append({"__truncated_items__": len(value) - _MAX_DATA_LIST_ITEMS})
        return out
    return value


def _agent_data(data: dict[str, Any] | None) -> dict[str, Any]:
    bounded = _bounded_value(data or {}, depth=0)
    return bounded if isinstance(bounded, dict) else {}


def _agent_value(value: Any) -> Any:
    return _bounded_value(value, depth=0)


def _bounded_text(value: Any, *, max_chars: int) -> tuple[Any, int | None]:
    """Keep a useful prefix of narrative text and report the original size."""
    if not isinstance(value, str) or len(value) <= max_chars:
        return value, None
    suffix = f"... [truncated chars={len(value)}]"
    keep = max(0, max_chars - len(suffix))
    return f"{value[:keep]}{suffix}", len(value)


def _serialized_size(value: Any) -> int:
    """Measure exactly as the MCP transport serialises an object."""
    return len(json.dumps(value, default=str).encode("utf-8"))


def _normalise_serialized_budget(value: int) -> int:
    return max(
        _MIN_AGENT_CONTEXT_MAX_BYTES,
        min(int(value), DEFAULT_AGENT_CONTEXT_MAX_BYTES),
    )


def _path_label(path: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for part in path:
        if isinstance(part, int):
            parts.append(f"[{part}]")
        else:
            if parts:
                parts.append(".")
            parts.append(str(part))
    label = "".join(parts)
    return label if len(label) <= 160 else f"{label[:157]}..."


def _record_trim(
    metadata: dict[str, Any],
    path: tuple[Any, ...],
    *,
    before: int,
    kept: int,
) -> None:
    paths = metadata.setdefault("trimmed_paths", {})
    label = _path_label(path)
    if label in paths:
        paths[label]["kept"] = kept
        return
    if len(paths) >= _MAX_TRUNCATION_PATHS:
        metadata["additional_trimmed_paths"] = (
            int(metadata.get("additional_trimmed_paths", 0)) + 1
        )
        return
    paths[label] = {"before": before, "kept": kept}


def _minimum_list_items(path: tuple[Any, ...], value: list[Any]) -> int:
    if not value:
        return 0
    if path and path[0] in {"agent_contract", "mcp_workflows", "rules"}:
        return len(value)
    if path == ("evidence_refs",) or (path and path[-1] == "created_by"):
        return 1
    if path in {
        ("entrypoints", "contracts"),
        ("entrypoints", "hot_symbols"),
        ("data_map", "entities"),
        ("risk_queue", "findings"),
        ("next_mcp_queries",),
    }:
        return 1
    return 0


def _scale_lists(
    value: Any,
    *,
    ratio: float,
    metadata: dict[str, Any],
    path: tuple[Any, ...] = (),
) -> bool:
    """Proportionally shrink all removable lists in one traversal."""
    changed = False
    if isinstance(value, dict):
        for key, child in value.items():
            if path == () and key == "truncation":
                continue
            changed = (
                _scale_lists(
                    child,
                    ratio=ratio,
                    metadata=metadata,
                    path=(*path, key),
                )
                or changed
            )
    elif isinstance(value, list):
        minimum = _minimum_list_items(path, value)
        before = len(value)
        kept = max(minimum, int(before * ratio))
        if kept < before:
            del value[kept:]
            _record_trim(metadata, path, before=before, kept=kept)
            changed = True
        for index, child in enumerate(value):
            changed = (
                _scale_lists(
                    child,
                    ratio=ratio,
                    metadata=metadata,
                    path=(*path, index),
                )
                or changed
            )
    return changed


def _scale_strings(
    value: Any,
    *,
    ratio: float,
    metadata: dict[str, Any],
    path: tuple[Any, ...] = (),
    include_identifiers: bool = False,
) -> bool:
    """Proportionally shorten all eligible strings in one traversal."""
    changed = False
    if isinstance(value, dict):
        for key, child in value.items():
            if path == () and key == "truncation":
                continue
            child_path = (*path, key)
            if isinstance(child, str):
                protected = (
                    child_path == ("schema",)
                    or child_path == ("target", "id")
                    or (
                        len(child_path) >= 3
                        and child_path[0] == "evidence_refs"
                        and child_path[-1] in {"id", "type"}
                    )
                )
                if len(child) > 64 and (include_identifiers or not protected):
                    before_bytes = len(child.encode("utf-8"))
                    suffix = f"... [artifact-truncated bytes={before_bytes}]"
                    desired_chars = max(64, int(len(child) * ratio))
                    keep = max(8, desired_chars - len(suffix))
                    replacement = f"{child[:keep]}{suffix}"
                    if len(replacement) < len(child):
                        value[key] = replacement
                        _record_trim(
                            metadata,
                            child_path,
                            before=before_bytes,
                            kept=keep,
                        )
                        changed = True
            else:
                changed = (
                    _scale_strings(
                        child,
                        ratio=ratio,
                        metadata=metadata,
                        path=child_path,
                        include_identifiers=include_identifiers,
                    )
                    or changed
                )
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = (*path, index)
            if isinstance(child, str):
                if len(child) > 64:
                    before_bytes = len(child.encode("utf-8"))
                    suffix = f"... [artifact-truncated bytes={before_bytes}]"
                    desired_chars = max(64, int(len(child) * ratio))
                    keep = max(8, desired_chars - len(suffix))
                    replacement = f"{child[:keep]}{suffix}"
                    if len(replacement) < len(child):
                        value[index] = replacement
                        _record_trim(
                            metadata,
                            child_path,
                            before=before_bytes,
                            kept=keep,
                        )
                        changed = True
            else:
                changed = (
                    _scale_strings(
                        child,
                        ratio=ratio,
                        metadata=metadata,
                        path=child_path,
                        include_identifiers=include_identifiers,
                    )
                    or changed
                )
    return changed


def _settled_size(artifact: dict[str, Any], metadata: dict[str, Any]) -> int:
    """Set ``actual_bytes`` to the fixed-point size that includes itself."""
    size = _serialized_size(artifact)
    for _ in range(6):
        if metadata.get("actual_bytes") == size:
            break
        metadata["actual_bytes"] = size
        size = _serialized_size(artifact)
    metadata["actual_bytes"] = size
    return _serialized_size(artifact)


def _minimal_artifact(
    artifact: dict[str, Any], metadata: dict[str, Any]
) -> dict[str, Any]:
    """Fail-safe shape that retains the source-reference contract."""
    keys = (
        "schema",
        "generated_at",
        "project_id",
        "project",
        "target",
        "budget",
        "rules",
        "error",
        "target_node",
        "target_edge",
        "finding",
        "subject",
        "evidence_refs",
        "next_mcp_queries",
        "agent_contract",
        "analysis_snapshot",
        "coverage_report",
    )
    out = {key: artifact[key] for key in keys if key in artifact}
    out["truncation"] = artifact["truncation"]
    metadata.setdefault("dropped_sections", []).append("nonessential_sections")
    return out


def _apply_serialized_budget(
    artifact: dict[str, Any], *, max_serialized_bytes: int
) -> dict[str, Any]:
    """Return a JSON artifact that cannot exceed its declared byte ceiling.

    List items are removed before text is shortened.  If hostile historical
    metadata still dominates the payload, a minimal contract is returned that
    retains schema, target identity, evidence references, and explicit
    truncation metadata.
    """
    ceiling = _normalise_serialized_budget(max_serialized_bytes)
    truncation = artifact.setdefault("truncation", {})
    if not isinstance(truncation, dict):
        truncation = {"producer_value": _agent_value(truncation)}
        artifact["truncation"] = truncation
    metadata = {
        "max_bytes": ceiling,
        "actual_bytes": 0,
        "truncated": False,
    }
    truncation["serialized_budget"] = metadata

    if _settled_size(artifact, metadata) <= ceiling:
        return artifact

    metadata["truncated"] = True
    metadata["reason"] = "serialized_byte_ceiling"

    # Prefer keeping every section represented. Scale all removable lists in
    # a handful of passes rather than repeatedly serialising once per item.
    for _ in range(8):
        size = _settled_size(artifact, metadata)
        if size <= ceiling:
            return artifact
        ratio = max(0.02, min(0.75, (ceiling / size) * 0.9))
        if not _scale_lists(
            artifact, ratio=ratio, metadata=metadata
        ):
            break

    # Large nested dictionaries (notably historical summaries and provider
    # payloads) may contain no removable lists. Shorten their largest strings
    # next, leaving schema/target/evidence identifiers untouched initially.
    for include_identifiers in (False, True):
        for _ in range(8):
            size = _settled_size(artifact, metadata)
            if size <= ceiling:
                return artifact
            ratio = max(0.05, min(0.75, (ceiling / size) * 0.9))
            if not _scale_strings(
                artifact,
                ratio=ratio,
                metadata=metadata,
                include_identifiers=include_identifiers,
            ):
                break

    if _settled_size(artifact, metadata) <= ceiling:
        return artifact

    artifact = _minimal_artifact(artifact, metadata)

    # The normal 50 KiB contract reaches a fixed point above. This final pass
    # protects the invariant for adversarial identifiers or a caller asking
    # for the supported 4 KiB minimum.
    for _ in range(8):
        size = _settled_size(artifact, metadata)
        if size <= ceiling:
            return artifact
        ratio = max(0.02, min(0.7, (ceiling / size) * 0.85))
        lists_changed = _scale_lists(
            artifact, ratio=ratio, metadata=metadata
        )
        strings_changed = _scale_strings(
            artifact,
            ratio=ratio,
            metadata=metadata,
            include_identifiers=True,
        )
        if not lists_changed and not strings_changed:
            break

    # Fixed schema plus the budget signal fit comfortably in the supported
    # minimum. Reaching this branch means attacker-controlled mapping keys made
    # the richer minimal form impossible, so discard them without hiding it.
    target_value = artifact.get("target")
    if isinstance(target_value, dict):
        target_value = {
            key: _bounded_value(target_value.get(key), key=key, depth=0)
            for key in ("id", "kind", "intent")
            if key in target_value
        }
    evidence_value = artifact.get("evidence_refs", [])
    first_evidence = (
        evidence_value[0]
        if isinstance(evidence_value, list) and evidence_value
        else None
    )
    if isinstance(first_evidence, dict):
        first_evidence = {
            key: _bounded_value(first_evidence.get(key), key=key, depth=0)
            for key in ("type", "id", "valid_from")
            if key in first_evidence
        }
    fallback = {
        "schema": artifact.get("schema"),
        "target": target_value,
        "evidence_refs": [first_evidence] if first_evidence is not None else [],
        "truncation": {
            "serialized_budget": {
                "max_bytes": ceiling,
                "actual_bytes": 0,
                "truncated": True,
                "reason": "serialized_byte_ceiling_minimal_fallback",
            }
        },
    }
    fallback_metadata = fallback["truncation"]["serialized_budget"]
    _settled_size(fallback, fallback_metadata)
    return fallback


def _signature_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    first_line = value.strip().splitlines()[0].strip()
    if len(first_line) <= _MAX_SIGNATURE_CHARS:
        return first_line
    return f"{first_line[:_MAX_SIGNATURE_CHARS]}... [truncated chars={len(value)}]"


def _path_rank(path: Any) -> int:
    if not isinstance(path, str) or not path:
        return 1
    segments = {
        part for part in path.replace("\\", "/").lower().split("/") if part
    }
    if segments & _LOW_SIGNAL_PATH_SEGMENTS:
        return 3
    if segments & _SUPPORT_PATH_SEGMENTS:
        return 2
    return 0


def _node_ref(
    node: Node | None, view: dict[str, Any] | None
) -> dict[str, Any] | None:
    if node is None:
        return None
    if view is None:
        raise ValueError("current node serialization requires a canonical read view")
    data = view["data"]
    safe_data = _agent_data(data)
    return {
        "id": node.id,
        "kind": node.kind,
        "name": _agent_value(data.get("name")),
        "signature": _signature_ref(data.get("signature")),
        "location": _agent_value(data.get("location")),
        "component_id": _agent_value(data.get("component_id")),
        "certainty": view["certainty"],
        "source_certainty": view["source_certainty"],
        "effective_certainty": view["effective_certainty"],
        "confirmed": view["confirmed"],
        "created_by": _agent_value(node.created_by or []),
        "data": safe_data,
        "evidence": {
            "type": "node",
            "id": node.id,
            "valid_from": _iso(node.valid_from),
        },
    }


def _edge_ref(edge: Edge, view: dict[str, Any]) -> dict[str, Any]:
    data = view["data"]
    return {
        "edge_id": str(edge.id),
        "kind": edge.kind,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "certainty": view["certainty"],
        "source_certainty": view["source_certainty"],
        "effective_certainty": view["effective_certainty"],
        "confirmed": view["confirmed"],
        "created_by": _agent_value(edge.created_by or []),
        "exercised": view["exercised"],
        "runtime": {
            key: _agent_value(data.get(key))
            for key in ("first_seen_at", "last_seen_at", "hit_count")
            if data.get(key) is not None
        },
        "site": _agent_value(
            data.get("invocation_site") or data.get("access_site")
        ),
        "data": _agent_data(data),
        "evidence": {
            "type": "edge",
            "id": str(edge.id),
            "valid_from": _iso(edge.valid_from),
        },
    }


async def _node_ref_map(
    session: AsyncSession,
    project_id: uuid.UUID,
    nodes: list[Node],
) -> dict[str, dict[str, Any]]:
    overlays = await load_node_human_overlays(
        session,
        project_id=project_id,
        node_ids=(node.id for node in nodes),
    )
    return {
        node.id: _node_ref(node, node_read_view(node, overlays.get(node.id))) or {}
        for node in nodes
    }


async def _edge_ref_map(
    session: AsyncSession,
    project_id: uuid.UUID,
    edges: list[Edge],
) -> dict[uuid.UUID, dict[str, Any]]:
    overlays = await load_edge_overlays(
        session,
        project_id=project_id,
        identities=(edge_identity(edge) for edge in edges),
    )
    result: dict[uuid.UUID, dict[str, Any]] = {}
    for edge in edges:
        identity = edge_identity(edge)
        view = edge_read_view(
            edge,
            overlays.human.get(identity),
            overlays.runtime.get(identity),
        )
        result[edge.id] = _edge_ref(edge, view)
    return result


def _project_ref(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "name": _agent_value(project.name),
        "gitlab_project_id": project.gitlab_project_id,
        "gitlab_url": _agent_value(project.gitlab_url),
        "default_branch": _agent_value(project.default_branch),
        "languages": _agent_value(project.languages or []),
    }


def _finding_ref(finding: Finding) -> dict[str, Any]:
    remediation, _remediation_chars = _bounded_text(
        finding.remediation, max_chars=_MAX_DETAILED_CHARS
    )
    return {
        "id": str(finding.id),
        "finding_id": str(finding.id),
        "kind": finding.kind,
        "severity": finding.severity,
        "status": finding.status,
        "risk_score": finding.risk_score,
        "subject_node_id": finding.subject_node_id,
        "subject_edge_id": str(finding.subject_edge_id) if finding.subject_edge_id else None,
        "detail": _agent_data(finding.detail),
        "remediation": remediation,
        "cwe_id": finding.cwe_id,
        "first_seen_at": _iso(finding.first_seen_at),
        "last_seen_at": _iso(finding.last_seen_at),
    }


def _summary_ref(summary: Summary) -> dict[str, Any]:
    summary_text, summary_chars = _bounded_text(
        summary.summary, max_chars=_MAX_SUMMARY_CHARS
    )
    detailed_text, detailed_chars = _bounded_text(
        summary.detailed, max_chars=_MAX_DETAILED_CHARS
    )
    claims_source = summary.claims if isinstance(summary.claims, list) else []
    questions_source = (
        summary.open_questions if isinstance(summary.open_questions, list) else []
    )
    out = {
        "target_id": summary.target_id,
        "level": summary.level,
        "summary": summary_text,
        "detailed": detailed_text,
        "claims": _agent_value(claims_source[:_MAX_SUMMARY_CLAIMS]),
        "open_questions": _agent_value(
            questions_source[:_MAX_OPEN_QUESTIONS]
        ),
        "model_used": summary.model_used,
        "fallback_reason": summary.fallback_reason,
        "generated_at": _iso(summary.generated_at),
        "validated_graph_generation": summary.validated_graph_generation,
        "validated_overlay_generation": summary.validated_overlay_generation,
        "validated_at": _iso(summary.validated_at),
    }
    truncated: dict[str, Any] = {}
    if summary_chars is not None:
        truncated["summary_chars"] = summary_chars
    if detailed_chars is not None:
        truncated["detailed_chars"] = detailed_chars
    if len(claims_source) > _MAX_SUMMARY_CLAIMS:
        truncated["claims"] = {
            "kept": _MAX_SUMMARY_CLAIMS,
            "total": len(claims_source),
        }
    if len(questions_source) > _MAX_OPEN_QUESTIONS:
        truncated["open_questions"] = {
            "kept": _MAX_OPEN_QUESTIONS,
            "total": len(questions_source),
        }
    if truncated:
        out["truncation"] = truncated
    return out


async def _require_project(
    session: AsyncSession, project_id: uuid.UUID
) -> Project | None:
    return (
        await session.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()


async def _certainty_breakdown(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, dict[str, int]]:
    node_rows = (
        await session.execute(
            select(Node.certainty, func.count())
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .group_by(Node.certainty)
        )
    ).all()
    edge_rows = (
        await session.execute(
            select(Edge.certainty, func.count())
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .group_by(Edge.certainty)
        )
    ).all()
    confirmed_node_rows = (
        await session.execute(
            select(Node.certainty, func.count())
            .join(
                GraphNodeHumanOverlay,
                and_(
                    GraphNodeHumanOverlay.project_id == Node.project_id,
                    GraphNodeHumanOverlay.node_id == Node.id,
                    GraphNodeHumanOverlay.action == "confirm",
                ),
            )
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .group_by(Node.certainty)
        )
    ).all()
    confirmed_edge_rows = (
        await session.execute(
            select(Edge.certainty, func.count())
            .join(
                GraphEdgeHumanOverlay,
                and_(
                    GraphEdgeHumanOverlay.project_id == Edge.project_id,
                    GraphEdgeHumanOverlay.source_id == Edge.source_id,
                    GraphEdgeHumanOverlay.target_id == Edge.target_id,
                    GraphEdgeHumanOverlay.kind == Edge.kind,
                    GraphEdgeHumanOverlay.action == "confirm",
                ),
            )
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .group_by(Edge.certainty)
        )
    ).all()

    def _effective_counts(
        rows: list[tuple[str, int]],
        confirmed_rows: list[tuple[str, int]],
    ) -> dict[str, int]:
        counts = {certainty: int(count) for certainty, count in rows}
        for source, raw_count in confirmed_rows:
            effective = effective_certainty(source, "confirm")
            count = int(raw_count)
            if effective == source:
                continue
            counts[source] = max(0, counts.get(source, 0) - count)
            counts[effective] = counts.get(effective, 0) + count
        return counts

    return {
        "nodes": _effective_counts(node_rows, confirmed_node_rows),
        "edges": _effective_counts(edge_rows, confirmed_edge_rows),
    }


async def _node_kind_counts(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Node.kind, func.count())
            .where(Node.project_id == project_id, Node.valid_to.is_(None))
            .group_by(Node.kind)
        )
    ).all()
    return {k: int(v) for k, v in rows}


async def _edge_kind_counts(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Edge.kind, func.count())
            .where(Edge.project_id == project_id, Edge.valid_to.is_(None))
            .group_by(Edge.kind)
        )
    ).all()
    return {k: int(v) for k, v in rows}


async def _latest_run(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    statuses: set[str] | None = None,
) -> dict[str, Any] | None:
    stmt = select(AnalysisRun).where(AnalysisRun.project_id == project_id)
    if statuses is not None:
        stmt = stmt.where(AnalysisRun.status.in_(statuses))
    run = (
        await session.execute(stmt.order_by(AnalysisRun.created_at.desc()).limit(1))
    ).scalar_one_or_none()
    if run is None:
        return None
    return _run_payload(run)


async def _run_by_id(
    session: AsyncSession,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
) -> dict[str, Any] | None:
    run = (
        await session.execute(
            select(AnalysisRun).where(
                AnalysisRun.id == run_id,
                AnalysisRun.project_id == project_id,
            )
        )
    ).scalar_one_or_none()
    return _run_payload(run) if run is not None else None


def _run_payload(run: AnalysisRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "status": run.status,
        "git_sha": run.git_sha,
        "scope": run.scope,
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "created_at": _iso(run.created_at),
        "stats": _agent_data(run.stats),
        "error_log": _bounded_value(run.error_log, key="error_log"),
    }


async def _top_symbols(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                Node,
                func.count(Edge.id).label("incoming_calls"),
            )
            .outerjoin(
                Edge,
                and_(
                    Edge.project_id == Node.project_id,
                    Edge.target_id == Node.id,
                    Edge.kind == "CALLS",
                    Edge.valid_to.is_(None),
                ),
            )
            .where(
                Node.project_id == project_id,
                Node.valid_to.is_(None),
                Node.kind == "Symbol",
            )
            .group_by(Node.id, Node.project_id, Node.valid_from)
            .order_by(desc("incoming_calls"), Node.id)
            .limit(max(20, min(limit * 10, 1000)))
        )
    ).all()
    nodes = [row[0] for row in rows]
    overlays = await load_node_human_overlays(
        session,
        project_id=project_id,
        node_ids=(node.id for node in nodes),
    )
    out = []
    for node, incoming_calls in rows:
        view = node_read_view(node, overlays.get(node.id))
        data = view["data"]
        out.append(
            {
                "symbol_id": node.id,
                "kind": node.kind,
                "name": _agent_value(data.get("name")),
                "signature": _signature_ref(data.get("signature")),
                "location": _agent_value(data.get("location")),
                "component_id": _agent_value(data.get("component_id")),
                "certainty": view["certainty"],
                "source_certainty": view["source_certainty"],
                "effective_certainty": view["effective_certainty"],
                "confirmed": view["confirmed"],
                "incoming_calls": int(incoming_calls or 0),
                "_path_rank": _path_rank((data.get("location") or {}).get("file")),
            }
        )
    out.sort(key=lambda row: (row.pop("_path_rank"), -row["incoming_calls"], row["symbol_id"]))
    return out[:limit]


async def _nodes_by_kind(
    session: AsyncSession, project_id: uuid.UUID, kind: str, limit: int
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.kind == kind,
                Node.valid_to.is_(None),
            )
            .order_by(Node.id)
            .limit(max(1, min(limit, 500)))
        )
    ).scalars().all()
    refs = await _node_ref_map(session, project_id, list(rows))
    return [refs[node.id] for node in rows]


async def _risk_findings(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int,
    subject_node_id: str | None = None,
    subject_edge_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        current_findings_select(project_id)
        .order_by(Finding.risk_score.desc(), Finding.last_seen_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    if subject_node_id is not None:
        stmt = stmt.where(Finding.subject_node_id == subject_node_id)
    if subject_edge_id is not None:
        stmt = stmt.where(Finding.subject_edge_id == subject_edge_id)
    rows = (await session.execute(stmt)).scalars().all()
    return [_finding_ref(f) for f in rows]


async def _known_flows(
    session: AsyncSession, project_id: uuid.UUID, limit: int
) -> list[dict[str, Any]]:
    from app.mcp.queries import list_flows

    return _agent_value(
        await list_flows(session, project_id=project_id, limit=limit)
    )


def _annotate_relative_files(obj: Any, root_prefix: str | None) -> None:
    """Walk a built artifact in place: every ``{"location": {"file": ...}}``
    gains one stable project-relative path.  Ephemeral worker paths are not
    exposed to agents because they cannot round-trip through snapshot-bound
    ``read_file`` and may already have been deleted."""
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            loc = cur.get("location")
            if (
                isinstance(loc, dict)
                and isinstance(loc.get("file"), str)
                and "relative_file" not in loc
            ):
                relative_file = relative_source_path(loc.get("file"), root_prefix)
                if relative_file:
                    loc["relative_file"] = relative_file
                    loc["file"] = relative_file
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


_EXTRACTION_STAGE_PREFIXES = (
    "symbols:", "contracts:", "calls:", "data_access:", "agent_extract:",
)


async def _summary_quality(
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Summary.model_used, func.count())
            .join(GraphHead, GraphHead.project_id == Summary.project_id)
            .where(
                Summary.project_id == project_id,
                Summary.superseded_by.is_(None),
                GraphHead.state == GRAPH_HEAD_READY,
                Summary.validated_graph_generation == GraphHead.generation,
                Summary.validated_overlay_generation
                == GraphHead.overlay_generation,
            )
            .group_by(Summary.model_used)
        )
    ).all()
    by_model = {str(model or "none"): int(count) for model, count in rows}
    stub_only = bool(by_model) and set(by_model) <= {"stub", "none"}
    out: dict[str, Any] = {"summaries_by_model": by_model, "stub_only": stub_only}
    if stub_only:
        out["warning"] = (
            "all summaries are deterministic structural placeholders "
            "(no LLM backend); use them for orientation only, not for "
            "behavioural claims"
        )
    return out


async def _graph_path_buckets(
    session: AsyncSession,
    project_id: uuid.UUID,
    root_prefix: str | None,
    *,
    sample_limit: int = 20000,
    top_n: int = 15,
) -> dict[str, Any]:
    """Symbol counts per top-level source directory — shows which parts of
    the repo the graph actually represents (and, by omission, which it
    doesn't). Bounded sample so a huge graph stays scale-safe."""
    rows = (
        await session.execute(
            select(Node.data["location"]["file"].astext)
            .where(
                Node.project_id == project_id,
                Node.kind == "Symbol",
                Node.valid_to.is_(None),
            )
            .limit(sample_limit)
        )
    ).all()
    buckets: dict[str, int] = {}
    for (file_path,) in rows:
        rel = relative_source_path(file_path, root_prefix)
        if not rel:
            continue
        top = rel.split("/", 1)[0]
        buckets[top] = buckets.get(top, 0) + 1
    ranked = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "symbols_by_top_dir": dict(ranked[:top_n]),
        "sampled": len(rows),
        "sample_truncated": len(rows) >= sample_limit,
    }


async def _coverage_report(
    session: AsyncSession,
    project_id: uuid.UUID,
    project: Project,
    latest_run: dict[str, Any] | None,
    node_counts: dict[str, int],
    root_prefix: str | None,
) -> dict[str, Any]:
    """How much of the repository the graph actually covers, and where it is
    blind (eval doc Task 2). A C-dominant repo whose ``*:cpp`` stages all
    skipped must say ``partial`` + a critical gap — an agent reading only
    node counts cannot see that the dominant language is missing."""
    run_id = (latest_run or {}).get("id")
    language_stages: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    symbol_stage_by_lang: dict[str, dict[str, Any]] = {}
    agent_extract_ok: set[str] = set()
    if run_id:
        stage_rows = (
            await session.execute(
                select(AnalysisStage)
                .where(AnalysisStage.run_id == uuid.UUID(str(run_id)))
                .order_by(AnalysisStage.position)
            )
        ).scalars().all()
        for st in stage_rows:
            stats = st.stats or {}
            is_skipped = bool(stats.get("skipped"))
            reason = stats.get("reason")
            entry = {
                "stage": st.name,
                "language": st.language,
                "status": st.status,
                "skipped": is_skipped,
                "reason": reason,
                "items_done": st.items_done,
                "authoritative": stats.get("authoritative"),
                "incomplete": (
                    st.status not in {"completed"}
                    or stats.get("authoritative") is False
                ),
            }
            language_stages.append(entry)
            if is_skipped and st.name.startswith(_EXTRACTION_STAGE_PREFIXES):
                skipped.append(
                    {"stage": st.name, "language": st.language, "reason": reason}
                )
            if st.name.startswith("symbols:") and st.language:
                symbol_stage_by_lang[st.language] = entry
            if (
                st.name.startswith("agent_extract:")
                and st.language
                and not is_skipped
                and st.status == "completed"
                and stats.get("authoritative") is not False
            ):
                agent_extract_ok.add(st.language)

    critical_gaps: list[dict[str, Any]] = []
    for language in project.languages or []:
        entry = symbol_stage_by_lang.get(language)
        if entry is None:
            continue
        reason = str(entry.get("reason") or "")
        if (
            (entry["skipped"] or entry["incomplete"])
            and not reason.startswith(("covered_by:", "unchanged_source_content"))
            and language not in agent_extract_ok
        ):
            critical_gaps.append({
                "language": language,
                "reason": reason or "skipped",
                "impact": (
                    f"project language '{language}' is not represented in "
                    "the graph; answers about it would be ungrounded"
                ),
            })

    real_skips = [
        s for s in skipped
        if not str(s.get("reason") or "").startswith("covered_by:")
    ]
    if node_counts.get("Symbol", 0) == 0:
        status = "insufficient"
    elif critical_gaps or real_skips:
        status = "partial"
    else:
        status = "complete"

    recommendations: list[str] = []
    if critical_gaps:
        langs = ", ".join(sorted({g["language"] for g in critical_gaps}))
        recommendations.append(
            f"add a deterministic analyzer or enable agent extraction for: {langs}; "
            "until then, do not claim whole-repo understanding"
        )
    summary_quality = await _summary_quality(session, project_id)
    if summary_quality.get("stub_only"):
        recommendations.append(
            "optional AI narration is unavailable; use deterministic graph "
            "evidence directly or explicitly enable a provider when needed"
        )
    if run_id is None:
        recommendations.append("no analysis run found; run an analysis first")
    if not recommendations:
        recommendations.append(
            "coverage is complete for the registered languages; re-run "
            "analysis after significant commits to keep it fresh"
        )

    return {
        "status": status,
        "requested_languages": list(project.languages or []),
        "language_stages": language_stages,
        "skipped": skipped,
        "critical_gaps": critical_gaps,
        "graph_coverage": await _graph_path_buckets(
            session, project_id, root_prefix
        ),
        "summary_quality": summary_quality,
        "recommendation": recommendations,
    }


async def build_project_index(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    top_k: int = 10,
    max_serialized_bytes: int = DEFAULT_AGENT_CONTEXT_MAX_BYTES,
) -> dict[str, Any]:
    """Return a compact map of the project for coding agents.

    This is deliberately an index, not a dump. Agents should use the IDs here
    to request task packs or call MCP graph tools.
    """
    project = await _require_project(session, project_id)
    if project is None:
        return _apply_serialized_budget(
            {"schema": SCHEMA_PROJECT_INDEX, "error": "project_not_found"},
            max_serialized_bytes=max_serialized_bytes,
        )

    top_k = max(1, min(top_k, 100))
    diagnostic_completed_run = await _latest_run(
        session, project_id, statuses={"completed"}
    )
    active_run = await _latest_run(
        session, project_id, statuses={"queued", "running"}
    )
    try:
        graph_stamp = await read_graph_stamp(session, project_id=project_id)
    except (
        GraphHeadMissing,
        GraphHeadNeedsRebuild,
        GraphPublicationInvariantError,
    ) as exc:
        # The compact index remains available to explain how to create the
        # first trusted graph. It deliberately omits all live Node/Edge rows.
        return _apply_serialized_budget(
            {
                "schema": SCHEMA_PROJECT_INDEX,
                "generated_at": _now_iso(),
                "project": _project_ref(project),
                "analysis_snapshot": {
                    "latest_run": diagnostic_completed_run,
                    "last_completed_run": diagnostic_completed_run,
                    "active_run": active_run,
                    "snapshot_consistency": "unavailable",
                    "graph_queries_safe": False,
                    "diagnostic_only": True,
                    "graph_head_error": str(exc),
                    "graph_data_omitted": "no_atomic_publication",
                },
                "repair": {
                    "required": True,
                    "scope": "full",
                    "action": "publish one successful staged full source analysis",
                },
                "agent_contract": {
                    "role": "source_reference_and_analysis_guide",
                    "do_not_use_current_graph": True,
                    "allowed_before_repair": [
                        "inspect this diagnostic index",
                        "read_file with an explicit historical run_id",
                    ],
                },
                "truncation": {
                    "top_k": 0,
                    "reason": "graph_withheld_until_atomic_publication",
                },
            },
            max_serialized_bytes=max_serialized_bytes,
        )

    last_completed_run = await _run_by_id(
        session,
        project_id,
        graph_stamp.current_run_id,
    )
    if last_completed_run is None:
        return _apply_serialized_budget(
            {
                "schema": SCHEMA_PROJECT_INDEX,
                "error": "published_graph_run_missing",
                "retryable": False,
            },
            max_serialized_bytes=max_serialized_bytes,
        )
    snapshot_consistency = "refreshing" if active_run is not None else "stable"

    node_counts = await _node_kind_counts(session, project_id)
    edge_counts = await _edge_kind_counts(session, project_id)
    root_prefix = await project_root_prefix(session, project_id)
    contracts = await _nodes_by_kind(session, project_id, "Contract", top_k)
    await _annotate_contract_exposers(session, project_id, contracts)
    index = {
        "schema": SCHEMA_PROJECT_INDEX,
        "generated_at": _now_iso(),
        "project": _project_ref(project),
        "analysis_snapshot": {
            # Compatibility: ``latest_run`` now deliberately means the last
            # completed graph snapshot, never a queued SHA paired with old
            # graph counts.
            "latest_run": last_completed_run,
            "last_completed_run": last_completed_run,
            "active_run": active_run,
            "snapshot_consistency": snapshot_consistency,
            "graph_queries_safe": True,
            "diagnostic_only": False,
            "unpublished_refresh": None,
            "graph_publication": {
                "generation": graph_stamp.generation,
                "run_id": str(graph_stamp.current_run_id),
                "published_at": graph_stamp.published_at.isoformat(),
            },
            "node_counts": node_counts,
            "edge_counts": edge_counts,
            "certainty_breakdown": await _certainty_breakdown(session, project_id),
        },
        "coverage_report": await _coverage_report(
            session,
            project_id,
            project,
            last_completed_run,
            node_counts,
            root_prefix,
        ),
        "agent_contract": {
            "role": "source_reference_and_analysis_guide",
            "source_of_truth": "mnemos_graph",
            "graph_facts_are_not_an_ai_conclusion": True,
            "ai_summaries_are_optional_narration": True,
            "do_not_load_whole_repo": True,
            "do_not_treat_inferred_as_verified": True,
            "certainty_order": list(_CERTAINTY_ORDER),
            "claim_protocol": [
                "anchor claims to returned node/edge evidence IDs",
                "state coverage gaps before drawing absence conclusions",
                "treat inferred relationships as hypotheses to verify",
                "read the narrow source range before editing or quoting code",
                "run impact_analysis before changing a shared symbol or contract",
            ],
            "preferred_flow": [
                "get_project_index",
                "search_symbols or list_findings",
                "get_task_context_pack",
                "read_file only for narrow line windows",
                "impact_analysis before edits",
            ],
        },
        "entrypoints": {
            "contracts": contracts,
            "hot_symbols": await _top_symbols(session, project_id, top_k),
        },
        "data_map": {
            "entities": await _nodes_by_kind(session, project_id, "DataEntity", top_k),
        },
        "risk_queue": {
            "findings": await _risk_findings(session, project_id, limit=top_k),
        },
        "analysis_entrypoints": {
            "known_flows": await _known_flows(session, project_id, top_k),
            "search_hints": [
                {
                    "tool": "search_symbols",
                    "use_for": "free-text target discovery; lexical ranking plus optional embedding RRF",
                },
                {
                    "tool": "list_flows",
                    "use_for": "cross-tier process flow discovery before asking for level-4 summaries",
                },
                {
                    "tool": "impact_analysis",
                    "use_for": "blast radius, data access, tests, opaque components, runtime-exercised signal",
                },
            ],
        },
        "mcp_workflows": _MCP_WORKFLOWS,
        "truncation": {
            "top_k": top_k,
            "contracts_truncated": node_counts.get("Contract", 0) > top_k,
            "symbols_truncated": node_counts.get("Symbol", 0) > top_k,
            "data_entities_truncated": node_counts.get("DataEntity", 0) > top_k,
        },
    }
    _annotate_relative_files(index, root_prefix)
    try:
        await revalidate_graph_stamp(session, stamp=graph_stamp)
    except GraphGenerationChanged as exc:
        # PostgreSQL READ COMMITTED may cross a promotion between the many
        # bounded index queries. Never serialize that mixed materialization.
        return _apply_serialized_budget(
            {
                "schema": SCHEMA_PROJECT_INDEX,
                "error": "graph_snapshot_changed_retry",
                "reason": str(exc),
                "retryable": True,
                "snapshot": {
                    "generation": graph_stamp.generation,
                    "run_id": str(graph_stamp.current_run_id),
                },
            },
            max_serialized_bytes=max_serialized_bytes,
        )
    return _apply_serialized_budget(
        index, max_serialized_bytes=max_serialized_bytes
    )


async def _get_node(
    session: AsyncSession, project_id: uuid.UUID, node_id: str
) -> Node | None:
    return (
        await session.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == node_id,
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _get_edge(
    session: AsyncSession, project_id: uuid.UUID, edge_id: uuid.UUID
) -> Edge | None:
    return (
        await session.execute(
            select(Edge).where(
                Edge.project_id == project_id,
                Edge.id == edge_id,
                Edge.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _get_finding(
    session: AsyncSession, project_id: uuid.UUID, finding_id: uuid.UUID
) -> Finding | None:
    return (
        await session.execute(
            current_findings_select(project_id).where(Finding.id == finding_id)
        )
    ).scalar_one_or_none()


async def _current_summaries(
    session: AsyncSession, project_id: uuid.UUID, target_id: str
) -> tuple[list[dict[str, Any]], bool]:
    rows = (
        await session.execute(
            select(Summary)
            .join(GraphHead, GraphHead.project_id == Summary.project_id)
            .where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.superseded_by.is_(None),
                GraphHead.state == GRAPH_HEAD_READY,
                Summary.validated_graph_generation == GraphHead.generation,
                Summary.validated_overlay_generation
                == GraphHead.overlay_generation,
            )
            .order_by(Summary.level, Summary.generated_at.desc())
            .limit(_MAX_CURRENT_SUMMARIES + 1)
        )
    ).scalars().all()
    return (
        [_summary_ref(s) for s in rows[:_MAX_CURRENT_SUMMARIES]],
        len(rows) > _MAX_CURRENT_SUMMARIES,
    )


async def _call_edges(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    symbol_id: str,
    direction: str,
    limit: int,
) -> list[dict[str, Any]]:
    if direction == "callers":
        predicate = Edge.target_id == symbol_id
    else:
        predicate = Edge.source_id == symbol_id
    rows = (
        await session.execute(
            select(Edge)
            .where(
                Edge.project_id == project_id,
                Edge.kind == "CALLS",
                Edge.valid_to.is_(None),
                predicate,
            )
            .order_by(Edge.source_id, Edge.target_id)
            .limit(limit)
        )
    ).scalars().all()
    refs = await _edge_ref_map(session, project_id, list(rows))
    return [refs[edge.id] for edge in rows]


async def _data_access_edges(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    symbol_id: str | None = None,
    entity_id: str | None = None,
    limit: int,
) -> dict[str, Any]:
    clauses = [
        Edge.project_id == project_id,
        Edge.kind.in_(("READS", "WRITES")),
        Edge.valid_to.is_(None),
    ]
    if symbol_id is not None:
        clauses.append(Edge.source_id == symbol_id)
    if entity_id is not None:
        clauses.append(Edge.target_id == entity_id)
    rows = (
        await session.execute(
            select(Edge)
            .where(*clauses)
            .order_by(Edge.kind, Edge.source_id, Edge.target_id)
            .limit(limit)
        )
    ).scalars().all()
    refs = await _edge_ref_map(session, project_id, list(rows))
    reads = [refs[edge.id] for edge in rows if edge.kind == "READS"]
    writes = [refs[edge.id] for edge in rows if edge.kind == "WRITES"]
    return {
        "reads": reads,
        "writes": writes,
        "truncated": len(rows) >= limit,
    }


async def _contract_edges(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    contract_id: str,
    limit: int,
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Edge)
            .where(
                Edge.project_id == project_id,
                Edge.target_id == contract_id,
                Edge.kind.in_(("EXPOSES", "CALLS")),
                Edge.valid_to.is_(None),
            )
            .order_by(Edge.kind, Edge.source_id)
            .limit(limit)
        )
    ).scalars().all()
    refs = await _edge_ref_map(session, project_id, list(rows))
    exposers = [refs[edge.id] for edge in rows if edge.kind == "EXPOSES"]
    return {
        "exposers": exposers,
        "callers": [refs[edge.id] for edge in rows if edge.kind == "CALLS"],
        # A contract inferred from a client-side fetch literal with no
        # EXPOSES edge means the serving side is NOT in the graph (e.g. a
        # C server the analyzers didn't cover) — the agent must not treat
        # the client's view as the whole story.
        "server_exposer_missing": not exposers,
        "truncated": len(rows) >= limit,
    }


async def _annotate_contract_exposers(
    session: AsyncSession,
    project_id: uuid.UUID,
    contracts: list[dict[str, Any]],
) -> None:
    """Mark each contract ref with whether any symbol EXPOSES it. A
    client-inferred contract without a server exposer is flagged so agents
    don't mistake a fetch-literal guess for a mapped endpoint."""
    ids = [c.get("id") for c in contracts if c.get("id")]
    if not ids:
        return
    rows = (
        await session.execute(
            select(Edge.target_id)
            .where(
                Edge.project_id == project_id,
                Edge.target_id.in_(ids),
                Edge.kind == "EXPOSES",
                Edge.valid_to.is_(None),
            )
            .distinct()
        )
    ).all()
    exposed = {row[0] for row in rows}
    for c in contracts:
        c["has_exposer"] = c.get("id") in exposed
        if not c["has_exposer"] and c.get("certainty") == "inferred":
            c["warning"] = "client_inferred_no_server_exposer"


async def _summary_rollups(
    session: AsyncSession, project_id: uuid.UUID, target_id: str
) -> list[dict[str, Any]]:
    from app.mcp.queries import get_module_summary

    out = []
    for level in (1, 2, 3, 4):
        summary = await get_module_summary(
            session, project_id=project_id, target_id=target_id, level=level
        )
        if summary is not None:
            out.append(_agent_value(summary))
    return out


async def _intent_symbol_matches(
    session: AsyncSession,
    project_id: uuid.UUID,
    intent: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if not intent:
        return []
    from app.mcp.queries import search_symbols

    return _agent_value(
        await search_symbols(
            session,
            project_id=project_id,
            query=intent,
            top_k=max(1, min(limit, 20)),
        )
    )


async def _symbol_mcp_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    symbol_id: str,
    *,
    intent: str | None,
    limit: int,
) -> dict[str, Any]:
    from app.mcp.queries import (
        find_callees,
        find_callers,
        get_data_access,
        get_symbol,
    )

    return {
        "symbol": _agent_value(
            await get_symbol(session, project_id=project_id, symbol_id=symbol_id)
        ),
        "transitive_callers": _agent_value(
            await find_callers(
                session,
                project_id=project_id,
                symbol_id=symbol_id,
                transitive=True,
                limit=limit,
            )
        ),
        "transitive_callees": _agent_value(
            await find_callees(
                session,
                project_id=project_id,
                symbol_id=symbol_id,
                transitive=True,
                limit=limit,
            )
        ),
        "data_access": _agent_value(
            await get_data_access(
                session, project_id=project_id, symbol_id=symbol_id, limit=limit
            )
        ),
        "intent_symbol_matches": await _intent_symbol_matches(
            session, project_id, intent, limit
        ),
    }


async def _contract_mcp_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    contract_id: str,
) -> dict[str, Any]:
    from app.mcp.queries import find_runtime_path, get_contract

    return {
        "contract": _agent_value(
            await get_contract(session, project_id=project_id, contract_id=contract_id)
        ),
        "runtime_path": _agent_value(
            await find_runtime_path(
                session, project_id=project_id, entry_contract_id=contract_id
            )
        ),
    }


async def _data_entity_mcp_context(
    session: AsyncSession,
    project_id: uuid.UUID,
    entity_id: str,
) -> dict[str, Any]:
    from app.mcp.data_queries import get_data_entity

    return {
        "data_entity": _agent_value(
            await get_data_entity(session, project_id=project_id, entity_id=entity_id)
        )
    }


def _next_queries_for_node(
    node: Node,
    intent: str | None,
    *,
    data: dict[str, Any],
) -> list[dict[str, Any]]:
    if node.kind == "Symbol":
        return [
            {"tool": "get_symbol", "args": {"symbol_id": node.id}},
            {"tool": "find_callers", "args": {"symbol_id": node.id, "transitive": True}},
            {"tool": "find_callees", "args": {"symbol_id": node.id, "transitive": True}},
            {"tool": "get_data_access", "args": {"symbol_id": node.id}},
            {"tool": "impact_analysis", "args": {"symbol_id": node.id}},
        ]
    if node.kind == "Contract":
        return [
            {"tool": "get_contract", "args": {"contract_id": node.id}},
            {"tool": "find_runtime_path", "args": {"entry_contract_id": node.id}},
        ]
    if node.kind == "DataEntity":
        return [
            {"tool": "get_data_entity", "args": {"entity_id": node.id}},
            {"tool": "get_sample_data", "args": {"entity_id": node.id, "limit": 5}},
        ]
    return [
        {
            "tool": "search_symbols",
            "args": {"query": data.get("name") or node.id},
        }
    ]


def _pack_header(
    project: Project,
    project_id: uuid.UUID,
    target_id: str,
    target_kind: str,
    intent: str | None,
    budget_items: int,
) -> dict[str, Any]:
    return {
        "schema": SCHEMA_TASK_PACK,
        "generated_at": _now_iso(),
        "project_id": str(project_id),
        "project": _project_ref(project),
        "target": {
            "id": target_id,
            "kind": target_kind,
            "intent": intent,
        },
        "budget": {
            "max_items_per_section": budget_items,
            "raw_source_included": False,
        },
        "rules": {
            "role": "source_reference_and_analysis_guide",
            "source_of_truth": "mnemos_graph",
            "read_source_only_after_identifying_file_ranges": True,
            "preserve_certainty": True,
            "certainty_order": list(_CERTAINTY_ORDER),
            "answer_protocol": [
                "separate verified/asserted facts from inferred hypotheses",
                "cite evidence IDs and file/line locations for material claims",
                "mention truncation or coverage gaps that limit the answer",
                "verify the narrow source window before proposing an edit",
            ],
            "summaries": "optional_narration_not_source_truth",
        },
    }


async def build_task_context_pack(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    target_id: str,
    target_kind: str = "auto",
    intent: str | None = None,
    budget_items: int = 50,
    max_serialized_bytes: int = DEFAULT_AGENT_CONTEXT_MAX_BYTES,
) -> dict[str, Any]:
    """Return the bounded context a coding agent needs for one task."""
    budget_items = max(5, min(int(budget_items), 200))
    project = await _require_project(session, project_id)
    if project is None:
        return _apply_serialized_budget(
            {"schema": SCHEMA_TASK_PACK, "error": "project_not_found"},
            max_serialized_bytes=max_serialized_bytes,
        )

    resolved_kind = target_kind
    node: Node | None = None
    edge: Edge | None = None
    finding: Finding | None = None

    if target_kind in ("auto", "node", "symbol", "contract", "data_entity"):
        node = await _get_node(session, project_id, target_id)
        if node is not None:
            resolved_kind = node.kind

    if node is None and target_kind in ("auto", "edge"):
        try:
            edge = await _get_edge(session, project_id, uuid.UUID(target_id))
        except ValueError:
            edge = None
        if edge is not None:
            resolved_kind = "Edge"

    if node is None and edge is None and target_kind in ("auto", "finding"):
        try:
            finding = await _get_finding(session, project_id, uuid.UUID(target_id))
        except ValueError:
            finding = None
        if finding is not None:
            resolved_kind = "Finding"

    pack = _pack_header(project, project_id, target_id, resolved_kind, intent, budget_items)
    pack["budget"]["max_serialized_bytes"] = _normalise_serialized_budget(
        max_serialized_bytes
    )
    if node is None and edge is None and finding is None:
        pack["error"] = "target_not_found"
        pack["next_mcp_queries"] = [
            {"tool": "search_symbols", "args": {"query": target_id}},
            {"tool": "list_findings", "args": {"status": "open"}},
        ]
        return _apply_serialized_budget(
            pack, max_serialized_bytes=max_serialized_bytes
        )

    evidence_refs: list[dict[str, Any]] = []

    if node is not None:
        node_refs = await _node_ref_map(session, project_id, [node])
        pack["target_node"] = node_refs[node.id]
        evidence_refs.append(pack["target_node"]["evidence"])
        pack["summaries"], summaries_truncated = await _current_summaries(
            session, project_id, node.id
        )
        if summaries_truncated:
            pack.setdefault("truncation", {})["summaries"] = {
                "kept": _MAX_CURRENT_SUMMARIES,
                "more_available": True,
            }
        pack["summary_rollups"] = await _summary_rollups(session, project_id, node.id)
        pack["related_findings"] = await _risk_findings(
            session, project_id, limit=budget_items, subject_node_id=node.id
        )
        if node.kind == "Symbol":
            pack["precomputed_mcp_context"] = await _symbol_mcp_context(
                session,
                project_id,
                node.id,
                intent=intent,
                limit=budget_items,
            )
            callers = await _call_edges(
                session, project_id, symbol_id=node.id, direction="callers",
                limit=budget_items,
            )
            callees = await _call_edges(
                session, project_id, symbol_id=node.id, direction="callees",
                limit=budget_items,
            )
            data_access = await _data_access_edges(
                session, project_id, symbol_id=node.id, limit=budget_items
            )
            pack["graph_slice"] = {
                "callers": callers,
                "callees": callees,
                "data_access": data_access,
                "truncated": {
                    "callers": len(callers) >= budget_items,
                    "callees": len(callees) >= budget_items,
                    "data_access": data_access["truncated"],
                },
            }
            from app.mcp.queries import impact_analysis

            pack["impact"] = _agent_value(
                await impact_analysis(
                    session, project_id=project_id, symbol_id=node.id
                )
            )
        elif node.kind == "Contract":
            pack["precomputed_mcp_context"] = await _contract_mcp_context(
                session, project_id, node.id
            )
            contract = await _contract_edges(
                session, project_id, contract_id=node.id, limit=budget_items
            )
            pack["graph_slice"] = {"contract": contract}
        elif node.kind == "DataEntity":
            pack["precomputed_mcp_context"] = await _data_entity_mcp_context(
                session, project_id, node.id
            )
            pack["graph_slice"] = {
                "accessors": await _data_access_edges(
                    session, project_id, entity_id=node.id, limit=budget_items
                )
            }
        else:
            pack["graph_slice"] = {}
        pack["next_mcp_queries"] = _next_queries_for_node(
            node,
            intent,
            data=pack["target_node"]["data"],
        )

    if edge is not None:
        edge_refs = await _edge_ref_map(session, project_id, [edge])
        pack["target_edge"] = edge_refs[edge.id]
        evidence_refs.append(pack["target_edge"]["evidence"])
        endpoint_nodes = [
            endpoint
            for endpoint in (
                await _get_node(session, project_id, edge.source_id),
                await _get_node(session, project_id, edge.target_id),
            )
            if endpoint is not None
        ]
        endpoint_refs = await _node_ref_map(session, project_id, endpoint_nodes)
        pack["endpoints"] = {
            "source": endpoint_refs.get(edge.source_id),
            "target": endpoint_refs.get(edge.target_id),
        }
        pack["related_findings"] = await _risk_findings(
            session, project_id, limit=budget_items, subject_edge_id=edge.id
        )
        pack["next_mcp_queries"] = [
            {"tool": "get_task_context_pack", "args": {"target_id": edge.source_id}},
            {"tool": "get_task_context_pack", "args": {"target_id": edge.target_id}},
        ]

    if finding is not None:
        pack["finding"] = _finding_ref(finding)
        subject_node = (
            await _get_node(session, project_id, finding.subject_node_id)
            if finding.subject_node_id
            else None
        )
        subject_edge = (
            await _get_edge(session, project_id, finding.subject_edge_id)
            if finding.subject_edge_id
            else None
        )
        subject_node_refs = await _node_ref_map(
            session,
            project_id,
            [subject_node] if subject_node is not None else [],
        )
        subject_edge_refs = await _edge_ref_map(
            session,
            project_id,
            [subject_edge] if subject_edge is not None else [],
        )
        pack["subject"] = {
            "node": (
                subject_node_refs.get(subject_node.id)
                if subject_node is not None
                else None
            ),
            "edge": (
                subject_edge_refs.get(subject_edge.id)
                if subject_edge is not None
                else None
            ),
        }
        if subject_node is not None:
            evidence_refs.append(pack["subject"]["node"]["evidence"])
        if subject_edge is not None:
            evidence_refs.append(pack["subject"]["edge"]["evidence"])
        if subject_node is not None and subject_node.kind == "Symbol":
            pack["precomputed_mcp_context"] = await _symbol_mcp_context(
                session,
                project_id,
                subject_node.id,
                intent=intent,
                limit=budget_items,
            )
            pack["summary_rollups"] = await _summary_rollups(
                session, project_id, subject_node.id
            )
            callers = await _call_edges(
                session,
                project_id,
                symbol_id=subject_node.id,
                direction="callers",
                limit=budget_items,
            )
            callees = await _call_edges(
                session,
                project_id,
                symbol_id=subject_node.id,
                direction="callees",
                limit=budget_items,
            )
            data_access = await _data_access_edges(
                session, project_id, symbol_id=subject_node.id, limit=budget_items
            )
            pack["graph_slice"] = {
                "subject": pack["subject"],
                "callers": callers,
                "callees": callees,
                "data_access": data_access,
                "truncated": {
                    "callers": len(callers) >= budget_items,
                    "callees": len(callees) >= budget_items,
                    "data_access": data_access["truncated"],
                },
            }
            from app.mcp.queries import impact_analysis

            pack["impact"] = _agent_value(
                await impact_analysis(
                    session, project_id=project_id, symbol_id=subject_node.id
                )
            )
        else:
            pack["graph_slice"] = {"subject": pack["subject"]}
        pack["next_mcp_queries"] = [
            {
                "tool": "get_task_context_pack",
                "args": {"target_id": finding.subject_node_id},
            }
            if finding.subject_node_id
            else {"tool": "list_findings", "args": {"status": "open"}},
            {
                "tool": "submit_plan",
                "args": {
                    "spec": {
                        "finding_id": str(finding.id),
                        "kind": finding.kind,
                        "remediation": finding.remediation,
                    }
                },
            },
        ]

    # Collect evidence from graph slices without making agents parse every
    # section to find what facts can be trusted.
    for section in ("graph_slice",):
        value = pack.get(section)
        if isinstance(value, dict):
            stack: list[Any] = [value]
            while stack:
                cur = stack.pop()
                if isinstance(cur, dict):
                    ev = cur.get("evidence")
                    if isinstance(ev, dict):
                        evidence_refs.append(ev)
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(cur)

    seen: set[tuple[str, str]] = set()
    deduped = []
    for ev in evidence_refs:
        key = (str(ev.get("type")), str(ev.get("id")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(ev)
    pack["evidence_refs"] = deduped
    _annotate_relative_files(
        pack, await project_root_prefix(session, project_id)
    )
    return _apply_serialized_budget(
        pack, max_serialized_bytes=max_serialized_bytes
    )
