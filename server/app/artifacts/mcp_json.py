"""Generate `.mcp.json` files Claude Code can drop into a project."""

from __future__ import annotations

import uuid
from typing import Any


def build_mcp_config(project_id: uuid.UUID, *, python: str = "python") -> dict[str, Any]:
    """Return a dict suitable for JSON-dumping into .mcp.json.

    Claude Code merges servers from the project's `.mcp.json` with user-scoped
    servers; this helper produces the project-scoped entry for Mnemos.
    """
    return {
        "mcpServers": {
            "mnemos": {
                "command": python,
                "args": ["-m", "app.mcp.server", "--project", str(project_id)],
                "env": {
                    "PYTHONPATH": "/app",
                },
            }
        }
    }
