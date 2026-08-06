"""normalize reanalysis pause state

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06
"""

from typing import Sequence, Union

from alembic import op


revision: str = "0007"
down_revision: Union[str, Sequence[str], None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "UPDATE reanalysis_batches SET status = 'paused' "
        "WHERE status IN "
        "('paused_credential_changed', 'paused_rules_changed', 'paused_error')"
    )


def downgrade() -> None:
    # Pause reasons remain derivable from ReanalysisItem/AnalysisVersion error_code;
    # there is no lossless reason to restore a non-canonical legacy status.
    pass
