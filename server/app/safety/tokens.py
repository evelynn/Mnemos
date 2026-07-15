"""Opaque-token helpers used across the platform.

Single-purpose module so callers that need to hash an opaque token
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

    Break-glass grants, invitations, and project-scoped MCP API keys store
    only this digest so a DB leak does not directly yield usable tokens. The
    function is intentionally deterministic with no salt because resolution
    requires an indexed equality lookup; issuers must generate high-entropy
    opaque credentials (for example ``secrets.token_urlsafe(32)``).
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = ["hash_token"]
