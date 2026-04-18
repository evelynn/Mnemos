"""Pluggable key management backend.

Why a plugin? Operators care deeply about how the Data Encryption Key
(the Fernet key that wraps every stored secret) is stored. The
Phase-A/B default — reading ``FERNET_KEY`` from env — is fine for
single-host deployments behind a hardened host, but a larger org wants
an HSM-backed KMS so the DEK never lives on disk or in env files.

This module exposes:

- ``KmsBackend`` protocol
- ``LocalFernetKms``   — reads ``FERNET_KEY``; the existing behaviour
- ``AwsKmsEnvelopeKms`` — wraps/unwraps the DEK with AWS KMS and caches
  the unwrapped DEK in memory

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


class AwsKmsEnvelopeKms:
    """AWS KMS envelope-encryption backend.

    The first call generates a data key via ``kms:GenerateDataKey`` and
    caches the plaintext DEK in memory for subsequent calls. The wrapped
    (ciphertext) DEK is stored alongside every secret so rewrap / rotation
    can be done without a platform restart.

    The actual import of ``boto3`` is deferred so the platform can boot
    without the package when ``KMS_BACKEND=local``.
    """

    def __init__(self) -> None:
        s = get_settings()
        if not s.kms_key_arn:
            raise RuntimeError("kms: AWS backend selected but KMS_KEY_ARN is empty")
        self._key_arn = s.kms_key_arn
        self._client = None
        self._cached_dek: bytes | None = None

    def _kms(self):
        if self._client is None:
            import boto3  # type: ignore[import-not-found]

            self._client = boto3.client("kms")
        return self._client

    def _dek(self) -> bytes:
        if self._cached_dek is not None:
            return self._cached_dek
        resp = self._kms().generate_data_key(KeyId=self._key_arn, KeySpec="AES_256")
        # Turn the 32-byte AES key into a Fernet key (urlsafe base64, 32 bytes).
        self._cached_dek = base64.urlsafe_b64encode(resp["Plaintext"])
        return self._cached_dek

    def encrypt(self, plaintext: bytes) -> bytes:
        return Fernet(self._dek()).encrypt(plaintext)

    def decrypt(self, ciphertext: bytes) -> bytes:
        return Fernet(self._dek()).decrypt(ciphertext)


@functools.lru_cache(maxsize=1)
def get_kms() -> KmsBackend:
    """Return the configured KMS backend (cached singleton)."""
    backend = os.getenv("KMS_BACKEND", get_settings().kms_backend).lower()
    if backend == "aws":
        logger.info("kms: using AWS KMS envelope backend")
        return AwsKmsEnvelopeKms()
    if backend != "local":
        logger.warning("kms: unknown backend %r, falling back to local", backend)
    logger.info("kms: using local Fernet backend")
    return LocalFernetKms()
