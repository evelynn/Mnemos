"""Gate-B second-opinion schema, grounding, and pipeline invariants."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest

from app.safety.review.second_opinion import SecondOpinionReview
from app.safety.review.second_opinion_schema import (
    SECOND_OPINION_CONTRACT,
    SecondOpinionSchemaError,
    collect_diff_line_evidence,
    ground_second_opinion_payload,
    normalize_second_opinion_candidate_payload,
    normalize_second_opinion_payload,
)
from app.safety.review.types import Finding


DIFF = (
    "--- a/service.py\n"
    "+++ b/service.py\n"
    "@@ -10,2 +10,2 @@\n"
    "-return old_value\n"
    "+return new_value\n"
    " context()\n"
)


def _reference(*, digest: str | None = None) -> dict:
    return {
        "kind": "diff_line",
        "path": "service.py",
        "side": "new",
        "line": 10,
        "content_sha256": digest
        or hashlib.sha256(b"return new_value").hexdigest(),
    }


def _payload(*, severity: str = "warning", message: str = "Missing guard") -> dict:
    return {
        "contract": SECOND_OPINION_CONTRACT,
        "findings": [
            {
                "severity": severity,
                "category": "correctness",
                "message": message,
                "evidence": [_reference()],
            }
        ],
    }


def test_diff_evidence_tracks_exact_old_and_new_lines_without_text_persistence():
    pack = collect_diff_line_evidence(DIFF)

    assert pack.complete is True
    assert [(line.side, line.line) for line in pack.lines] == [
        ("old", 10),
        ("new", 10),
    ]
    assert pack.lines[1].content_sha256 == hashlib.sha256(
        b"return new_value"
    ).hexdigest()
    assert "text" not in pack.lines[1].reference_value()


def test_diff_parser_does_not_confuse_changed_header_like_text_with_file_headers():
    diff = (
        "--- a/readme.txt\n"
        "+++ b/readme.txt\n"
        "@@ -1,1 +1,1 @@\n"
        "--- old heading\n"
        "+++ new heading\n"
    )

    pack = collect_diff_line_evidence(diff)

    assert pack.complete is True
    assert [(line.path, line.side, line.text) for line in pack.lines] == [
        ("readme.txt", "old", "-- old heading"),
        ("readme.txt", "new", "++ new heading"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        # A hunk that declares one new line cannot hide another changed line
        # after its counters reach zero.
        "@@ -1,1 +1,1 @@\n-old\n+new\n+hidden\n",
        "@@ -1,1 +1,1 @@\n-old\n+new\n-hidden\n",
        # EOF and a new hunk must not silently truncate declared context.
        "@@ -1,2 +1,2 @@\n-old\n+new\n",
        (
            "@@ -1,2 +1,2 @@\n-old\n+new\n"
            "@@ -4,1 +4,1 @@\n-before\n+after\n"
        ),
        # Bare text is not a unified-diff context row (which starts with a
        # single space).
        "@@ -1,2 +1,2 @@\n-old\n+new\nnot-context\n",
    ],
)
def test_diff_parser_fails_closed_on_hunk_count_or_row_grammar_drift(body):
    diff = "--- a/service.py\n+++ b/service.py\n" + body

    pack = collect_diff_line_evidence(diff)

    assert pack.complete is False


def test_diff_parser_accepts_exact_no_newline_markers_without_consuming_counts():
    diff = (
        "--- a/service.py\n"
        "+++ b/service.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
    )

    pack = collect_diff_line_evidence(diff)

    assert pack.complete is True
    assert [(line.side, line.line, line.text) for line in pack.lines] == [
        ("old", 1, "old"),
        ("new", 1, "new"),
    ]


def test_diff_parser_never_falls_back_to_the_other_side_for_invalid_paths():
    diff = (
        "--- /absolute/secret.py\n"
        "+++ b/service.py\n"
        "@@ -1,1 +1,1 @@\n"
        "-secret\n"
        "+public\n"
    )

    pack = collect_diff_line_evidence(diff)

    assert pack.complete is False
    assert [(line.path, line.side, line.text) for line in pack.lines] == [
        ("service.py", "new", "public")
    ]


def test_diff_parser_accepts_dev_null_only_for_the_empty_hunk_side():
    diff = (
        "--- /dev/null\n"
        "+++ b/new_file.py\n"
        "@@ -0,0 +1,1 @@\n"
        "+created = True\n"
    )

    pack = collect_diff_line_evidence(diff)

    assert pack.complete is True
    assert [(line.path, line.side, line.line) for line in pack.lines] == [
        ("new_file.py", "new", 1)
    ]


def test_second_opinion_normalization_is_idempotent_and_severity_is_explicit():
    canonical = normalize_second_opinion_payload(
        _payload(severity="info", message="not critical; this is informational")
    )

    assert canonical["findings"][0]["severity"] == "info"
    assert normalize_second_opinion_payload(canonical) == canonical


def test_candidate_minimization_drops_provider_prose_without_changing_product():
    provider_prose = "provider-free-prose-sentinel must never persist"
    raw = _payload(message=provider_prose)
    candidate = normalize_second_opinion_candidate_payload(raw)

    serialized = json.dumps(candidate, sort_keys=True)
    assert provider_prose not in serialized
    assert candidate["findings"][0]["message"].startswith(
        "Mnemos candidate signal correctness:"
    )
    assert normalize_second_opinion_candidate_payload(candidate) == candidate

    evidence = collect_diff_line_evidence(DIFF)
    assert (
        ground_second_opinion_payload(candidate, evidence=evidence)
        == ground_second_opinion_payload(raw, evidence=evidence)
    )


@pytest.mark.parametrize("severity", [None, "error", "CRITICAL", 1])
def test_second_opinion_rejects_unknown_severity_with_safe_path(severity):
    with pytest.raises(SecondOpinionSchemaError) as error:
        normalize_second_opinion_payload(_payload(severity=severity))

    diagnostic = str(error.value)
    assert "$.findings[0].severity" in diagnostic
    assert "Missing guard" not in diagnostic


def test_grounding_accepts_exact_diff_reference_and_drops_changed_hash():
    pack = collect_diff_line_evidence(DIFF)
    accepted = ground_second_opinion_payload(_payload(), evidence=pack)

    assert accepted.rejected_findings == 0
    assert len(accepted.findings) == 1
    assert accepted.findings[0].location == "service.py:10"
    assert accepted.findings[0].rule == "llm_correctness"
    assert accepted.findings[0].message == (
        "Independent review flagged a correctness concern at cited changed lines."
    )
    assert "text" not in accepted.findings[0].evidence[0]

    changed = _payload()
    changed["findings"][0]["evidence"][0] = _reference(digest="f" * 64)
    rejected = ground_second_opinion_payload(changed, evidence=pack)
    assert rejected.findings == ()
    assert rejected.rejected_findings == 1


def test_grounding_never_retains_provider_prose_or_copied_changed_secret():
    pack = collect_diff_line_evidence(DIFF)
    secret = 'API_KEY="sk-live-source-secret"'
    payload = _payload(message=f"The changed source contains {secret}")

    grounded = ground_second_opinion_payload(payload, evidence=pack)
    serialized = json.dumps(grounded.canonical_payload, sort_keys=True)

    assert grounded.rejected_findings == 0
    assert secret not in serialized
    assert "changed source contains" not in serialized
    assert grounded.findings[0].message == (
        "Independent review flagged a correctness concern at cited changed lines."
    )


@pytest.mark.asyncio
async def test_pipeline_does_not_revalidate_exact_diff_evidence_as_graph_node(
    monkeypatch,
):
    from app.safety.review import pipeline

    llm_finding = Finding(
        pass_name="second_opinion",
        severity="critical",
        rule="llm_security",
        location="service.py:10",
        message="Authentication is bypassed",
        evidence=[_reference()],
    )

    async def deterministic(*_args, **_kwargs):
        return []

    async def second(*_args, **_kwargs):
        return SecondOpinionReview(
            findings=(llm_finding,),
            coverage="complete",
        )

    validated_inputs: list[list[Finding]] = []

    async def validate(_session, *, project_id, findings):
        assert project_id
        validated_inputs.append(findings)
        return findings

    monkeypatch.setattr(pipeline, "_PASSES", [("rules", deterministic)])
    monkeypatch.setattr(pipeline.second_opinion, "run", second)
    monkeypatch.setattr(pipeline.validator, "validate", validate)

    report = await pipeline.run_pipeline(
        None,
        project_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        diff=DIFF,
    )

    assert validated_inputs == [[]]
    assert report.verdict == "blocked"
    assert report.findings == [llm_finding]
    assert report.coverage == "complete"


@pytest.mark.asyncio
async def test_pipeline_persists_only_static_error_code(monkeypatch):
    from app.safety.review import pipeline

    sentinel = "raw-diff-and-provider-secret-sentinel"

    async def exploding(*_args, **_kwargs):
        raise RuntimeError(sentinel)

    async def incomplete(*_args, **_kwargs):
        return SecondOpinionReview(
            coverage="incomplete",
            failure_code="second_opinion_provider_unavailable",
        )

    async def validate(_session, *, project_id, findings):
        assert project_id
        return findings

    monkeypatch.setattr(pipeline, "_PASSES", [("rules", exploding)])
    monkeypatch.setattr(pipeline.second_opinion, "run", incomplete)
    monkeypatch.setattr(pipeline.validator, "validate", validate)

    report = await pipeline.run_pipeline(
        None,
        project_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        diff=DIFF,
    )
    serialized = repr(report.as_jsonable())

    assert sentinel not in serialized
    assert report.coverage == "incomplete"
    assert report.verdict == "blocked"
    assert report.passes[0].error == "rules_failed"
    assert report.passes[1].error == "second_opinion_provider_unavailable"


@pytest.mark.asyncio
async def test_pipeline_blocks_deterministic_self_review_error_when_llm_incomplete(
    monkeypatch,
):
    from app.safety.review import pipeline, rules

    async def incomplete(*_args, **_kwargs):
        return SecondOpinionReview(
            coverage="incomplete",
            failure_code="second_opinion_provider_unavailable",
        )

    monkeypatch.setattr(pipeline, "_PASSES", [("rules", rules.run)])
    monkeypatch.setattr(pipeline.second_opinion, "run", incomplete)

    report = await pipeline.run_pipeline(
        None,
        project_id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        diff=(
            "--- a/config.py\n"
            "+++ b/config.py\n"
            "@@ -0,0 +1,1 @@\n"
            "+api_key = \"definitely-secret\"\n"
        ),
    )

    assert report.verdict == "blocked"
    assert any(
        finding.rule == "hardcoded_secret"
        and finding.severity == "critical"
        for finding in report.findings
    )


@pytest.mark.asyncio
async def test_second_opinion_uses_independent_sessions_for_lifecycle(monkeypatch):
    from app.llm.contracts import (
        AttemptKind,
        AttemptStatus,
        ResultStatus,
        UsageSource,
        unavailable_usage,
    )
    from app.llm.lifecycle import (
        AttemptOutcome,
        AttemptStartMetadata,
        AttemptTicket,
        SemanticCandidate,
        current_attempt_callbacks,
        fingerprint_input,
    )
    from app.review_revision import CanonicalReviewRevision
    from app.safety.review import second_opinion
    from app.safety.review.second_opinion_provider import (
        SECOND_OPINION_MODEL,
        SECOND_OPINION_SYSTEM_PROMPT,
        SecondOpinionProviderResult,
    )

    request_session = object()
    accounting_sessions: list[object] = []
    lifecycle_sessions: list[object] = []

    class AccountingContext:
        def __init__(self):
            self.session = object()
            accounting_sessions.append(self.session)

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *_args):
            return False

    async def budget(session, _project_id):
        lifecycle_sessions.append(session)

    async def begin(session, **kwargs):
        lifecycle_sessions.append(session)
        metadata = kwargs["metadata"]
        return AttemptTicket(
            attempt_id=uuid.uuid4(),
            operation_id=metadata.operation_id,
            budget_scope_id=kwargs["run_budget"].scope_id,
            started_at=datetime.now(tz=timezone.utc),
            remaining_seconds=60,
        )

    async def finalize(session, *, ticket, outcome, candidate=None):
        assert ticket.attempt_id
        assert outcome.result_status == ResultStatus.PENDING
        assert candidate is not None
        assert candidate.contract_name == SECOND_OPINION_CONTRACT
        lifecycle_sessions.append(session)

    async def provider(
        *, prompt, model, operation_id, binding_fingerprint
    ):
        assert model == SECOND_OPINION_MODEL
        callbacks = current_attempt_callbacks()
        assert callbacks is not None
        ticket = await callbacks.start(
            AttemptStartMetadata(
                operation_id=operation_id,
                attempt_no=1,
                attempt_kind=AttemptKind.PRIMARY,
                provider="anthropic",
                provider_mode="api",
                requested_model=model,
                input_fingerprint=fingerprint_input(
                    SECOND_OPINION_SYSTEM_PROMPT,
                    prompt,
                ),
                estimated_input_tokens=100,
                input_estimate_method="test",
                requested_max_output_tokens=1_024,
                usage_source=UsageSource.PROVIDER_API,
                billing_route="anthropic_messages_api",
            )
        )
        prompt_value = json.loads(prompt)
        evidence = dict(prompt_value["diff_lines"][1])
        evidence.pop("text")
        raw_payload = {
            "contract": SECOND_OPINION_CONTRACT,
            "findings": [
                {
                    "severity": "warning",
                    "category": "correctness",
                    "message": "The changed return value needs a guard.",
                    "evidence": [evidence],
                }
            ],
        }
        payload = normalize_second_opinion_candidate_payload(raw_payload)
        assert callbacks.finish_candidate is not None
        await callbacks.finish_candidate(
            ticket,
            AttemptOutcome(
                attempt_status=AttemptStatus.COMPLETED,
                result_status=ResultStatus.PENDING,
                usage=unavailable_usage(source=UsageSource.PROVIDER_API),
                resolved_model=model,
                finish_reason="end_turn",
            ),
            SemanticCandidate(
                contract_name=SECOND_OPINION_CONTRACT,
                binding_fingerprint=binding_fingerprint,
                payload=payload,
            ),
        )
        return SecondOpinionProviderResult(payload, ticket.attempt_id, None)

    monkeypatch.setattr(
        second_opinion,
        "_accounting_session_factory",
        AccountingContext,
    )
    monkeypatch.setattr(second_opinion, "require_budget", budget)
    monkeypatch.setattr(second_opinion, "begin_attempt", begin)
    monkeypatch.setattr(second_opinion, "finalize_attempt", finalize)
    monkeypatch.setattr(second_opinion, "review_via_anthropic", provider)

    async def no_replay(**_kwargs):
        return second_opinion._ReplayLookup()

    async def persist(**kwargs):
        grounded = kwargs["grounded"]
        identity = kwargs["identity"]
        return SecondOpinionReview(
            findings=grounded.findings,
            coverage="complete",
            operation_id=identity.operation_id,
            input_fingerprint=identity.input_fingerprint,
            attempt_id=kwargs["attempt_id"],
            canonical_payload=grounded.canonical_payload,
        )

    monkeypatch.setattr(second_opinion, "_lookup_replay_or_attempt", no_replay)
    monkeypatch.setattr(
        second_opinion,
        "_persist_product_and_classify",
        persist,
    )

    project_id = uuid.uuid4()
    review = await second_opinion.run(
        request_session,
        project_id=project_id,
        plan_id=uuid.uuid4(),
        diff=DIFF,
        prior_findings=[],
        review_revision=CanonicalReviewRevision(
            project_id=project_id,
            run_id=uuid.uuid4(),
            source_generation=1,
            overlay_generation=0,
            git_sha="a" * 40,
        ),
    )

    assert review.coverage == "complete"
    assert len(review.findings) == 1
    assert len(accounting_sessions) == 2
    assert len(set(map(id, accounting_sessions))) == 2
    assert request_session not in lifecycle_sessions
    assert set(map(id, lifecycle_sessions)) == set(map(id, accounting_sessions))
