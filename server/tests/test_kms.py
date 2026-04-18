"""KMS backend round-trip.

Validates that the default local backend still encrypts/decrypts after
the envelope refactor, and that the ``get_kms`` singleton honours the
``KMS_BACKEND`` env flag.
"""

import importlib
import os

from cryptography.fernet import Fernet


def _reload_kms(env: dict[str, str]):
    for k, v in env.items():
        os.environ[k] = v
    # Force fresh singleton + settings.
    from app import config, safety
    from app.safety import kms

    config.get_settings.cache_clear()
    if hasattr(kms.get_kms, "cache_clear"):
        kms.get_kms.cache_clear()
    importlib.reload(safety.kms)
    return safety.kms


def test_local_backend_round_trip(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.setenv("KMS_BACKEND", "local")
    kms = _reload_kms({})
    ct = kms.get_kms().encrypt(b"super-secret-credentials")
    assert kms.get_kms().decrypt(ct) == b"super-secret-credentials"


def test_unknown_backend_falls_back_to_local(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("FERNET_KEY", key)
    monkeypatch.setenv("KMS_BACKEND", "weird-unknown")
    kms = _reload_kms({})
    ct = kms.get_kms().encrypt(b"abc")
    assert kms.get_kms().decrypt(ct) == b"abc"
