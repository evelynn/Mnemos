"""Privacy-safe failure categories for durable analysis state and events.

Exception messages and class names can contain source text, provider payloads,
credentials, or connection strings.  Orchestration boundaries therefore expose
only this closed set of deterministic categories and codes.
"""

from __future__ import annotations

import asyncio
from typing import Literal, cast


class AnalysisCancelled(RuntimeError):
    """Cooperative stop requested through the analysis-run API."""

FailureCategory = Literal[
    "cancelled",
    "budget_exceeded",
    "timeout",
    "permission_denied",
    "not_found",
    "dependency_unavailable",
    "io_failure",
    "invalid_state",
    "internal_failure",
]
FailureDomain = Literal["analysis", "postprocess", "stage"]
AnalysisFailureCode = Literal[
    "continuation_requires_published_graph",
    "full_rebuild_requires_complete_producer_coverage",
    "no_usable_source_analyzer",
    "source_changed_during_analysis",
    "source_kind_mismatch",
    "source_ref_mismatch",
    "source_root_mismatch",
    "source_tree_prefix_mismatch",
]
StagePartialReason = Literal[
    "agent_extraction_incomplete",
    "analyzer_output_incomplete",
    "live_schema_output_incomplete",
]

_STAGE_PARTIAL_REASONS: frozenset[str] = frozenset(
    {
        "agent_extraction_incomplete",
        "analyzer_output_incomplete",
        "live_schema_output_incomplete",
    }
)

_ANALYSIS_FAILURE_CODES: frozenset[str] = frozenset(
    {
        "continuation_requires_published_graph",
        "full_rebuild_requires_complete_producer_coverage",
        "no_usable_source_analyzer",
        "source_changed_during_analysis",
        "source_kind_mismatch",
        "source_ref_mismatch",
        "source_root_mismatch",
        "source_tree_prefix_mismatch",
    }
)

_FAILURE_CODES: dict[tuple[FailureDomain, FailureCategory], str] = {
    ("analysis", "cancelled"): "analysis_cancelled",
    ("analysis", "budget_exceeded"): "analysis_budget_exceeded",
    ("analysis", "timeout"): "analysis_timeout",
    ("analysis", "permission_denied"): "analysis_permission_denied",
    ("analysis", "not_found"): "analysis_not_found",
    ("analysis", "dependency_unavailable"): "analysis_dependency_unavailable",
    ("analysis", "io_failure"): "analysis_io_failure",
    ("analysis", "invalid_state"): "analysis_invalid_state",
    ("analysis", "internal_failure"): "analysis_internal_failure",
    ("postprocess", "cancelled"): "postprocess_cancelled",
    ("postprocess", "budget_exceeded"): "postprocess_budget_exceeded",
    ("postprocess", "timeout"): "postprocess_timeout",
    ("postprocess", "permission_denied"): "postprocess_permission_denied",
    ("postprocess", "not_found"): "postprocess_not_found",
    ("postprocess", "dependency_unavailable"): "postprocess_dependency_unavailable",
    ("postprocess", "io_failure"): "postprocess_io_failure",
    ("postprocess", "invalid_state"): "postprocess_invalid_state",
    ("postprocess", "internal_failure"): "postprocess_internal_failure",
    ("stage", "cancelled"): "stage_cancelled",
    ("stage", "budget_exceeded"): "stage_budget_exceeded",
    ("stage", "timeout"): "stage_timeout",
    ("stage", "permission_denied"): "stage_permission_denied",
    ("stage", "not_found"): "stage_not_found",
    ("stage", "dependency_unavailable"): "stage_dependency_unavailable",
    ("stage", "io_failure"): "stage_io_failure",
    ("stage", "invalid_state"): "stage_invalid_state",
    ("stage", "internal_failure"): "stage_internal_failure",
}

PUBLIC_FAILURE_DETAILS_UNAVAILABLE = "analysis_failure_details_unavailable"
_OPERATIONAL_FAILURE_CODES = frozenset(
    {
        PUBLIC_FAILURE_DETAILS_UNAVAILABLE,
        "analysis_enqueue_cancelled",
        "analysis_enqueue_failed",
        "analysis_job_not_enqueued",
        "analysis_project_not_found",
        "analysis_run_project_mismatch",
        "duplicate_webhook_job",
        "manual_source_path_required",
        "superseded_by_newer_webhook",
        "webhook_source_path_must_be_empty",
        "worker_cancelled_or_job_timeout",
    }
)
_PUBLIC_FAILURE_CODES = frozenset(
    {
        *_FAILURE_CODES.values(),
        *_ANALYSIS_FAILURE_CODES,
        *_STAGE_PARTIAL_REASONS,
        *_OPERATIONAL_FAILURE_CODES,
    }
)


class AnalysisFailure(RuntimeError):
    """Intentional analysis stop carrying only an allowlisted public code."""

    def __init__(self, code: AnalysisFailureCode) -> None:
        if code not in _ANALYSIS_FAILURE_CODES:
            raise ValueError("unsupported_analysis_failure_code")
        self.code = code
        super().__init__(code)


def exception_category(
    exc: BaseException,
    *,
    cancelled: bool = False,
    budget_exceeded: bool = False,
) -> FailureCategory:
    """Classify an exception without reading its message or exposing its type name."""

    if cancelled or isinstance(exc, (AnalysisCancelled, asyncio.CancelledError)):
        return "cancelled"
    if budget_exceeded:
        return "budget_exceeded"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, ConnectionError):
        return "dependency_unavailable"
    if isinstance(exc, OSError):
        return "io_failure"
    if isinstance(exc, AnalysisFailure):
        return "invalid_state"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_state"
    return "internal_failure"


def exception_failure_code(
    exc: BaseException,
    *,
    domain: FailureDomain,
    cancelled: bool = False,
    budget_exceeded: bool = False,
) -> str:
    """Return one static, bounded code from the closed domain/category matrix."""

    if domain == "analysis" and isinstance(exc, AnalysisFailure):
        return exc.code
    category = exception_category(
        exc,
        cancelled=cancelled,
        budget_exceeded=budget_exceeded,
    )
    return _FAILURE_CODES[(domain, category)]


def validate_stage_partial_reason(reason: str) -> StagePartialReason:
    """Accept only the three incomplete-output reasons emitted by the pipeline."""

    if reason not in _STAGE_PARTIAL_REASONS:
        raise ValueError("unsupported_stage_partial_reason")
    return cast(StagePartialReason, reason)


def sanitize_public_failure_code(value: object) -> str | None:
    """Project durable/legacy diagnostics into the closed public code set.

    Old deployments may already contain exception text in ``error_log``.
    Every API, SSE, and agent-context reader must therefore validate again at
    the serialization boundary instead of trusting historical database rows.
    """

    if value is None:
        return None
    if isinstance(value, str) and value in _PUBLIC_FAILURE_CODES:
        return value
    return PUBLIC_FAILURE_DETAILS_UNAVAILABLE
