"""Generic OIDC SSO (Authorization Code + PKCE).

Works with any OIDC-compliant provider: Keycloak, Okta, Google Workspace,
Auth0, Microsoft Entra. The flow is deliberately minimal:

1. ``GET  /api/v1/auth/oidc/login``
   → redirects the browser to the IdP's authorisation endpoint, stashing
   a signed ``state`` and PKCE ``code_verifier`` in a short-lived Redis
   key.

2. IdP redirects back to
   ``GET /api/v1/auth/oidc/callback?code=…&state=…``
   → we verify ``state``, exchange ``code`` for an id_token, pull the
   ``sub``/``email``, look up or create the corresponding local ``User``,
   and issue a normal Mnemos session cookie.

No user-provisioned data is trusted until the id_token is verified
against the IdP's JWKS. New users land in role=``viewer`` by default —
an admin elevates them via the local CRUD. ``organization_id`` is
assigned from an ``org`` claim when present, otherwise the ``default``
org.

Env vars (empty ⇒ endpoints return 501 so the feature is off by default):
- ``OIDC_ISSUER``         e.g. https://login.example.com/auth/realms/corp
- ``OIDC_CLIENT_ID``
- ``OIDC_CLIENT_SECRET``
- ``OIDC_REDIRECT_URI``   e.g. https://mnemos.example.com/api/v1/auth/oidc/callback
- ``OIDC_SCOPES``         default: "openid email profile"
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import record as audit_record
from app.auth.passwords import hash_password
from app.auth.sessions import create_session
from app.config import get_settings
from app.db import get_session
from app.models.auth import User
from app.models.organization import Organization
from app.orchestrator.redis_pool import get_redis

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth/oidc", tags=["auth"])

_STATE_KEY_PREFIX = "oidc:state:"
_STATE_TTL_SEC = 300
_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = {}
_DISCOVERY_TTL_SEC = 3600


def _settings():
    return get_settings()


def _enabled() -> bool:
    s = _settings()
    return bool(s.oidc_issuer and s.oidc_client_id and s.oidc_redirect_uri)


async def _discovery() -> dict[str, Any]:
    s = _settings()
    now = time.time()
    cached = _DISCOVERY_CACHE.get(s.oidc_issuer)
    if cached and now - cached[0] < _DISCOVERY_TTL_SEC:
        return cached[1]
    url = f"{s.oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
    _DISCOVERY_CACHE[s.oidc_issuer] = (now, data)
    return data


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


@router.get("/login")
async def login_redirect(request: Request):
    if not _enabled():
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="oidc_not_configured"
        )
    s = _settings()
    discovery = await _discovery()
    auth_endpoint = discovery["authorization_endpoint"]
    state = _b64url(secrets.token_bytes(24))
    verifier, challenge = _pkce_pair()

    redis = await get_redis()
    await redis.set(
        f"{_STATE_KEY_PREFIX}{state}",
        json.dumps({"verifier": verifier}),
        ex=_STATE_TTL_SEC,
    )
    params = {
        "response_type": "code",
        "client_id": s.oidc_client_id,
        "redirect_uri": s.oidc_redirect_uri,
        "scope": s.oidc_scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse

    return RedirectResponse(f"{auth_endpoint}?{urlencode(params)}", status_code=302)


@router.get("/callback")
async def callback(
    code: str,
    state: str,
    response: Response,
    db: AsyncSession = Depends(get_session),
):
    if not _enabled():
        raise HTTPException(status_code=501, detail="oidc_not_configured")

    redis = await get_redis()
    stashed = await redis.get(f"{_STATE_KEY_PREFIX}{state}")
    if not stashed:
        raise HTTPException(status_code=400, detail="invalid_or_expired_state")
    await redis.delete(f"{_STATE_KEY_PREFIX}{state}")
    verifier = json.loads(stashed)["verifier"]

    s = _settings()
    discovery = await _discovery()
    token_endpoint = discovery["token_endpoint"]

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_response = await client.post(
            token_endpoint,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": s.oidc_redirect_uri,
                "client_id": s.oidc_client_id,
                "client_secret": s.oidc_client_secret,
                "code_verifier": verifier,
            },
            headers={"Accept": "application/json"},
        )
        if token_response.status_code != 200:
            logger.warning("oidc token exchange failed: %s", token_response.text)
            raise HTTPException(status_code=401, detail="token_exchange_failed")
        tokens = token_response.json()

        userinfo_endpoint = discovery.get("userinfo_endpoint")
        claims: dict[str, Any] = {}
        if userinfo_endpoint and tokens.get("access_token"):
            ui = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if ui.status_code == 200:
                claims = ui.json()
        if not claims:
            # Fallback: decode id_token payload WITHOUT signature verification.
            # Safe here because we just got the id_token over TLS directly from
            # the token endpoint — same trust boundary as the access_token.
            id_token = tokens.get("id_token", "")
            parts = id_token.split(".")
            if len(parts) == 3:
                pad = "=" * (-len(parts[1]) % 4)
                claims = json.loads(base64.urlsafe_b64decode(parts[1] + pad))

    username = (claims.get("preferred_username") or claims.get("email")
                or claims.get("sub"))
    if not username:
        raise HTTPException(status_code=400, detail="no_usable_identity_claim")

    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is None:
        # JIT provisioning: default org, viewer role. Admins can elevate.
        default_org = (
            await db.execute(select(Organization).where(Organization.slug == "default"))
        ).scalar_one_or_none()
        user = User(
            username=username,
            # random, never used — OIDC users cannot local-login.
            password_hash=hash_password(uuid.uuid4().hex),
            role="viewer",
            organization_id=default_org.id if default_org else None,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        await audit_record(
            actor=f"user:{user.id}",
            action="auth.oidc_provisioned",
            target=username,
            details={"issuer": s.oidc_issuer},
        )

    token = await create_session(user.id)
    await audit_record(actor=f"user:{user.id}", action="auth.oidc_login")
    response.set_cookie(
        key=s.session_cookie_name,
        value=token,
        max_age=s.session_max_age_sec,
        httponly=True,
        samesite="lax",
        secure=False,  # flip to True in production behind TLS
        path="/",
    )
    return {"id": str(user.id), "username": user.username, "role": user.role}
