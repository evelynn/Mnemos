"""CRUD for per-project database bindings (spec §12.2)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.auth.org_scope import require_project_org
from app.auth.rbac import require_admin
from app.data_sampler.probe import ProbeResult, probe_via_analyzer
from app.db import get_session
from app.models.auth import Secret, User
from app.models.projects import ProjectDB
from app.safety.crypto import decrypt
from app.secret_scope import SecretScopeNotFound, resolve_project_secret

router = APIRouter(
    prefix="/api/v1/projects/{project_id}/dbs",
    tags=["project_dbs"],
    # Every route here targets a single project; the org-scope check
    # happens once at the router level rather than on each endpoint.
    dependencies=[Depends(require_project_org())],
)

DBKind = Literal["mssql", "oracle"]


class ProjectDBCreate(BaseModel):
    kind: DBKind
    display_name: str = Field(min_length=1, max_length=200)
    component_id: str = Field(min_length=1, max_length=300)
    secret_id: uuid.UUID | None = None
    allow_awr: bool = False
    sensitive_tables: list[str] = Field(default_factory=list)
    masking_rules: dict = Field(default_factory=dict)
    maintenance_windows: list[str] = Field(default_factory=list)


class ProjectDBUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    secret_id: uuid.UUID | None = None
    allow_awr: bool | None = None
    sensitive_tables: list[str] | None = None
    masking_rules: dict | None = None
    maintenance_windows: list[str] | None = None


class ProjectDBOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    kind: str
    display_name: str
    component_id: str
    secret_id: uuid.UUID | None
    allow_awr: bool
    sensitive_tables: list[str]
    masking_rules: dict
    maintenance_windows: list[str]
    last_probe_at: datetime | None
    last_probe_result: dict | None
    disabled_at: datetime | None
    created_at: datetime


def _to_out(row: ProjectDB) -> ProjectDBOut:
    return ProjectDBOut(
        id=row.id,
        project_id=row.project_id,
        kind=row.kind,
        display_name=row.display_name,
        component_id=row.component_id,
        secret_id=row.secret_id,
        allow_awr=row.allow_awr,
        sensitive_tables=list(row.sensitive_tables or []),
        masking_rules=dict(row.masking_rules or {}),
        maintenance_windows=list(row.maintenance_windows or []),
        last_probe_at=row.last_probe_at,
        last_probe_result=dict(row.last_probe_result or {}) if row.last_probe_result else None,
        disabled_at=row.disabled_at,
        created_at=row.created_at,
    )


async def _resolve_secret_in_project_org(
    db: AsyncSession,
    project_id: uuid.UUID,
    secret_id: uuid.UUID,
) -> Secret:
    """Resolve a credential only when it belongs to the project's exact org.

    Secret UUIDs are bearer-like references: accepting a UUID from another
    tenant would let an admin make Mnemos decrypt and use that tenant's
    database credential.  Missing projects/secrets, NULL legacy ownership,
    and cross-org pairs deliberately collapse to the same 404 response.
    """

    try:
        return await resolve_project_secret(
            db,
            project_id=project_id,
            secret_id=secret_id,
        )
    except SecretScopeNotFound as exc:
        raise HTTPException(status_code=404, detail="secret_not_found") from exc


async def _resolve_conn_ref(
    db: AsyncSession,
    project_id: uuid.UUID,
    secret_id: uuid.UUID | None,
) -> str | None:
    """Decrypt the linked Secret without ever holding the plaintext on the row.

    Returns ``None`` if no secret is wired up or decryption fails — the
    probe handler then surfaces an explicit ``secret_unavailable`` error
    instead of silently degrading to "deferred".
    """
    if secret_id is None:
        return None
    secret = await _resolve_secret_in_project_org(db, project_id, secret_id)
    try:
        return decrypt(secret.ciphertext, secret.iv)
    except Exception:
        return None


async def _probe_or_412(
    db: AsyncSession,
    project_id: uuid.UUID,
    kind: str,
    secret_id: uuid.UUID | None,
) -> ProbeResult:
    """Run a read-only probe and translate any unsafe result into HTTP 412."""
    conn = await _resolve_conn_ref(db, project_id, secret_id)
    if conn is None:
        raise HTTPException(
            status_code=412,
            detail={"code": "secret_unavailable",
                    "message": "no usable secret to probe — wire a secret first"},
        )
    result = await probe_via_analyzer(kind, conn)
    if not result.is_acceptable():
        # Surface enough info to debug, but never the connection string.
        raise HTTPException(
            status_code=412,
            detail={"code": "probe_failed", "result": result.as_jsonable()},
        )
    return result


@router.get("")
async def list_project_dbs(
    project_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> list[ProjectDBOut]:
    result = await db.execute(
        select(ProjectDB)
        .where(ProjectDB.project_id == project_id)
        .order_by(ProjectDB.created_at.desc())
    )
    return [_to_out(r) for r in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_project_db(
    project_id: uuid.UUID,
    body: ProjectDBCreate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> ProjectDBOut:
    # Spec §2.5 / §2.8: confirm the credential is read-only *before*
    # we accept the binding. The probe interrogates the live DB via the
    # analyzer subprocess (the platform itself never opens a connection
    # to the project DB) and refuses with 412 if write access cannot
    # be ruled out. A 24-hour cache window for the result is enforced
    # by the orchestrator, not here.
    probe = await _probe_or_412(db, project_id, body.kind, body.secret_id)
    row = ProjectDB(
        project_id=project_id,
        kind=body.kind,
        display_name=body.display_name,
        component_id=body.component_id,
        secret_id=body.secret_id,
        allow_awr=body.allow_awr,
        sensitive_tables=body.sensitive_tables,
        masking_rules=body.masking_rules,
        maintenance_windows=body.maintenance_windows,
        last_probe_at=datetime.now(tz=timezone.utc),
        last_probe_result=probe.as_jsonable(),
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="component_id_already_bound")
    await db.refresh(row)
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.create",
        target=str(row.id),
        project_id=project_id,
        details={
            "component_id": row.component_id,
            "kind": row.kind,
            "probe_status": probe.status,
            "probe_latency_ms": probe.latency_ms,
        },
    )
    return _to_out(row)


@router.post("/{db_id}/probe")
async def reprobe_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> dict[str, Any]:
    """Refresh the read-only probe and persist the new result.

    Used by the daily ``probe_recheck`` cron job (PR-4) and by operators
    after rotating credentials. Marks the binding as disabled when the
    new probe cannot confirm read-only access.
    """
    row = (
        await db.execute(
            select(ProjectDB).where(
                ProjectDB.id == db_id, ProjectDB.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    conn = await _resolve_conn_ref(db, project_id, row.secret_id)
    if conn is None:
        raise HTTPException(
            status_code=412,
            detail={"code": "secret_unavailable"},
        )
    result = await probe_via_analyzer(row.kind, conn)
    row.last_probe_at = datetime.now(tz=timezone.utc)
    row.last_probe_result = result.as_jsonable()
    if not result.is_acceptable() and row.disabled_at is None:
        row.disabled_at = row.last_probe_at
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.probe",
        target=str(row.id),
        project_id=project_id,
        details={
            "status": result.status,
            "disabled_at": row.disabled_at.isoformat() if row.disabled_at else None,
        },
    )
    return {"status": result.status, "result": result.as_jsonable()}


@router.get("/{db_id}")
async def get_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    _: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> ProjectDBOut:
    row = (
        await db.execute(
            select(ProjectDB).where(
                ProjectDB.id == db_id, ProjectDB.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    return _to_out(row)


@router.patch("/{db_id}")
async def update_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    body: ProjectDBUpdate,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> ProjectDBOut:
    row = (
        await db.execute(
            select(ProjectDB).where(
                ProjectDB.id == db_id, ProjectDB.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="not_found")
    if body.secret_id is not None:
        # Validate before assigning/persisting the bearer-like Secret UUID.
        # This also prevents a legacy contaminated ProjectDB row from being
        # re-bound to another tenant's credential.
        await _resolve_secret_in_project_org(db, project_id, body.secret_id)
    changes: dict = {}
    for field_name in (
        "display_name",
        "secret_id",
        "allow_awr",
        "sensitive_tables",
        "masking_rules",
        "maintenance_windows",
    ):
        value = getattr(body, field_name)
        if value is not None:
            setattr(row, field_name, value)
            changes[field_name] = True
    await db.commit()
    await db.refresh(row)
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.update",
        target=str(row.id),
        project_id=project_id,
        details={"fields": sorted(changes.keys())},
    )
    return _to_out(row)


@router.delete("/{db_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_db(
    project_id: uuid.UUID,
    db_id: uuid.UUID,
    db: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> None:
    result = await db.execute(
        delete(ProjectDB).where(
            ProjectDB.id == db_id, ProjectDB.project_id == project_id
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="not_found")
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="project_db.delete",
        target=str(db_id),
        project_id=project_id,
    )
