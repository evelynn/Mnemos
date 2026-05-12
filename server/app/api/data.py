"""Data entity + sample browsing API (spec §8, §11.4)."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_operator
from app.data_sampler import MaskingEngine, compute_column_stats, mask_rows
from app.data_sampler.project_db import (
    PolicyViolation,
    enforce_policy,
    requires_awr,
    resolve_project_db,
)
from app.db import get_session
from app.models.auth import User
from app.models.graph import Node
from app.models.samples import DataQueryLog, DataSample
from app.safety.dialects import SUPPORTED_DIALECTS, is_supported
from app.safety.ratelimit import actor_key, enforce as rl_enforce
from app.safety.sql_limit import (
    DEFAULT_MAX_ROWS,
    WriteStatementBlocked,
    enforce_limit,
)

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/data_entities",
    tags=["data"],
    dependencies=[Depends(require_project_org())],
)


@router.get("")
async def list_data_entities(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalars().all()
    return [
        {
            "id": r.id,
            "name": (r.data or {}).get("name"),
            "kind": (r.data or {}).get("kind"),
            "component_id": (r.data or {}).get("component_id"),
            "is_sensitive": (r.data or {}).get("is_sensitive", False),
            "certainty": r.certainty,
        }
        for r in rows
    ]


@router.get("/{entity_id:path}")
async def get_data_entity(
    project_id: uuid.UUID,
    entity_id: str,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    node = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail="not_found")

    latest_sample = (
        await db.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    return {
        "id": node.id,
        "data": node.data,
        "certainty": node.certainty,
        "sample_available": latest_sample is not None,
        "is_sensitive": (node.data or {}).get("is_sensitive", False),
    }


class SampleIngest(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    row_count_estimate: int | None = None


@router.get("/{entity_id:path}/sample")
async def get_sample(
    project_id: uuid.UUID,
    entity_id: str,
    user: CurrentUser,
    limit: int = 10,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    limit = max(1, min(limit, 100))
    node = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if node is None:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if (node.data or {}).get("is_sensitive"):
        raise HTTPException(status_code=403, detail="sensitive_entity_sample_disallowed")

    sample = (
        await db.execute(
            select(DataSample)
            .where(
                DataSample.project_id == project_id,
                DataSample.data_entity_id == entity_id,
            )
            .order_by(DataSample.sampled_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sample is None:
        raise HTTPException(status_code=404, detail="no_sample_yet")

    rows = sample.sample_rows[:limit] if isinstance(sample.sample_rows, list) else []
    await audit_record(
        actor=f"user:{user.id}",
        action="data.sample_view",
        target=entity_id,
        project_id=project_id,
        details={"limit": limit},
    )
    return {
        "entity_id": entity_id,
        "rows": rows,
        "row_count_estimate": sample.row_count_estimate,
        "column_stats": sample.column_stats,
        "sampled_at": sample.sampled_at,
        "masking_applied": sample.masking_applied,
    }


@router.post("/{entity_id:path}/refresh_sample")
async def refresh_sample(
    project_id: uuid.UUID,
    entity_id: str,
    body: SampleIngest,
    user: CurrentUser,
    request: Request,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Ingest a freshly-captured raw sample.

    The caller (analyzer runner / manual trigger) supplies pre-fetched rows;
    the platform performs masking and stat computation server-side so raw
    values never linger in storage.
    """
    # Samples hit the source DB via the analyzer, so keep them bounded.
    request.state.user = user
    await rl_enforce(actor_key(request, "data.sample"), limit=20, window_sec=60)
    entity = (
        await db.execute(
            select(Node).where(
                Node.project_id == project_id,
                Node.id == entity_id,
                Node.kind == "DataEntity",
                Node.valid_to.is_(None),
            )
        )
    ).scalar_one_or_none()
    if entity is None:
        raise HTTPException(status_code=404, detail="entity_not_found")
    if (entity.data or {}).get("is_sensitive"):
        raise HTTPException(
            status_code=403, detail="sensitive_entity_sample_disallowed"
        )

    # Resolve per-project-DB masking overrides when the entity advertises
    # its DB component id (spec §12.2). Absence falls back to defaults so
    # projects without explicit bindings still mask PII.
    db_component = (entity.data or {}).get("component_id")
    engine: MaskingEngine | None = None
    if db_component:
        pdb = await resolve_project_db(db, project_id, db_component)
        if pdb is not None:
            engine = MaskingEngine.from_project_db(pdb.masking_rules)

    masked_rows, _col_flags, any_masked = mask_rows(body.columns, body.rows, engine)
    stats = compute_column_stats(body.columns, masked_rows)

    sample = DataSample(
        project_id=project_id,
        data_entity_id=entity_id,
        sample_rows=[
            {col: row[i] for i, col in enumerate(body.columns)} for row in masked_rows
        ],
        row_count_estimate=body.row_count_estimate,
        column_stats=stats,
        masking_applied=any_masked,
    )
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    await audit_record(
        actor=f"user:{user.id}",
        action="data.sample_refresh",
        target=entity_id,
        project_id=project_id,
        details={"rows": len(masked_rows), "masking_applied": any_masked},
    )
    return {
        "id": str(sample.id),
        "rows": len(masked_rows),
        "masking_applied": any_masked,
        "sampled_at": sample.sampled_at,
    }


