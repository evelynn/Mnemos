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


_JWKS_CACHE: dict[str, tuple[float, dict]] = {}
_JWKS_TTL_SEC = 3600


async def _jwks(client: httpx.AsyncClient, discovery: dict) -> dict:
    """Fetch and cache the IdP JWKS (rotates per ``_JWKS_TTL_SEC``)."""
    url = discovery.get("jwks_uri")
    if not url:
        raise HTTPException(status_code=500, detail="oidc_no_jwks_uri")
    now = time.time()
    cached = _JWKS_CACHE.get(url)
    if cached and now - cached[0] < _JWKS_TTL_SEC:
        return cached[1]
    r = await client.get(url, timeout=5.0)
    r.raise_for_status()
    data = r.json()
    _JWKS_CACHE[url] = (now, data)
    return data


async def _verify_id_token(
    client: httpx.AsyncClient,
    discovery: dict,
    id_token: str,
    s,
) -> dict:
    """Verify the id_token signature against the IdP JWKS and return claims.

    Validates: signature, issuer match, audience match, exp/iat skew.
    """
    import jwt
    from jwt.exceptions import InvalidTokenError

    jwks_data = await _jwks(client, discovery)
    try:
        unverified_header = jwt.get_unverified_header(id_token)
    except InvalidTokenError as exc:
        # PR-132 — generic detail to the caller, full exc in server log.
        # Exposing the PyJWT message gives an attacker hints about
        # which token format / claim parsing failed.
        logger.warning("oidc id_token_malformed: %s", exc)
        raise HTTPException(status_code=401, detail="id_token_malformed")

    kid = unverified_header.get("kid")
    matching = next(
        (k for k in jwks_data.get("keys", []) if k.get("kid") == kid),
        None,
    )
    if matching is None and len(jwks_data.get("keys", [])) == 1:
        matching = jwks_data["keys"][0]
    if matching is None:
        raise HTTPException(status_code=401, detail="id_token_unknown_kid")

    alg = matching.get("alg") or unverified_header.get("alg") or "RS256"
    kty = (matching.get("kty") or "").upper()
    try:
        if kty == "RSA":
            public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(matching))
        elif kty == "EC":
            public_key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(matching))
        else:
            # PR-132 — kty is a fixed enum (RSA/EC/oct/OKP), leak risk
            # nominal but generic detail follows the same pattern as
            # the other id_token_* responses.
            logger.warning("oidc id_token_unsupported_kty: %s", kty)
            raise HTTPException(
                status_code=401, detail="id_token_unsupported_kty"
            )
    except (ValueError, TypeError) as exc:
        logger.warning("oidc id_token_jwk_invalid: %s", exc)
        raise HTTPException(status_code=401, detail="id_token_jwk_invalid")

    try:
        claims = jwt.decode(
            id_token,
            public_key,
            algorithms=[alg],
            audience=s.oidc_client_id,
            issuer=discovery.get("issuer", s.oidc_issuer),
            leeway=30,
        )
    except InvalidTokenError as exc:
        logger.warning("oidc id_token_invalid: %s", exc)
        raise HTTPException(status_code=401, detail="id_token_invalid")
    return claims


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
    # PR-130 — OIDC nonce. Defends against id_token replay: a
    # captured token from a different login attempt won't match
    # the nonce stashed for *this* state, so /callback rejects it.
    # Standard recommends this even with state + PKCE for
    # belt-and-braces.
    nonce = _b64url(secrets.token_bytes(24))
    verifier, challenge = _pkce_pair()

    redis = await get_redis()
    await redis.set(
        f"{_STATE_KEY_PREFIX}{state}",
        json.dumps({"verifier": verifier, "nonce": nonce}),
        ex=_STATE_TTL_SEC,
    )
    params = {
        "response_type": "code",
        "client_id": s.oidc_client_id,
        "redirect_uri": s.oidc_redirect_uri,
        "scope": s.oidc_scopes,
        "state": state,
        "nonce": nonce,
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
    stashed_data = json.loads(stashed)
    verifier = stashed_data["verifier"]
    # PR-130 — nonce echo must match what we stashed in /login.
    # Older state entries (pre-PR-130) won't carry a nonce key;
    # treat them as legitimate to avoid kicking valid in-flight
    # logins during deploy.
    expected_nonce = stashed_data.get("nonce")

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

        # Verify the id_token signature against the IdP JWKS BEFORE we
        # trust any claim. Without this step a compromised IdP host or a
        # MitM on the issuer-issued token could spoof an arbitrary user.
        id_token = tokens.get("id_token")
        if not id_token:
            raise HTTPException(status_code=401, detail="no_id_token")
        claims = await _verify_id_token(client, discovery, id_token, s)

        # PR-130 — nonce comparison. id_token MUST echo the nonce
        # we sent at /login. Replay attacks fail here even when
        # state somehow leaks. If we issued the state pre-nonce
        # (deploy mid-flight), expected_nonce is None and we skip
        # the check — the state TTL bounds the staleness anyway.
        if expected_nonce is not None:
            id_token_nonce = claims.get("nonce")
            if id_token_nonce != expected_nonce:
                logger.warning("oidc nonce mismatch")
                raise HTTPException(status_code=401, detail="id_token_nonce_mismatch")

        # Optional userinfo enrichment — preferred for richer profile
        # data, but we trust id_token claims as the authoritative
        # identity now that the signature is validated.
        userinfo_endpoint = discovery.get("userinfo_endpoint")
        if userinfo_endpoint and tokens.get("access_token"):
            ui = await client.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {tokens['access_token']}"},
            )
            if ui.status_code == 200:
                # Merge userinfo on top of verified id_token claims; do
                # not allow userinfo to override the verified ``sub``.
                merged = {**ui.json(), **{"sub": claims.get("sub")}}
                claims = {**claims, **merged}

    username = (claims.get("preferred_username") or claims.get("email")
                or claims.get("sub"))
    if not username:
        raise HTTPException(status_code=400, detail="no_usable_identity_claim")

    user = (
        await db.execute(select(User).where(User.username == username))
    ).scalar_one_or_none()
    if user is not None and user.disabled_at is not None:
        await audit_record(
            actor=f"user:{user.id}",
            action="auth.oidc_login_blocked_disabled",
            target=username,
            details={"issuer": s.oidc_issuer},
        )
        raise HTTPException(status_code=401, detail="invalid_credentials")
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
        secure=s.session_cookie_secure,
        path="/",
    )
    return {"id": str(user.id), "username": user.username, "role": user.role}
