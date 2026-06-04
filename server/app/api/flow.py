"""PR-143 — cross-tier flow / process analysis endpoint.

``POST /api/v1/projects/{id}/trace_flow`` traces one process end-to-end
across the tiers whose source files the operator points at (frontend →
backend → database), via the Claude Code subscription, and returns the
structured trace: ordered steps, the signal crossing each boundary with
its fields, every flag value and its meaning, and the rows touched. The
result is persisted as a level-4 Summary so it joins the knowledge graph.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.db import get_session
from app.extractor.agent_flow import (
    FLOW_LEVEL,
    analyze_flow_via_agent_sdk,
    is_agent_sdk_available,
)
from app.models.findings import Summary
from app.models.graph import Edge, Node

router = APIRouter(
    prefix="/api/v1/projects/{project_id}",
    tags=["flow"],
    dependencies=[Depends(require_project_org())],
)

# Path/extension → (tier, language) heuristics. The tier hint in the path
# wins (frontend/backend/db dirs); otherwise the extension decides.
_FRONTEND_EXT = {".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
_DB_EXT = {".sql", ".ddl"}
_LANG_BY_EXT = {
    ".py": "python", ".cs": "csharp", ".java": "java", ".go": "go",
    ".rb": "ruby", ".php": "php", ".rs": "rust", ".ts": "typescript",
    ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".sql": "sql", ".ddl": "sql", ".cpp": "cpp", ".cc": "cpp", ".h": "cpp",
}


def _classify(path: Path) -> tuple[str, str]:
    lowered = {p.lower() for p in path.parts}
    ext = path.suffix.lower()
    lang = _LANG_BY_EXT.get(ext, "unknown")
    if ext in _DB_EXT or {"db", "database", "sql", "schema", "migrations"} & lowered:
        return "database", "sql"
    if ext in _FRONTEND_EXT or {"frontend", "front", "ui", "client", "web"} & lowered:
        return "frontend", lang
    return "backend", lang


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "flow"


class TraceFlowRequest(BaseModel):
    entry: str = Field(min_length=1, max_length=300, description="Process to trace")
    source_paths: list[str] = Field(
        min_length=1, max_length=20,
        description="Absolute paths of the FE/BE/DB files involved in the flow",
    )
    persist: bool = Field(default=True)
    max_file_bytes: int = Field(default=60_000, ge=1, le=400_000)


@router.post("/trace_flow", dependencies=[Depends(require_operator)])
async def trace_flow(
    project_id: uuid.UUID,
    body: TraceFlowRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    if not is_agent_sdk_available():
        raise HTTPException(status_code=503, detail="agent_sdk_unavailable")

    sources: list[dict] = []
    skipped: list[str] = []
    for raw in body.source_paths:
        p = Path(raw)
        if not p.is_file():
            skipped.append(raw)
            continue
        try:
            code = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            skipped.append(raw)
            continue
        if len(code) > body.max_file_bytes:
            code = code[: body.max_file_bytes]
        tier, lang = _classify(p)
        sources.append({"tier": tier, "language": lang, "label": p.name, "code": code})

    if not sources:
        raise HTTPException(status_code=400, detail="no_readable_source_paths")

    return await _analyze_and_persist(
        db, project_id, user, body.entry, sources, body.persist, skipped
    )


async def _read_source(path: Path, max_bytes: int) -> dict | None:
    if not path.is_file():
        return None
    try:
        code = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if len(code) > max_bytes:
        code = code[:max_bytes]
    tier, lang = _classify(path)
    return {"tier": tier, "language": lang, "label": path.name, "code": code}


async def _gather_files_from_graph(
    db: AsyncSession, project_id: uuid.UUID, entry: str, max_files: int
) -> list[str]:
    """Resolve an entry to the set of relative source files involved in the
    flow, using the knowledge graph: symbols whose name/id/signature match
    the entry (any tier — so a query like "place order" pulls the frontend
    ``placeOrder`` and the backend ``handle_create_order`` alike), plus the
    nodes one CALLS/READS/WRITES hop away (which reaches the DataEntity =
    SQL-schema files via data-access edges). Returns distinct ``data.file``
    values, newest/most-relevant first, capped at ``max_files``."""
    terms = [t for t in re.split(r"[^a-z0-9]+", entry.lower()) if len(t) >= 3]
    if not terms:
        return []
    conds = []
    for t in terms:
        like = f"%{t}%"
        conds.append(Node.data["name"].astext.ilike(like))
        conds.append(Node.data["id"].astext.ilike(like))
        conds.append(Node.data["signature"].astext.ilike(like))
    seeds = (
        await db.execute(
            select(Node)
            .where(
                Node.project_id == project_id,
                Node.valid_to.is_(None),
                or_(*conds),
            )
            .limit(20)
        )
    ).scalars().all()
    seed_ids = [n.id for n in seeds]
    node_by_id = {n.id: n for n in seeds}

    # One hop out along any edge (CALLS / CONTAINS / READS / WRITES) so the
    # tables a handler touches (and its immediate callers/callees) come too.
    if seed_ids:
        edges = (
            await db.execute(
                select(Edge).where(
                    Edge.project_id == project_id,
                    Edge.valid_to.is_(None),
                    or_(Edge.source_id.in_(seed_ids), Edge.target_id.in_(seed_ids)),
                ).limit(200)
            )
        ).scalars().all()
        neighbour_ids = {e.source_id for e in edges} | {e.target_id for e in edges}
        neighbour_ids -= set(seed_ids)
        if neighbour_ids:
            extra = (
                await db.execute(
                    select(Node).where(
                        Node.project_id == project_id,
                        Node.valid_to.is_(None),
                        Node.id.in_(neighbour_ids),
                    )
                )
            ).scalars().all()
            for n in extra:
                node_by_id.setdefault(n.id, n)

    files: list[str] = []
    for n in node_by_id.values():
        f = (n.data or {}).get("file")
        if f and f not in files:
            files.append(f)
    return files[:max_files]


async def _analyze_and_persist(
    db: AsyncSession,
    project_id: uuid.UUID,
    user,  # noqa: ANN001
    entry: str,
    sources: list[dict],
    persist: bool,
    skipped: list[str],
) -> dict:
    flow = await analyze_flow_via_agent_sdk(entry=entry, sources=sources)
    if flow is None:
        raise HTTPException(status_code=502, detail="flow_analysis_failed")

    summary_id: str | None = None
    if persist:
        claims = [
            {"section": "steps", "data": flow["steps"]},
            {"section": "flags", "data": flow["flags"]},
            {"section": "data_touched", "data": flow["data_touched"]},
        ]
        row = Summary(
            project_id=project_id,
            target_id=f"flow:{_slug(entry)}",
            level=FLOW_LEVEL,
            summary=flow["summary"],
            detailed=flow["detailed"],
            claims=claims,
            open_questions=flow["open_questions"],
            model_used="claude_code",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        summary_id = str(row.id)

    await audit_record(
        actor=f"user:{user.id}",
        action="flow.trace",
        target=entry,
        project_id=project_id,
        details={
            "tiers": sorted({s["tier"] for s in sources}),
            "files": len(sources),
            "steps": len(flow["steps"]),
            "flags": len(flow["flags"]),
        },
    )
    return {
        "entry": entry,
        "tiers_analyzed": sorted({s["tier"] for s in sources}),
        "files_analyzed": [s["label"] for s in sources],
        "skipped_paths": skipped,
        "summary_id": summary_id,
        "flow": flow,
    }


class TraceFlowAutoRequest(BaseModel):
    entry: str = Field(min_length=1, max_length=300, description="Process to trace")
    source_root: str = Field(
        min_length=1,
        description="Absolute repo root; graph-resolved relative files join to it",
    )
    persist: bool = Field(default=True)
    max_files: int = Field(default=8, ge=1, le=30)
    max_file_bytes: int = Field(default=60_000, ge=1, le=400_000)


@router.post("/trace_flow/auto", dependencies=[Depends(require_operator)])
async def trace_flow_auto(
    project_id: uuid.UUID,
    body: TraceFlowAutoRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Trace a process from just an entry point: the relevant FE/BE/DB
    source files are gathered from the knowledge graph (PR-147), so the
    operator no longer has to know which files implement the flow."""
    if not is_agent_sdk_available():
        raise HTTPException(status_code=503, detail="agent_sdk_unavailable")

    rel_files = await _gather_files_from_graph(
        db, project_id, body.entry, body.max_files
    )
    if not rel_files:
        raise HTTPException(status_code=404, detail="no_graph_files_for_entry")

    root = Path(body.source_root)
    sources: list[dict] = []
    skipped: list[str] = []
    for rel in rel_files:
        src = await _read_source(root / rel, body.max_file_bytes)
        (sources.append(src) if src else skipped.append(rel))

    if not sources:
        raise HTTPException(status_code=400, detail="no_readable_source_paths")

    result = await _analyze_and_persist(
        db, project_id, user, body.entry, sources, body.persist, skipped
    )
    result["auto_collected_files"] = rel_files
    return result