query_router = APIRouter(
    prefix="/api/v1/projects/{project_id}/data",
    tags=["data"],
    dependencies=[Depends(require_project_org())],
)


class QueryRequest(BaseModel):
    db_component_id: str
    columns: list[str] = Field(description="Pre-validated column names")
    rows: list[list[Any]] = Field(description="Pre-executed result rows")
    sql: str
    purpose: str = Field(min_length=3)
    execution_ms: int | None = None
    max_rows: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Operator-supplied row cap. Values above the platform max "
            "(10 000) are silently clamped and the response carries a "
            "Warning header (RFC 7234) so the operator can see what we "
            "trimmed. The analyzer honours the same cap via "
            "MNEMOS_MAX_ROWS on its fetchmany loop."
        ),
    )


@query_router.post("/query")
async def query_data(
    project_id: uuid.UUID,
    body: QueryRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_operator),
) -> dict[str, Any]:
    """Accept a pre-executed read-only query result, mask it, and log it.

    The actual SELECT is run by the analyzer container (ggoss-sql-mssql /
    ggoss-sql-oracle ``query``) so the platform's own process stays isolated
    from production DBs; this endpoint is the guarded landing zone.
    """
    # 30 queries per minute per user: the landing zone is fronting
    # production DBs, so even operators need a ceiling.
    request.state.user = user
    await rl_enforce(actor_key(request, "data.query"), limit=30, window_sec=60)

    # AST-level write block + row-cap (spec §2.8). Regex "starts-with-SELECT"
    # is not sufficient — a `WITH cte AS (UPDATE ...) SELECT ...` form
    # is a SELECT shell over a write statement.
    pdb_for_dialect = await resolve_project_db(db, project_id, body.db_component_id)
    if pdb_for_dialect is None or not is_supported(pdb_for_dialect.kind):
        # Refuse to guess a dialect for an unbound or unknown kind.
        # The previous fallback to postgres would silently mis-parse
        # MSSQL ``TOP`` / Oracle ``FETCH FIRST`` quirks.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "db_component_unbound_or_unsupported",
                "component_id": body.db_component_id,
                "supported": sorted(SUPPORTED_DIALECTS),
            },
        )

    # Clamp the operator-supplied max_rows instead of refusing (Team B
    # critique 1.3: UX-driven choice, audit trail tells the truth).
    requested = body.max_rows or DEFAULT_MAX_ROWS
    effective_cap = min(requested, DEFAULT_MAX_ROWS)
    cap_clamped = requested > DEFAULT_MAX_ROWS
    if cap_clamped:
        # RFC 7234 §5.5 — Warning: 299 from a non-cache agent communicates
        # a transformation the operator should know about.
        response.headers["Warning"] = (
            f'299 - "max_rows clamped from {requested} to {DEFAULT_MAX_ROWS}"'
        )

    try:
        enforced = enforce_limit(
            body.sql,
            dialect=pdb_for_dialect.kind,
            max_rows=effective_cap,
        )
    except WriteStatementBlocked as exc:
        await audit_record(
            actor=f"user:{user.id}",
            action="data.write_blocked",
            target=body.db_component_id,
            project_id=project_id,
            details={"kind": exc.kind, "purpose": body.purpose},
        )
        raise HTTPException(status_code=400, detail=f"write_blocked:{exc.kind}")
    except Exception as exc:  # sqlglot.ParseError and friends
        # Surface a structured detail so the dashboard can render a
        # friendlier error than the raw sqlglot exception string —
        # 4th-round audit (scenario 2 / C3).
        detail = {
            "code": "sql_parse_failed",
            "message": (
                "The SQL could not be parsed as a read-only SELECT. "
                "Common causes: missing FROM clause, dialect mismatch, "
                "or a write keyword that slipped through (INSERT/UPDATE/"
                "DELETE/MERGE)."
            ),
            "parser_error": str(exc)[:300],
        }
        raise HTTPException(status_code=400, detail=detail)

    # Per-project-DB policy: block sensitive tables, enforce AWR consent
    # and maintenance windows, and apply masking overrides
    # (spec §7.4, §12.2, §14.2). No row means the platform falls back to
    # defaults — operators are expected to register DBs before first use.
    # We reuse the lookup we already did to choose a parse dialect; the
    # row enforces project-DB policy too.
    pdb = pdb_for_dialect
    if pdb is not None and pdb.disabled_at is not None:
        raise HTTPException(status_code=423, detail="project_db_disabled")
    engine: MaskingEngine | None = None
    awr_needed = requires_awr(body.sql)
    try:
        enforce_policy(pdb, awr_required=awr_needed, sql=body.sql)
    except PolicyViolation as exc:
        await audit_record(
            actor=f"user:{user.id}",
            action=f"data.{exc.code}",
            target=body.db_component_id,
            project_id=project_id,
            details={"purpose": body.purpose, "awr": awr_needed},
        )
        status_code = 423 if exc.code == "outside_maintenance_window" else 403
        raise HTTPException(status_code=status_code, detail=exc.code)
    if pdb is not None:
        engine = MaskingEngine.from_project_db(pdb.masking_rules)

    masked_rows, _flags, any_masked = mask_rows(body.columns, body.rows, engine)

    # The analyzer is contractually capped at ``effective_limit`` rows
    # but we double-check here so the platform never serves more than
    # promised even if an older analyzer ignored the cap.
    if len(masked_rows) > enforced.effective_limit:
        masked_rows = masked_rows[: enforced.effective_limit]

    log = DataQueryLog(
        project_id=project_id,
        db_component_id=body.db_component_id,
        sql=body.sql,
        purpose=body.purpose,
        requester=f"user:{user.id}",
        row_count=len(masked_rows),
        execution_ms=body.execution_ms,
    )
    db.add(log)
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="data.query",
        target=body.db_component_id,
        project_id=project_id,
        details={
            "purpose": body.purpose,
            "rows": len(masked_rows),
            "limit_clamp": enforced.clamp,
            "original_limit": enforced.original_limit,
            "effective_limit": enforced.effective_limit,
        },
    )
    return {
        "columns": body.columns,
        "rows": masked_rows,
        "row_count": len(masked_rows),
        "execution_ms": body.execution_ms,
        "masking_applied": any_masked,
        "executed_at": datetime.utcnow(),
        "limit_clamp": enforced.clamp,
        "original_limit": enforced.original_limit,
        "effective_limit": enforced.effective_limit,
        "rewritten_sql": enforced.sql,
    }
