"""PR-148 — regression guard for the diff-submission response shape.

Re-verifying the Plan/Diff lifecycle (carried score, never exercised
end-to-end over HTTP) found POST /diff_submissions returning 500: the
handler stored ``report.as_jsonable()`` — the whole {verdict, passes,
findings} DICT — into ``auto_review_findings``, but the model, GUI and
DiffOut all expect the LIST of findings. The dict failed DiffOut's
``list[dict]`` validation on the way out.

These guards pin the contract: the review report serializes as a dict,
the findings are a list, and DiffOut accepts the list but rejects the
report dict (the bug shape).
"""

from __future__ import annotations

import os

os.environ.setdefault("MNEMOS_ENV", "test")
os.environ.setdefault("SECRET_KEY", "ci-test-pr148")
os.environ.setdefault("FERNET_KEY", "4oEY9MJGAjGCbrScyvvi4CZgm8KxFuQuklXSQwUYpys=")
os.environ.setdefault("MNEMOS_SKIP_STARTUP_VERIFY", "1")

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


def _report():
    from app.safety.review.pipeline import PassResult, ReviewReport
    from app.safety.review.types import Finding, compute_verdict

    f = Finding(
        pass_name="rules",
        severity="warning",
        rule="no_secret",
        location="x.py:1",
        message="possible secret",
    )
    return ReviewReport(
        verdict=compute_verdict([f]),
        passes=[PassResult(name="rules", position=1, findings=[f])],
        findings=[f],
    )


def test_report_jsonable_is_a_dict_findings_are_a_list():
    r = _report()
    assert isinstance(r.as_jsonable(), dict)
    findings_list = [f.as_jsonable() for f in r.findings]
    assert isinstance(findings_list, list)
    assert findings_list[0]["rule"] == "no_secret"


def test_diffout_accepts_findings_list_rejects_report_dict():
    from app.api.diffs import DiffOut

    r = _report()
    findings_list = [f.as_jsonable() for f in r.findings]

    def _mk(arf):
        return DiffOut(
            id=uuid.uuid4(),
            plan_id=uuid.uuid4(),
            task_id="task-1",
            status="pending_approval",
            diff="--- a\n+++ b\n",
            test_results=None,
            self_review_notes=None,
            auto_review_findings=arf,
            submitted_at=datetime.now(tz=timezone.utc),
            gitlab_mr_iid=None,
            gitlab_mr_url=None,
        )

    # the fixed shape (list) validates
    out = _mk(findings_list)
    assert isinstance(out.auto_review_findings, list)
    # the bug shape (the whole report dict) must fail validation
    with pytest.raises(ValidationError):
        _mk(r.as_jsonable())
