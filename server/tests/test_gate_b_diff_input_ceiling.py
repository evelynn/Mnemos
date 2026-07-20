"""Gate-B diff input ceiling.

``DiffSubmit.diff`` is rejected at ingress (422) past
``DIFF_INPUT_MAX_CHARS``, and ``run_pipeline`` fail-closes its non-HTTP
callers with ``DiffInputTooLarge`` before any deterministic pass scans the
string.
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-diff-ceiling")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

import uuid

import pytest
from pydantic import ValidationError


def test_diff_submit_rejects_oversized_diff():
    from app.api.diffs import DiffSubmit
    from app.safety.review import DIFF_INPUT_MAX_CHARS

    with pytest.raises(ValidationError):
        DiffSubmit(
            plan_id=uuid.uuid4(),
            task_id="T1",
            diff="x" * (DIFF_INPUT_MAX_CHARS + 1),
        )


def test_diff_submit_accepts_diff_at_the_ceiling():
    from app.api.diffs import DiffSubmit
    from app.safety.review import DIFF_INPUT_MAX_CHARS

    body = DiffSubmit(
        plan_id=uuid.uuid4(),
        task_id="T1",
        diff="x" * DIFF_INPUT_MAX_CHARS,
    )
    assert len(body.diff) == DIFF_INPUT_MAX_CHARS


@pytest.mark.asyncio
async def test_run_pipeline_fail_closes_before_any_pass():
    from app.safety.review import DIFF_INPUT_MAX_CHARS, DiffInputTooLarge
    from app.safety.review.pipeline import run_pipeline

    # The ceiling guard must fire before any session/pass work — a session
    # of None proves nothing else was touched.
    with pytest.raises(DiffInputTooLarge):
        await run_pipeline(
            None,  # type: ignore[arg-type]
            project_id=uuid.uuid4(),
            plan_id=uuid.uuid4(),
            diff="x" * (DIFF_INPUT_MAX_CHARS + 1),
        )
