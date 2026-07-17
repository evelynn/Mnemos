"""Safety unit tests (spec §14, §18.3)."""

import uuid

import pytest

from app.merge.contract_id import http_contract_id, normalize_http_path
from app.safety.self_review import review_diff
from app.sandbox.allowlist import is_allowed


def test_path_normalization_collapses_numeric_segments():
    assert normalize_http_path("/api/orders/123") == "/api/orders/{id}"
    assert normalize_http_path("/api/orders/:id") == "/api/orders/{id}"
    assert http_contract_id("get", "/api/orders/123?x=1") == "http.GET./api/orders/{id}"


def test_allowlist_blocks_arbitrary_commands():
    assert is_allowed("dotnet test") is True
    assert is_allowed("pytest -q tests") is True
    assert is_allowed("git status") is True
    assert is_allowed("rm -rf /") is False
    assert is_allowed("curl http://evil") is False
    assert is_allowed("git push") is False


def test_self_review_blocks_hardcoded_secret():
    diff = """+++ b/x.py
+password = \"super_secret_9999\"
"""
    result = review_diff(diff)
    assert any(f.severity == "error" and f.rule == "hardcoded_secret" for f in result.findings)


def test_self_review_blocks_drop_table():
    diff = """+++ b/x.sql
+DROP TABLE orders;
"""
    result = review_diff(diff)
    assert any(f.rule == "forbidden_sql" for f in result.findings)


def test_self_review_warns_on_todo():
    diff = """+++ b/x.py
+# TODO: fix this later
"""
    result = review_diff(diff)
    assert any(f.rule == "todo_added" and f.severity == "warning" for f in result.findings)


@pytest.mark.asyncio
async def test_second_opinion_never_persists_raw_provider_exception(monkeypatch):
    from app.review_revision import CanonicalReviewRevision
    from app.safety.review import second_opinion

    sentinel = "provider-secret-and-prompt-sentinel"
    project_id = uuid.uuid4()

    async def failing_provider(**_kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(
        second_opinion,
        "review_via_anthropic",
        failing_provider,
    )

    async def no_replay(**_kwargs):
        return second_opinion._ReplayLookup()

    monkeypatch.setattr(second_opinion, "_lookup_replay_or_attempt", no_replay)
    review = await second_opinion.run(
        None,
        project_id=project_id,
        plan_id=uuid.uuid4(),
        diff=(
            "--- a/example.py\n"
            "+++ b/example.py\n"
            "@@ -1,1 +1,1 @@\n"
            "-before = True\n"
            "+after = True\n"
        ),
        prior_findings=[],
        review_revision=CanonicalReviewRevision(
            project_id=project_id,
            run_id=uuid.uuid4(),
            source_generation=1,
            overlay_generation=0,
            git_sha="a" * 40,
        ),
    )

    assert review.coverage == "incomplete"
    assert review.failure_code == "second_opinion_provider_failed"
    assert review.findings == ()
    assert sentinel not in repr(review)


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["hardcoded_secret", "forbidden_sql"])
async def test_gate_b_self_review_errors_normalize_to_blocking_critical(rule):
    from app.safety.review import rules
    from app.safety.review.types import compute_verdict

    diff = (
        "--- a/example.py\n"
        "+++ b/example.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+password = \"secret-value\"\n"
        if rule == "hardcoded_secret"
        else (
            "--- a/schema.sql\n"
            "+++ b/schema.sql\n"
            "@@ -0,0 +1,1 @@\n"
            "+DROP TABLE orders;\n"
        )
    )
    findings = await rules.run(
        None,
        project_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        diff=diff,
    )

    matched = [finding for finding in findings if finding.rule == rule]
    assert matched and matched[0].severity == "critical"
    assert compute_verdict(findings) == "blocked"


def test_review_finding_rejects_unknown_runtime_severity():
    from app.safety.review.types import Finding

    with pytest.raises(ValueError, match="finding severity"):
        Finding(
            pass_name="rules",
            severity="error",  # type: ignore[arg-type]
            rule="bad_severity",
            location="example.py:1",
            message="must fail closed",
        )
