"""Fail-closed authentication and authorization for the MCP stdio server.

The stdio transport receives an opaque credential through the environment,
but possession of any non-empty string is not authorization.  This module
binds the credential to one active API key, user, organisation, and project.
The resulting context is immutable and can be revalidated on every request so
a long-lived MCP child process cannot outlive key revocation or user disablement.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.auth import ApiKey, User
from app.models.projects import Project

_MCP_ROLES = frozenset({"viewer", "operator", "admin"})
MCP_AUTHENTICATION_INVALID_CODE = "mcp_authentication_invalid"


class MCPAuthenticationError(RuntimeError):
    """The credential is not bound to the requested project."""


def mcp_authentication_error() -> dict[str, object]:
    """Return the non-disclosing error shared by every MCP auth boundary."""

    return {
        "schema": "mnemos.error.v1",
        "error": MCP_AUTHENTICATION_INVALID_CODE,
        "retryable": False,
    }


@dataclass(frozen=True, slots=True)
class MCPAuthContext:
    """Immutable identity and project boundary for one MCP process."""

    api_key_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    role: str
    organization_id: uuid.UUID
    project_id: uuid.UUID
    # The digest is needed to prove that the same key still occupies the row
    # on revalidation.  Hide it from repr so diagnostics never emit credential
    # material, even though this is already a one-way digest.
    token_hash: str = field(repr=False)

    @property
    def actor(self) -> str:
        return f"mcp:{self.username}:{self.user_id}"


def _auth_query(
    *,
    token_hash: str,
    expected_project_id: uuid.UUID,
    lock: bool,
):
    statement = (
        select(ApiKey, User, Project)
        .join(User, ApiKey.user_id == User.id)
        .join(Project, ApiKey.project_id == Project.id)
        .where(
            ApiKey.token_hash == token_hash,
            ApiKey.revoked_at.is_(None),
            ApiKey.user_id.is_not(None),
            ApiKey.project_id.is_not(None),
            ApiKey.project_id == expected_project_id,
            Project.id == expected_project_id,
            User.disabled_at.is_(None),
            User.organization_id.is_not(None),
            Project.organization_id.is_not(None),
            User.organization_id == Project.organization_id,
        )
        .execution_options(populate_existing=True)
    )
    if lock:
        # Mutation dispatch keeps these rows locked until its transaction ends.
        # SQLite ignores FOR UPDATE; PostgreSQL holds the identity boundary.
        statement = statement.with_for_update()
    return statement


async def resolve_mcp_auth_context(
    session: AsyncSession,
    *,
    token_hash: str,
    expected_project_id: uuid.UUID,
    lock: bool = False,
) -> MCPAuthContext:
    """Resolve exactly one active project-scoped API key or fail closed.

    The error is deliberately generic: random, revoked, orphaned, disabled,
    cross-organisation, cross-project, and missing credentials must not disclose
    which protected row exists.
    """

    rows = (
        await session.execute(
            _auth_query(
                token_hash=token_hash,
                expected_project_id=expected_project_id,
                lock=lock,
            )
        )
    ).all()
    if len(rows) != 1:
        raise MCPAuthenticationError("invalid_mcp_credentials")

    key, user, project = rows[0]
    if user.role not in _MCP_ROLES:
        raise MCPAuthenticationError("invalid_mcp_credentials")

    # The SQL predicates prove these are concrete.  Keep the explicit guard so
    # a future query refactor cannot turn nullable legacy rows into a wildcard.
    if (
        key.user_id is None
        or key.project_id is None
        or user.organization_id is None
        or project.organization_id is None
    ):
        raise MCPAuthenticationError("invalid_mcp_credentials")

    return MCPAuthContext(
        api_key_id=key.id,
        user_id=user.id,
        username=user.username,
        role=user.role,
        organization_id=user.organization_id,
        project_id=project.id,
        token_hash=token_hash,
    )


async def revalidate_mcp_auth_context(
    session: AsyncSession,
    *,
    context: MCPAuthContext,
    lock: bool = False,
) -> None:
    """Prove the immutable context still matches live authorization state."""

    current = await resolve_mcp_auth_context(
        session,
        token_hash=context.token_hash,
        expected_project_id=context.project_id,
        lock=lock,
    )
    if current != context:
        # Covers key replacement and user role/organisation/name changes.  A
        # caller must establish a fresh process/context after any such change.
        raise MCPAuthenticationError("invalid_mcp_credentials")


__all__ = [
    "MCP_AUTHENTICATION_INVALID_CODE",
    "MCPAuthContext",
    "MCPAuthenticationError",
    "mcp_authentication_error",
    "resolve_mcp_auth_context",
    "revalidate_mcp_auth_context",
]
