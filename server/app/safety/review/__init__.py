"""Ultrareview-style multi-pass Gate-B review pipeline.

Design: see ``docs/ultrareview.md``. Callers use :func:`run_pipeline` which
returns a structured :class:`ReviewReport`. The Diffs API persists this
report on the DiffSubmission row and gates approval on its ``verdict``.
"""

from app.safety.review.pipeline import (
    DIFF_INPUT_MAX_CHARS,
    DiffInputTooLarge,
    Finding,
    ReviewReport,
    run_pipeline,
)

__all__ = [
    "DIFF_INPUT_MAX_CHARS",
    "DiffInputTooLarge",
    "Finding",
    "ReviewReport",
    "run_pipeline",
]
