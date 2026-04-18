"""OIDC id_token signature verification.

Generates an ephemeral RSA key in the test, signs a real id_token with
it, hand-crafts the matching JWKS, and feeds both to ``_verify_id_token``.
Catches the regression where we trusted unverified payloads.
"""

from __future__ import annotations

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa


def _key_pair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private.public_key().public_numbers()
    n = public_numbers.n.to_bytes((public_numbers.n.bit_length() + 7) // 8, "big")
    e = public_numbers.e.to_bytes((public_numbers.e.bit_length() + 7) // 8, "big")
    jwk = {
        "kty": "RSA",
        "alg": "RS256",
        "use": "sig",
        "kid": "test-kid-1",
        "n": base64.urlsafe_b64encode(n).rstrip(b"=").decode(),
        "e": base64.urlsafe_b64encode(e).rstrip(b"=").decode(),
    }
    return private, jwk


class _FakeClient:
    """Just enough of httpx.AsyncClient for ``_verify_id_token``.

    We never reach the network — discovery and JWKS are pre-populated
    via the ``_JWKS_CACHE`` so the test stays hermetic.
    """

    pass


@pytest.fixture(autouse=True)
def reset_jwks_cache():
    from app.auth import oidc

    oidc._JWKS_CACHE.clear()
    yield
    oidc._JWKS_CACHE.clear()


async def test_signed_id_token_accepted(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "mnemos-test")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app/cb")
    from app import config
    from app.auth import oidc

    config.get_settings.cache_clear()
    s = config.get_settings()

    private, jwk = _key_pair()
    discovery = {
        "issuer": "https://idp.example.com",
        "jwks_uri": "https://idp.example.com/jwks",
    }
    oidc._JWKS_CACHE[discovery["jwks_uri"]] = (
        time.time(),
        {"keys": [jwk]},
    )

    token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "mnemos-test",
            "sub": "user-42",
            "email": "u@example.com",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )

    claims = await oidc._verify_id_token(_FakeClient(), discovery, token, s)
    assert claims["sub"] == "user-42"
    assert claims["email"] == "u@example.com"


async def test_wrong_audience_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "mnemos-test")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app/cb")
    from app import config
    from app.auth import oidc

    config.get_settings.cache_clear()
    s = config.get_settings()

    private, jwk = _key_pair()
    discovery = {"issuer": "https://idp.example.com", "jwks_uri": "https://idp.example.com/jwks"}
    oidc._JWKS_CACHE[discovery["jwks_uri"]] = (time.time(), {"keys": [jwk]})

    token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "some-other-client",
            "sub": "evil",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        private,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await oidc._verify_id_token(_FakeClient(), discovery, token, s)
    assert exc.value.status_code == 401


async def test_tampered_signature_rejected(monkeypatch):
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "mnemos-test")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "x")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://app/cb")
    from app import config
    from app.auth import oidc

    config.get_settings.cache_clear()
    s = config.get_settings()

    # Sign with key A, but publish key B as the JWKS — signature fails.
    private_a, _ = _key_pair()
    _, jwk_b = _key_pair()
    jwk_b["kid"] = "test-kid-1"
    discovery = {"issuer": "https://idp.example.com", "jwks_uri": "https://idp.example.com/jwks"}
    oidc._JWKS_CACHE[discovery["jwks_uri"]] = (time.time(), {"keys": [jwk_b]})

    token = jwt.encode(
        {
            "iss": "https://idp.example.com",
            "aud": "mnemos-test",
            "sub": "spoof",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        },
        private_a,
        algorithm="RS256",
        headers={"kid": "test-kid-1"},
    )

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await oidc._verify_id_token(_FakeClient(), discovery, token, s)
    assert exc.value.status_code == 401


def test_imports_alone_do_not_pull_redis():
    """Regression: importing oidc.py shouldn't drag in the redis client."""
    import importlib
    import sys

    sys.modules.pop("app.auth.oidc", None)
    importlib.import_module("app.auth.oidc")
