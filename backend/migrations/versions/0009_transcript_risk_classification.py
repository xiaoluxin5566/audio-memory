"""persist transcript risk classification completion

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transcripts",
        sa.Column(
            "risk_classified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.execute(
        "UPDATE transcripts SET risk_classified = 1 "
        "WHERE risk_state IS NOT NULL OR reliability_weight = 0.6"
    )


def downgrade() -> None:
    op.drop_column("transcripts", "risk_classified")
