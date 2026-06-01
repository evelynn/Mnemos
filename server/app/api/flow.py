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

    flow = await analyze_flow_via_agent_sdk(entry=body.entry, sources=sources)
    if flow is None:
        raise HTTPException(status_code=502, detail="flow_analysis_failed")

    summary_id: str | None = None
    if body.persist:
        target_id = f"flow:{_slug(body.entry)}"
        claims = [
            {"section": "steps", "data": flow["steps"]},
            {"section": "flags", "data": flow["flags"]},
            {"section": "data_touched", "data": flow["data_touched"]},
        ]
        row = Summary(
            project_id=project_id,
            target_id=target_id,
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
        target=body.entry,
        project_id=project_id,
        details={
            "tiers": sorted({s["tier"] for s in sources}),
            "files": len(sources),
            "steps": len(flow["steps"]),
            "flags": len(flow["flags"]),
        },
    )

    return {
        "entry": body.entry,
        "tiers_analyzed": sorted({s["tier"] for s in sources}),
        "files_analyzed": [s["label"] for s in sources],
        "skipped_paths": skipped,
        "summary_id": summary_id,
        "flow": flow,
    }
