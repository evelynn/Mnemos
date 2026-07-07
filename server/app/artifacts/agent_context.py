"""Machine-readable context artifacts for coding agents.

Mnemos stores the full source-analysis graph in the database. Claude Code and
Codex should not receive that whole graph in one prompt; they should receive a
small project index, then ask for bounded task packs as they work. This module
builds those two artifacts from the current graph snapshot.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.mcp.queries import project_root_prefix, relative_source_path
from app.models.findings import Finding, Summary
from app.models.graph import AnalysisRun, Edge, Node
from app.models.projects import Project
from app.models.stages import AnalysisStage

SCHEMA_PROJECT_INDEX = "mnemos.agent.project_index.v1"
SCHEMA_TASK_PACK = "mnemos.agent.task_context_pack.v1"

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
_MAX_DATA_LIST_ITEMS = 50
_MAX_DATA_STRING_CHARS = 512
_MAX_SIGNATURE_CHARS = 240
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
        items = list(value.items())
        for child_key, child_value in items[:_MAX_DATA_KEYS]:
            out[str(child_key)] = _bounded_value(
                child_value, key=str(child_key), depth=depth + 1
            )
        if len(items) > _MAX_DATA_KEYS:
            out["__truncated_keys__"] = len(items) - _MAX_DATA_KEYS
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
    source = dict(data or {})
    if "signature" in source:
        source["signature"] = _signature_ref(source.get("signature"))
    bounded = _bounded_value(source, depth=0)
    return bounded if isinstance(bounded, dict) else {}


def _agent_value(value: Any) -> Any:
    return _bounded_value(value, depth=0)


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


def _node_ref(node: Node | None) -> dict[str, Any] | None:
    if node is None:
        return None
    data = node.data or {}
    safe_data = _agent_data(data)
    return {
        "id": node.id,
        "kind": node.kind,
        "name": data.get("name"),
        "signature": _signature_ref(data.get("signature")),
        "location": data.get("location"),
        "component_id": data.get("component_id"),
        "certainty": node.certainty,
        "created_by": list(node.created_by or []),
        "data": safe_data,
        "evidence": {
            "type": "node",
            "id": node.id,
            "valid_from": _iso(node.valid_from),
        },
    }


def _edge_ref(edge: Edge) -> dict[str, Any]:
    data = edge.data or {}
    return {
        "edge_id": str(edge.id),
        "kind": edge.kind,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "certainty": edge.certainty,
        "created_by": list(edge.created_by or []),
        "exercised": str(data.get("exercised", "")).lower() == "true",
        "site": data.get("invocation_site") or data.get("access_site"),
        "data": _agent_data(data),
        "evidence": {
            "type": "edge",
            "id": str(edge.id),
            "valid_from": _iso(edge.valid_from),
        },
    }


def _project_ref(project: Project) -> dict[str, Any]:
    return {
        "id": str(project.id),
        "name": project.name,
        "gitlab_project_id": project.gitlab_project_id,
        "gitlab_url": project.gitlab_url,
        "default_branch": project.default_branch,
        "languages": list(project.languages or []),
    }


def _finding_ref(finding: Finding) -> dict[str, Any]:
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
        "remediation": finding.remediation,
        "cwe_id": finding.cwe_id,
        "first_seen_at": _iso(finding.first_seen_at),
        "last_seen_at": _iso(finding.last_seen_at),
    }


def _summary_ref(summary: Summary) -> dict[str, Any]:
    return {
        "target_id": summary.target_id,
        "level": summary.level,
        "summary": summary.summary,
        "detailed": summary.detailed,
        "claims": summary.claims or [],
        "open_questions": summary.open_questions or [],
        "model_used": summary.model_used,
        "fallback_reason": summary.fallback_reason,
        "generated_at": _iso(summary.generated_at),
    }


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
    return {
        "nodes": {k: int(v) for k, v in node_rows},
        "edges": {k: int(v) for k, v in edge_rows},
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
    session: AsyncSession, project_id: uuid.UUID
) -> dict[str, Any] | None:
    run = (
        await session.execute(
            select(AnalysisRun)
            .where(AnalysisRun.project_id == project_id)
            .order_by(AnalysisRun.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None:
        return None
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
                Node.id,
                Node.kind,
                Node.data,
                Node.certainty,
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
    out = []
    for node_id, kind, data, certainty, incoming_calls in rows:
        data = data or {}
        out.append(
            {
                "symbol_id": node_id,
                "kind": kind,
                "name": data.get("name"),
                "signature": _signature_ref(data.get("signature")),
                "location": data.get("location"),
                "component_id": data.get("component_id"),
                "certainty": certainty,
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
    return [_node_ref(n) or {} for n in rows]


async def _risk_findings(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    limit: int,
    subject_node_id: str | None = None,
    subject_edge_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    stmt = (
        select(Finding)
        .where(Finding.project_id == project_id)
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
    gains ``location.relative_file`` — one stable project-relative path even
    though the analyzers disagree on path style (eval doc Task 3). The
    original absolute path is kept untouched."""
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
                loc["relative_file"] = relative_source_path(
                    loc.get("file"), root_prefix
                )
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
            .where(
                Summary.project_id == project_id,
                Summary.superseded_by.is_(None),
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
                and st.status in ("completed", "partial")
            ):
                agent_extract_ok.add(st.language)

    critical_gaps: list[dict[str, Any]] = []
    for language in project.languages or []:
        entry = symbol_stage_by_lang.get(language)
        if entry is None:
            continue
        reason = str(entry.get("reason") or "")
        if (
            entry["skipped"]
            and not reason.startswith("covered_by:")
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
            "configure an LLM backend so L1-L3 summaries carry real "
            "behavioural narratives instead of structural stubs"
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
    top_k: int = 25,
) -> dict[str, Any]:
    """Return a compact map of the project for coding agents.

    This is deliberately an index, not a dump. Agents should use the IDs here
    to request task packs or call MCP graph tools.
    """
    project = await _require_project(session, project_id)
    if project is None:
        return {"schema": SCHEMA_PROJECT_INDEX, "error": "project_not_found"}

    top_k = max(1, min(top_k, 100))
    node_counts = await _node_kind_counts(session, project_id)
    edge_counts = await _edge_kind_counts(session, project_id)
    latest_run = await _latest_run(session, project_id)
    root_prefix = await project_root_prefix(session, project_id)
    contracts = await _nodes_by_kind(session, project_id, "Contract", top_k)
    await _annotate_contract_exposers(session, project_id, contracts)
    index = {
        "schema": SCHEMA_PROJECT_INDEX,
        "generated_at": _now_iso(),
        "project": _project_ref(project),
        "analysis_snapshot": {
            "latest_run": latest_run,
            "node_counts": node_counts,
            "edge_counts": edge_counts,
            "certainty_breakdown": await _certainty_breakdown(session, project_id),
        },
        "coverage_report": await _coverage_report(
            session, project_id, project, latest_run, node_counts, root_prefix
        ),
        "agent_contract": {
            "source_of_truth": "mnemos_graph",
            "do_not_load_whole_repo": True,
            "do_not_treat_inferred_as_verified": True,
            "certainty_order": list(_CERTAINTY_ORDER),
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
    return index


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
            select(Finding).where(
                Finding.project_id == project_id,
                Finding.id == finding_id,
            )
        )
    ).scalar_one_or_none()


