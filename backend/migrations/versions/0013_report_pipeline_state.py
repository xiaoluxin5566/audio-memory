"""persist report pipeline parameters, checkpoints, and metrics

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-13
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_versions",
        sa.Column(
            "pipeline_parameters_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "analysis_versions",
        sa.Column(
            "pipeline_parameters_fingerprint",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "analysis_versions",
        sa.Column(
            "pipeline_checkpoints_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )
    op.add_column(
        "analysis_versions",
        sa.Column(
            "pipeline_metrics_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("analysis_versions", "pipeline_metrics_json")
    op.drop_column("analysis_versions", "pipeline_checkpoints_json")
    op.drop_column("analysis_versions", "pipeline_parameters_fingerprint")
    op.drop_column("analysis_versions", "pipeline_parameters_json")
