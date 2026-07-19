"""Import-order regression guard for the lifecycle ↔ extractor cycle.

``app.llm.lifecycle`` → ``app.extractor.cost`` used to execute the package
init's eager re-exports (``agent`` → ``agent_sdk`` → ``app.llm.lifecycle``,
still partially initialized), so any process that imported
``app.llm.lifecycle`` first died with ImportError. A fresh interpreter is
the only faithful reproducer — in-process imports are already cached.
"""

from __future__ import annotations

import os
import subprocess
import sys


def test_llm_lifecycle_imports_first_without_cycle():
    env = {
        **os.environ,
        "MNEMOS_ENV": "test",
        "SECRET_KEY": "import-order-guard",
        "MNEMOS_SKIP_STARTUP_VERIFY": "1",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.llm.lifecycle; import app.extractor.agent",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
