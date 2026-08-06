"""persist provider generation and worker leases

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_metadata",
        sa.Column(
            "credential_generation",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "analysis_versions",
        sa.Column("worker_owner_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "analysis_versions",
        sa.Column("lease_expires_at", sa.String(length=40), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analysis_versions", "lease_expires_at")
    op.drop_column("analysis_versions", "worker_owner_id")
    op.drop_column("provider_metadata", "credential_generation")
