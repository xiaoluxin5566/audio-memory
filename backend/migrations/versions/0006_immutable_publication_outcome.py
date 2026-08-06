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
    op.execute(
        sa.text(
            """
            UPDATE analysis_versions
            SET published_card_count = (
                    SELECT count(*)
                    FROM cards
                    WHERE cards.analysis_version_id = analysis_versions.id
                ),
                published_todo_count = CASE
                    WHEN EXISTS (
                        SELECT 1
                        FROM todo_candidates
                        WHERE todo_candidates.analysis_version_id = analysis_versions.id
                    ) THEN (
                        SELECT count(*)
                        FROM todo_candidates
                        WHERE todo_candidates.analysis_version_id = analysis_versions.id
                    )
                    ELSE (
                        SELECT count(*)
                        FROM todos
                        WHERE todos.analysis_version_id = analysis_versions.id
                    )
                END
            WHERE status = 'completed'
            """
        )
    )


def downgrade() -> None:
    op.drop_column("analysis_versions", "published_todo_count")
    op.drop_column("analysis_versions", "published_card_count")
