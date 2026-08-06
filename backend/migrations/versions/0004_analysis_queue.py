"""durable analysis queue and checkpoint normalization

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_versions",
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("10"),
        ),
    )
    op.create_index(
        "uq_analysis_versions_active_source_job",
        "analysis_versions",
        ["source_job_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'running')"),
    )
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE analysis_jobs SET staged_results_json = '{}' "
            "WHERE staged_results_json IS NULL "
            "OR trim(staged_results_json) = '' "
            "OR json_type(staged_results_json) != 'object'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE analysis_versions SET staged_results_json = '{}' "
            "WHERE staged_results_json IS NULL "
            "OR trim(staged_results_json) = '' "
            "OR json_type(staged_results_json) != 'object'"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "uq_analysis_versions_active_source_job",
        table_name="analysis_versions",
    )
    op.drop_column("analysis_versions", "priority")
