"""Opaque-token helpers used across the platform.

Single-purpose module so callers that need to hash a break-glass token
do not have to drag in either ``app.api.break_glass`` (which would
re-introduce the import cycle the 3rd-round audit flagged in
``api.diffs`` ↔ ``api.break_glass``) or ``app.safety.crypto`` (which is
Fernet symmetric encryption — semantically the wrong neighbour for a
sha256 digest).
"""

from __future__ import annotations

import hashlib


def hash_token(token: str) -> str:
    """Return the canonical sha256 hex digest of ``token``.

    The break-glass workflow stores only this digest in
    ``diff_break_glass_grants.token_hash`` so a DB leak does not yield
    usable tokens. The function is intentionally deterministic with no
    salt — the token itself is 256 bits of entropy from
    ``secrets.token_urlsafe(32)``, so a salted hash buys nothing.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["hash_token"]
