"""Symmetric encryption for secrets storage.

Uses Fernet (AES-128-CBC + HMAC-SHA256). The key is read from settings;
when missing, a deterministic key is derived from SECRET_KEY so dev still works,
but production must set FERNET_KEY explicitly.
"""

import base64
import hashlib
import os

from cryptography.fernet import Fernet

from app.config import get_settings

_settings = get_settings()


def _key() -> bytes:
    if _settings.fernet_key:
        return _settings.fernet_key.encode()
    digest = hashlib.sha256(_settings.secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_key())


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Return (ciphertext, iv). Fernet stores the IV inside the token, so we
    return a random nonce alongside for compatibility with the schema.
    """
    iv = os.urandom(16)
    ciphertext = _fernet.encrypt(plaintext.encode())
    return ciphertext, iv


def decrypt(ciphertext: bytes, iv: bytes) -> str:  # noqa: ARG001
    return _fernet.decrypt(ciphertext).decode()
