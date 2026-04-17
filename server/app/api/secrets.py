import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.deps import CurrentUser
from app.db import get_session
from app.models.auth import Secret
from app.safety.crypto import decrypt, encrypt

router = APIRouter(prefix="/api/v1/secrets", tags=["secrets"])

SecretKind = Literal["db_connection", "gitlab_token", "llm_api_key", "other"]


class SecretCreate(BaseModel):
    label: str = Field(min_length=1, max_length=200)
    kind: SecretKind
    value: str = Field(min_length=1)


class SecretUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    value: str | None = Field(default=None, min_length=1)


class SecretOut(BaseModel):
    id: uuid.UUID
    label: str
    kind: str
    created_at: datetime
    last_tested_at: datetime | None
    last_test_result: str | None


class SecretTestResult(BaseModel):
    ok: bool
    message: str
    tested_at: datetime


def _to_out(s: Secret) -> SecretOut:
    return SecretOut(
        id=s.id,
        label=s.label,
        kind=s.kind,
        created_at=s.created_at,
        last_tested_at=s.last_tested_at,
        last_test_result=s.last_test_result,
    )


@router.get("")
async def list_secrets(
    _: CurrentUser, db: AsyncSession = Depends(get_session)
) -> list[SecretOut]:
    result = await db.execute(select(Secret).order_by(Secret.created_at.desc()))
    return [_to_out(s) for s in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_secret(
    body: SecretCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> SecretOut:
    ciphertext, iv = encrypt(body.value)
    secret = Secret(
        label=body.label, kind=body.kind, ciphertext=ciphertext, iv=iv
    )
    db.add(secret)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="label_already_exists"
        )
    await db.refresh(secret)
    await audit_record(
        actor=f"user:{user.id}",
        action="secret.create",
        target=str(secret.id),
        details={"label": secret.label, "kind": secret.kind},
    )
    return _to_out(secret)


@router.patch("/{secret_id}")
async def update_secret(
    secret_id: uuid.UUID,
    body: SecretUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> SecretOut:
    secret = (
        await db.execute(select(Secret).where(Secret.id == secret_id))
    ).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="not_found")
    if body.label is not None:
        secret.label = body.label
    if body.value is not None:
        secret.ciphertext, secret.iv = encrypt(body.value)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="label_already_exists")
    await db.refresh(secret)
    await audit_record(
        actor=f"user:{user.id}",
        action="secret.update",
        target=str(secret.id),
    )
    return _to_out(secret)


@router.delete("/{secret_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    secret_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> None:
    result = await db.execute(delete(Secret).where(Secret.id == secret_id))
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="not_found")
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}", action="secret.delete", target=str(secret_id)
    )


@router.post("/{secret_id}/test")
async def test_secret(
    secret_id: uuid.UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_session),
) -> SecretTestResult:
    """Phase-1 placeholder test: only verifies that the secret decrypts.

    Live DB connection probes are wired in TKT covered in Week 4 alongside
    the SQL analyzers, when the ``ggoss-sql-mssql`` connector lands.
    """
    secret = (
        await db.execute(select(Secret).where(Secret.id == secret_id))
    ).scalar_one_or_none()
    if secret is None:
        raise HTTPException(status_code=404, detail="not_found")
    try:
        decrypt(secret.ciphertext, secret.iv)
        ok = True
        message = "secret_decrypts_ok"
    except Exception as exc:  # noqa: BLE001
        ok = False
        message = f"decrypt_failed: {exc.__class__.__name__}"
    now = datetime.utcnow()
    await db.execute(
        update(Secret)
        .where(Secret.id == secret_id)
        .values(last_tested_at=now, last_test_result=message)
    )
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="secret.test",
        target=str(secret_id),
        details={"ok": ok, "message": message},
    )
    return SecretTestResult(ok=ok, message=message, tested_at=now)