async def _current_summaries(
    session: AsyncSession, project_id: uuid.UUID, target_id: str
) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(Summary)
            .where(
                Summary.project_id == project_id,
                Summary.target_id == target_id,
                Summary.superseded_by.is_(None),
            )
            .order_by(Summary.level, Summary.generated_at.desc())
        )
    ).scalars().all()
    return [_summary_ref(s) for s in rows]


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
    return [_edge_ref(e) for e in rows]


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
    reads = [_edge_ref(e) for e in rows if e.kind == "READS"]
    writes = [_edge_ref(e) for e in rows if e.kind == "WRITES"]
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
    exposers = [_edge_ref(e) for e in rows if e.kind == "EXPOSES"]
    return {
        "exposers": exposers,
        "callers": [_edge_ref(e) for e in rows if e.kind == "CALLS"],
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


def _next_queries_for_node(node: Node, intent: str | None) -> list[dict[str, Any]]:
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
            "args": {"query": (node.data or {}).get("name") or node.id},
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
            "source_of_truth": "mnemos_graph",
            "read_source_only_after_identifying_file_ranges": True,
            "preserve_certainty": True,
            "certainty_order": list(_CERTAINTY_ORDER),
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
) -> dict[str, Any]:
    """Return the bounded context a coding agent needs for one task."""
    budget_items = max(5, min(int(budget_items), 200))
    project = await _require_project(session, project_id)
    if project is None:
        return {"schema": SCHEMA_TASK_PACK, "error": "project_not_found"}

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
    if node is None and edge is None and finding is None:
        pack["error"] = "target_not_found"
        pack["next_mcp_queries"] = [
            {"tool": "search_symbols", "args": {"query": target_id}},
            {"tool": "list_findings", "args": {"status": "open"}},
        ]
        return pack

    evidence_refs: list[dict[str, Any]] = []

    if node is not None:
        pack["target_node"] = _node_ref(node)
        evidence_refs.append(pack["target_node"]["evidence"])
        pack["summaries"] = await _current_summaries(session, project_id, node.id)
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

            pack["impact"] = await impact_analysis(
                session, project_id=project_id, symbol_id=node.id
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
        pack["next_mcp_queries"] = _next_queries_for_node(node, intent)

    if edge is not None:
        pack["target_edge"] = _edge_ref(edge)
        evidence_refs.append(pack["target_edge"]["evidence"])
        pack["endpoints"] = {
            "source": _node_ref(await _get_node(session, project_id, edge.source_id)),
            "target": _node_ref(await _get_node(session, project_id, edge.target_id)),
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
        pack["subject"] = {
            "node": _node_ref(subject_node),
            "edge": _edge_ref(subject_edge) if subject_edge else None,
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

            pack["impact"] = await impact_analysis(
                session, project_id=project_id, symbol_id=subject_node.id
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
    return pack
