"""Regression coverage for database dependency identity across DB reloads."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_RELOAD_PROBE = """
import importlib

import app.db as db

dependency = db.get_session
importlib.reload(db)
assert db.get_session is dependency
"""


def test_get_session_identity_survives_db_backend_reload() -> None:
    """Existing FastAPI routes and later overrides must share one callable.

    The Docker-free E2E harness reloads ``app.db`` to swap PostgreSQL for
    SQLite. A function declared directly in that module used to be recreated,
    leaving already-registered routes keyed to a stale dependency object.
    """
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": "sqlite+aiosqlite:///:memory:",
            "MNEMOS_ENV": "test",
            "SECRET_KEY": "reload-identity-test-secret",
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", _RELOAD_PROBE],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
