"""Role-based access control dependencies.

Roles (spec §14.4 actor model, pragmatic Phase-A set):
- ``admin``    — full access incl. secrets, users, destructive actions.
- ``operator`` — day-to-day: run analyses, browse data, sample refresh.
- ``viewer``   — read-only.

Routes use ``Depends(require_admin)`` etc.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from fastapi import Depends, HTTPException, status

if TYPE_CHECKING:
    from app.models.auth import User

_ORDER = {"viewer": 0, "operator": 1, "admin": 2}


def _rank(role: str) -> int:
    return _ORDER.get(role, -1)


def check_role(user_role: str, min_role: str) -> bool:
    """Pure ordering check. Unknown roles fail closed."""
    assert min_role in _ORDER, f"unknown role: {min_role}"
    return _rank(user_role) >= _rank(min_role)


def require_role(min_role: str) -> Callable[..., Awaitable["User"]]:
    """Return a FastAPI dependency that rejects users below ``min_role``."""
    # Local import keeps the cost of importing this module minimal and
    # avoids pulling the redis/session stack into offline tooling.
    from app.auth.deps import current_user

    async def _dep(user: "User" = Depends(current_user)) -> "User":
        if not check_role(user.role, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="insufficient_role"
            )
        return user

    return _dep


def __getattr__(name: str):  # noqa: D401
    """Resolve ``require_admin``/``require_operator``/``require_viewer`` lazily.

    Without this shim, importing ``rbac`` would eagerly load ``app.auth.deps``
    (and with it the redis session stack), making unit tests of this module
    unnecessarily heavy.
    """
    mapping = {
        "require_admin": "admin",
        "require_operator": "operator",
        "require_viewer": "viewer",
    }
    if name in mapping:
        return require_role(mapping[name])
    raise AttributeError(name)
