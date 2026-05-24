"""PR-96 (P1-T3): GitLab webhook secret moves to the encrypted store.

Platform reviewer #12: the webhook HMAC secret was stored as a
plaintext JSON ``PlatformSetting`` row, contradicting spec §2.8's
"no plaintext credentials" intent. Every other long-lived
credential lives in the ``Secret`` table with KMS-protected
ciphertext — this PR brings the webhook secret into the same store.

Resolution is encrypted-first: the new code reads
``Secret(label='gitlab_webhook_secret')``, decrypts via
``app.safety.crypto.decrypt``, and falls back to the legacy
``PlatformSetting`` JSON path so existing deployments keep working
until they rotate the secret into the new table.
"""

from __future__ import annotations

from pathlib import Path

_WEBHOOKS = (
    Path(__file__).resolve().parents[1] / "app" / "api" / "webhooks.py"
)


def _read() -> str:
    return _WEBHOOKS.read_text(encoding="utf-8")


def test_secret_label_constant_present():
    body = _read()
    assert '_SECRET_LABEL = "gitlab_webhook_secret"' in body


def test_secret_loader_tries_encrypted_first():
    body = _read()
    idx = body.find("async def _secret(")
    slab = body[idx:idx + 1800]
    # Encrypted path looked up by Secret.label and decrypted.
    assert "Secret.label == _SECRET_LABEL" in slab
    assert "from app.safety.crypto import decrypt" in slab
    assert "decrypt(enc.ciphertext, enc.iv)" in slab
    # And the encrypted lookup happens BEFORE the PlatformSetting fallback.
    enc_at = slab.find("Secret.label == _SECRET_LABEL")
    legacy_at = slab.find("PlatformSetting.key == _SETTING_KEY")
    assert 0 <= enc_at < legacy_at, "encrypted path must precede legacy fallback"


def test_legacy_platformsetting_still_works():
    """A deployment that hasn't rotated yet keeps working — the
    plaintext PlatformSetting JSON is read as a fallback."""
    body = _read()
    idx = body.find("async def _secret(")
    slab = body[idx:idx + 1800]
    assert "PlatformSetting" in slab
    assert '"secret"' in slab  # value.get("secret") legacy shape


def test_decrypt_failure_falls_through_not_crash():
    """A bad decrypt (mis-rotated KMS key) must NOT 503 every push —
    it logs and falls back to legacy."""
    body = _read()
    idx = body.find("async def _secret(")
    slab = body[idx:idx + 1800]
    assert "except Exception" in slab
    assert "decrypt failed" in slab.lower()
