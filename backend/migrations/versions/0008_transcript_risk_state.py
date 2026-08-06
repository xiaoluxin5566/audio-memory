"""add transcript risk state

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_files",
        sa.Column(
            "vad_energy_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column("transcripts", sa.Column("risk_state", sa.String(length=40)))
    op.add_column(
        "transcripts",
        sa.Column(
            "is_reliable",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.add_column(
        "transcripts",
        sa.Column(
            "reliability_weight",
            sa.Float(),
            nullable=False,
            server_default=sa.text("1.0"),
        ),
    )
    op.add_column("transcripts", sa.Column("risk_reason", sa.Text()))
    with op.batch_alter_table("transcripts", recreate="always") as batch_op:
        batch_op.create_check_constraint(
            "ck_transcripts_risk_state",
            "risk_state IS NULL OR risk_state IN "
            "('REJECTED', 'HIGH_RISK_PENDING', 'POST_EDIT_PASSED', "
            "'POST_EDIT_FAILED')",
        )


def downgrade() -> None:
    with op.batch_alter_table("transcripts", recreate="always") as batch_op:
        batch_op.drop_constraint("ck_transcripts_risk_state", type_="check")
    op.drop_column("transcripts", "risk_reason")
    op.drop_column("transcripts", "reliability_weight")
    op.drop_column("transcripts", "is_reliable")
    op.drop_column("transcripts", "risk_state")
    op.drop_column("job_files", "vad_energy_json")
