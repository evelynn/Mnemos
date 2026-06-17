"""PR-179 — admin API for the Chat tab's AI-provider configuration.

Backs the "AI 제공자" Settings section so an operator configures providers
in the GUI instead of editing env vars and restarting. API keys are stored
encrypted in the ``Secret`` table (reusing the platform's KMS); model names
and Atlas's base URL go in the global ``PlatformSetting`` row
``chat_providers``. ``llm_providers.resolve_config`` reads both back (DB
over env) at chat time.

Admin-only. Keys are write-only over this API — a GET never returns a
stored key, only whether one is present.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.llm_providers import (
    PROVIDER_LABELS,
    PROVIDER_ORDER,
    SECRET_KIND,
    SECRET_PREFIX,
    SETTING_KEY,
    SUGGESTED_MODELS,
    default_provider,
    is_provider_available,
    resolve_config,
    secret_label,
    test_provider,
)
from app.audit.logger import record as audit_record
from app.auth.rbac import require_admin
from app.db import get_session
from app.extractor.agent_sdk import is_agent_sdk_available
from app.models.auth import PlatformSetting, Secret, User
from app.safety.crypto import encrypt

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


class ProviderConfigUpdate(BaseModel):
    api_key: str | None = Field(default=None, max_length=400)
    clear_key: bool = False
    model: str | None = Field(default=None, max_length=120)
    base_url: str | None = Field(default=None, max_length=400)
    mode: str | None = Field(default=None, max_length=20)
    agent_id: str | None = Field(default=None, max_length=100)


def _status(provider: str, cfg: dict[str, dict], by_label: dict | None = None) -> dict:
    c = cfg.get(provider) or {}
    sec = (by_label or {}).get(secret_label(provider))
    msg = sec.last_test_result if sec else None
    return {
        "id": provider,
        "label": PROVIDER_LABELS[provider],
        "has_key": bool(c.get("api_key")),
        "model": c.get("model"),
        "base_url": c.get("base_url"),
        "agent_id": c.get("agent_id"),
        "available": is_provider_available(provider, cfg),
        "needs_base_url": provider == "atlas",
        "needs_agent_id": provider == "atlas",
        # claudecode needs a key only in explicit "api" mode; the default
        # "subscription" mode uses the local Claude Code login (no key).
        "needs_key": provider != "claudecode" or c.get("mode") == "api",
        "mode": c.get("mode"),
        "subscription_available": is_agent_sdk_available(),
        "suggested_models": SUGGESTED_MODELS.get(provider, []),
        # Persisted last-test outcome (drives the status dot). OK-prefixed
        # messages mean success (see llm_providers.test_provider).
        "last_tested_at": (
            sec.last_tested_at.isoformat() if sec and sec.last_tested_at else None
        ),
        "last_test_ok": (msg.startswith("OK") if msg else None),
        "last_test_message": msg,
    }


@router.get("/provider-config")
async def get_provider_config(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Per-provider config status for the Settings UI. Never returns a
    stored API key — only ``has_key`` and the last-test outcome."""
    cfg = await resolve_config(db)
    secs = (
        await db.execute(select(Secret).where(Secret.label.like(SECRET_PREFIX + "%")))
    ).scalars().all()
    by_label = {s.label: s for s in secs}
    return {
        "providers": [_status(pid, cfg, by_label) for pid in PROVIDER_ORDER],
        "default": default_provider(cfg),
    }


@router.put("/provider-config/{provider}")
async def put_provider_config(
    provider: str,
    body: ProviderConfigUpdate,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    if provider not in PROVIDER_ORDER:
        raise HTTPException(status_code=404, detail="unknown_provider")

    # 1. model + base_url → the global PlatformSetting JSON blob.
    ps = (
        await db.execute(
            select(PlatformSetting).where(PlatformSetting.key == SETTING_KEY)
        )
    ).scalar_one_or_none()
    blob: dict = dict(ps.value) if ps and ps.value else {}
    pv: dict = dict(blob.get(provider) or {})
    if body.model is not None:
        pv["model"] = body.model.strip()
    if body.base_url is not None:
        pv["base_url"] = body.base_url.strip()
    if body.mode is not None:
        pv["mode"] = body.mode.strip()
    if body.agent_id is not None:
        pv["agent_id"] = body.agent_id.strip()
    blob[provider] = pv
    if ps is None:
        db.add(PlatformSetting(key=SETTING_KEY, value=blob))
    else:
        ps.value = blob  # reassign so SQLAlchemy flushes the JSON change

    # 2. API key → encrypted Secret (write-only; clear removes it).
    label = secret_label(provider)
    existing = (
        await db.execute(select(Secret).where(Secret.label == label))
    ).scalar_one_or_none()
    if body.clear_key:
        if existing is not None:
            await db.execute(delete(Secret).where(Secret.label == label))
    elif body.api_key and body.api_key.strip():
        ciphertext, iv = encrypt(body.api_key.strip())
        if existing is None:
            db.add(Secret(
                label=label, kind=SECRET_KIND,
                ciphertext=ciphertext, iv=iv,
                organization_id=user.organization_id,
            ))
        else:
            existing.ciphertext = ciphertext
            existing.iv = iv

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="conflict")

    await audit_record(
        actor=f"user:{user.id}",
        action="chat.provider_config.update",
        target=provider,
        details={"model": pv.get("model"),
                 "key_changed": bool(body.api_key) or body.clear_key},
    )
    cfg = await resolve_config(db)
    return _status(provider, cfg)


@router.post("/provider-config/{provider}/test")
async def test_provider_config(
    provider: str,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> dict:
    """Live-validate the provider's key/base_url and return the models it
    exposes (the UI refreshes its dropdown from this)."""
    if provider not in PROVIDER_ORDER:
        raise HTTPException(status_code=404, detail="unknown_provider")
    cfg = await resolve_config(db)
    result = await test_provider(provider, cfg)

    # Record the outcome on the stored Secret if there is one.
    label = secret_label(provider)
    now = datetime.utcnow()
    await db.execute(
        update(Secret).where(Secret.label == label).values(
            last_tested_at=now, last_test_result=result.get("message", "")[:200]
        )
    )
    await db.commit()
    await audit_record(
        actor=f"user:{user.id}",
        action="chat.provider_config.test",
        target=provider,
        details={"ok": result.get("ok"), "message": result.get("message")},
    )
    return result
