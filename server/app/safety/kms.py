"""Pluggable key management backend.

Why a plugin? Operators care deeply about how the Data Encryption Key
(the Fernet key that wraps every stored secret) is stored. The
Phase-A/B default — reading ``FERNET_KEY`` from env — is fine for
single-host deployments behind a hardened host, but a larger org wants
an external KMS so the DEK never lives on disk or in env files.

This module exposes:

- ``KmsBackend`` protocol
- ``LocalFernetKms``  — reads ``FERNET_KEY``; the existing behaviour.
- ``VaultKmsBackend`` — fetches the DEK from HashiCorp Vault's KV-v2
  engine; picked for self-hosted deployments because Vault itself is
  self-hostable and carries no cloud dependency.

Selection via ``KMS_BACKEND`` env var (default ``local``). Consumers
should call ``get_kms()`` instead of hard-coding Fernet.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import logging
import os
from typing import Protocol

import httpx
from cryptography.fernet import Fernet

from app.config import get_settings

logger = logging.getLogger(__name__)


class KmsBackend(Protocol):
    """Minimal surface every KMS plugin must implement."""

    def encrypt(self, plaintext: bytes) -> bytes: ...
    def decrypt(self, ciphertext: bytes) -> bytes: ...


class LocalFernetKms:
    """Reads the DEK from the FERNET_KEY env var.

    When no key is set, deterministically derives one from SECRET_KEY so
    local dev still works; this is unsafe in production and ``get_kms``
    emits a startup warning in that case.
    """

    def __init__(self) -> None:
        s = get_settings()
        if s.fernet_key:
            key = s.fernet_key.encode()
        else:
            logger.warning(
                "kms: FERNET_KEY not set, deriving DEK from SECRET_KEY — "
                "only safe for local development"
            )
            digest = hashlib.sha256(s.secret_key.encode()).digest()
            key = base64.urlsafe_b64encode(digest)
        self._fernet = Fernet(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)


class VaultKmsBackend:
    """HashiCorp Vault KV-v2 backed DEK.

    Fetches a single Fernet key at startup via the Vault HTTP API and
    caches it for the process lifetime. Vault is chosen (over AWS KMS)
    because it is self-hostable on the same network as the Mnemos
    stack, so deployments stay air-gappable when that is a requirement.

    Env vars:
    - ``VAULT_ADDR``       e.g. http://vault:8200
    - ``VAULT_TOKEN``      periodic token with read on the KV path
    - ``VAULT_KV_PATH``    e.g. secret/data/mnemos/kms  (KV v2 layout)
    - ``VAULT_KV_KEY``     json key holding the base64 Fernet key
                           (default: ``fernet_key``)

    For token renewal / AppRole auth, wrap this class with an operator-
    side sidecar that refreshes ``VAULT_TOKEN`` — deliberately out of
    scope here to keep the core backend narrow.
    """

    def __init__(self) -> None:
        self._addr = os.getenv("VAULT_ADDR", "").rstrip("/")
        self._token = os.getenv("VAULT_TOKEN", "")
        self._kv_path = os.getenv("VAULT_KV_PATH", "")
        self._kv_key = os.getenv("VAULT_KV_KEY", "fernet_key")
        if not (self._addr and self._token and self._kv_path):
            raise RuntimeError(
                "kms: Vault backend selected but VAULT_ADDR/VAULT_TOKEN/VAULT_KV_PATH missing"
            )
        self._fernet = Fernet(self._fetch_key())

    def _fetch_key(self) -> bytes:
        url = f"{self._addr}/v1/{self._kv_path.lstrip('/')}"
        try:
            r = httpx.get(
                url,
                headers={"X-Vault-Token": self._token},
                timeout=5.0,
            )
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"kms: vault fetch failed: {exc}") from exc

        # KV v2 returns ``{"data": {"data": {<key>: <value>}, "metadata": ...}}``.
        payload = r.json()
        try:
            value = payload["data"]["data"][self._kv_key]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(
                f"kms: vault payload missing data.data.{self._kv_key}"
            ) from exc
        if not isinstance(value, str):
            raise RuntimeError("kms: vault DEK must be a string")
        return value.encode()

    def encrypt(self, plaintext: bytes) -> bytes:
        return self._fernet.encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return self._fernet.decrypt(ciphertext)


@functools.lru_cache(maxsize=1)
def get_kms() -> KmsBackend:
    """Return the configured KMS backend (cached singleton)."""
    backend = os.getenv("KMS_BACKEND", get_settings().kms_backend).lower()
    if backend == "vault":
        logger.info("kms: using HashiCorp Vault backend")
        return VaultKmsBackend()
    if backend != "local":
        logger.warning("kms: unknown backend %r, falling back to local", backend)
    logger.info("kms: using local Fernet backend")
    return LocalFernetKms()

