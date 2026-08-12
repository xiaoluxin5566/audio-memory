"""persist batch overview and native search provenance

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-12
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_versions",
        sa.Column("batch_overview_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "analysis_versions",
        sa.Column("search_rounds_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "analysis_versions",
        sa.Column("external_sources_json", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_versions", "external_sources_json")
    op.drop_column("analysis_versions", "search_rounds_json")
    op.drop_column("analysis_versions", "batch_overview_json")
