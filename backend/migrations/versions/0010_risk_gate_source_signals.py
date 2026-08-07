"""persist raw VAD and optional model calibration signals

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_files",
        sa.Column(
            "vad_speech_json",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "job_files",
        sa.Column(
            "vad_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column("transcripts", sa.Column("no_speech_prob", sa.Float()))
    op.add_column("transcripts", sa.Column("avg_logprob", sa.Float()))
    op.execute(
        "UPDATE transcripts SET risk_state = 'POST_EDIT_FAILED', "
        "is_reliable = 0, reliability_weight = 0.0, text = '', "
        "words_json = '[]', risk_reason = 'legacy_risk_context_unavailable' "
        "WHERE risk_classified = 1 AND risk_state = 'POST_EDIT_PASSED'"
    )
    op.execute(
        "UPDATE transcripts SET risk_classified = 0, reliability_weight = 1.0, "
        "risk_reason = NULL WHERE risk_classified = 1 AND risk_state IS NULL "
        "AND is_reliable = 1"
    )


def downgrade() -> None:
    op.drop_column("transcripts", "avg_logprob")
    op.drop_column("transcripts", "no_speech_prob")
    op.drop_column("job_files", "vad_available")
    op.drop_column("job_files", "vad_speech_json")
