"""Symmetric encryption for secrets storage.

Delegates to the pluggable KMS backend (``app.safety.kms``). The
previous behaviour — Fernet-from-env — is still the default via
``LocalFernetKms``, so single-host deployments are unaffected.
"""

import os

from app.safety.kms import get_kms


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Return (ciphertext, iv).

    Fernet stores the IV inside the token; the ``iv`` return slot is kept
    for schema compatibility — callers persist the bytes but we ignore
    them on decrypt.
    """
    iv = os.urandom(16)
    ciphertext = get_kms().encrypt(plaintext.encode())
    return ciphertext, iv


def decrypt(ciphertext: bytes, iv: bytes) -> str:  # noqa: ARG001
    return get_kms().decrypt(ciphertext).decode()
