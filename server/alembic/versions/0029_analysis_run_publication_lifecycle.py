"""AnalysisRun source-publication and postprocess lifecycle.

Revision ID: 0029_analysis_run_lifecycle
Revises: 0028_graph_fact_overlays
Create Date: 2026-07-15

Graph promotion no longer claims that the whole analysis completed.  It
atomically records ``published`` together with the graph head and publication
receipt; derived runtime/findings/narration work then closes the run as either
``completed`` or ``partial``.  The check constraint makes that state envelope
explicit while retaining the pre-publication ``failed``/``cancelled`` states.
"""

from alembic import op

revision = "0029_analysis_run_lifecycle"
down_revision = "0028_graph_fact_overlays"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_analysis_runs_status",
        "analysis_runs",
        "status IN ('queued', 'running', 'published', 'completed', "
        "'partial', 'failed', 'cancelled')",
    )


def downgrade() -> None:
    # Pre-0029 workers do not understand either state. Mapping ``partial`` to
    # completed would overstate derived-data success, while mapping a
    # publication-in-flight run can race the postprocessor. Require an
    # operator to drain workers and make the disposition explicit.
    op.execute(
        """
        DO $mnemos$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM analysis_runs
                WHERE status IN ('published', 'partial')
                LIMIT 1
            )
            THEN
                RAISE EXCEPTION
                    'cannot downgrade 0029: analysis runs use published/partial states'
                    USING HINT =
                        'Drain all workers, then explicitly map each affected run '
                        'to a pre-0029 terminal status after reviewing its '
                        'publication receipt and postprocess outcome.';
            END IF;
        END
        $mnemos$;
        """
    )
    op.drop_constraint(
        "ck_analysis_runs_status",
        "analysis_runs",
        type_="check",
    )
