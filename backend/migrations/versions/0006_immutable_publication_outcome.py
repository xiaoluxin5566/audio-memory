"""persist immutable publication outcome counts

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "analysis_versions",
        sa.Column("published_card_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "analysis_versions",
        sa.Column("published_todo_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_versions", "published_todo_count")
    op.drop_column("analysis_versions", "published_card_count")
