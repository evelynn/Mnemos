from __future__ import annotations

import asyncio

import pytest

from app.orchestrator.failure_codes import (
    AnalysisCancelled,
    AnalysisFailure,
    exception_category,
    exception_failure_code,
    validate_stage_partial_reason,
)


@pytest.mark.parametrize(
    ("exc", "category", "analysis_code"),
    [
        (AnalysisCancelled("credential=secret"), "cancelled", "analysis_cancelled"),
        (asyncio.CancelledError("credential=secret"), "cancelled", "analysis_cancelled"),
        (TimeoutError("credential=secret"), "timeout", "analysis_timeout"),
        (
            ConnectionError("postgresql://user:secret@db.invalid/mnemos"),
            "dependency_unavailable",
            "analysis_dependency_unavailable",
        ),
        (OSError("source=secret"), "io_failure", "analysis_io_failure"),
        (ValueError("api_key=secret"), "invalid_state", "analysis_invalid_state"),
        (RuntimeError("provider-token=secret"), "internal_failure", "analysis_internal_failure"),
    ],
)
def test_exception_failure_contract_is_closed_and_message_independent(
    exc: BaseException,
    category: str,
    analysis_code: str,
) -> None:
    assert exception_category(exc) == category
    assert exception_failure_code(exc, domain="analysis") == analysis_code
    assert "secret" not in analysis_code


def test_unknown_exception_class_name_is_not_exposed() -> None:
    secret_named_error = type("ProviderTokenSecretError", (Exception,), {})
    exc = secret_named_error("dsn=postgresql://user:secret@db.invalid/mnemos")

    assert exception_category(exc) == "internal_failure"
    assert exception_failure_code(exc, domain="postprocess") == (
        "postprocess_internal_failure"
    )
    assert exception_failure_code(exc, domain="stage") == "stage_internal_failure"


def test_allowlisted_analysis_failure_preserves_only_its_static_code() -> None:
    exc = AnalysisFailure("source_ref_mismatch")

    assert exception_failure_code(exc, domain="analysis") == "source_ref_mismatch"
    assert exception_failure_code(exc, domain="postprocess") == (
        "postprocess_invalid_state"
    )


def test_analysis_failure_rejects_non_allowlisted_code_without_echoing_it() -> None:
    unsafe_code = "source_failed_api_key=secret"

    with pytest.raises(ValueError) as caught:
        AnalysisFailure(unsafe_code)  # type: ignore[arg-type]

    assert str(caught.value) == "unsupported_analysis_failure_code"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "reason",
    [
        "agent_extraction_incomplete",
        "analyzer_output_incomplete",
        "live_schema_output_incomplete",
    ],
)
def test_stage_partial_reason_allowlist(reason: str) -> None:
    assert validate_stage_partial_reason(reason) == reason


def test_stage_partial_reason_rejects_dynamic_text_without_echoing_it() -> None:
    secret_reason = "partial because api_key=secret"

    with pytest.raises(ValueError) as caught:
        validate_stage_partial_reason(secret_reason)

    assert str(caught.value) == "unsupported_stage_partial_reason"
    assert "secret" not in str(caught.value)
