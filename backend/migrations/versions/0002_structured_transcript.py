"""structured transcript metadata

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_files", sa.Column("recording_started_at", sa.String(length=40))
    )
    op.add_column(
        "job_files", sa.Column("recording_time_source", sa.String(length=16))
    )
    op.add_column("job_files", sa.Column("timezone", sa.String(length=64)))
    op.add_column("job_files", sa.Column("speech_mapping_json", sa.Text()))
    op.execute(
        "UPDATE job_files SET recording_time_source = 'unknown' "
        "WHERE recording_time_source IS NULL"
    )
    op.execute(
        "UPDATE job_files SET speech_mapping_json = '[]' "
        "WHERE speech_mapping_json IS NULL"
    )
    with op.batch_alter_table("job_files", recreate="always") as batch_op:
        batch_op.alter_column(
            "recording_time_source",
            existing_type=sa.String(length=16),
            nullable=False,
        )
        batch_op.alter_column(
            "speech_mapping_json",
            existing_type=sa.Text(),
            nullable=False,
        )

    op.add_column("transcripts", sa.Column("segment_uid", sa.String(length=96)))
    op.add_column("transcripts", sa.Column("speaker_id", sa.String(length=40)))
    op.execute(
        "UPDATE transcripts "
        "SET segment_uid = job_file_id || ':' || CAST(segment_index AS TEXT) "
        "WHERE segment_uid IS NULL"
    )
    with op.batch_alter_table("transcripts", recreate="always") as batch_op:
        batch_op.alter_column(
            "segment_uid",
            existing_type=sa.String(length=96),
            nullable=False,
        )
    op.create_index(
        "uq_transcripts_segment_uid",
        "transcripts",
        ["segment_uid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_transcripts_segment_uid", table_name="transcripts")
    op.drop_column("transcripts", "speaker_id")
    op.drop_column("transcripts", "segment_uid")
    op.drop_column("job_files", "timezone")
    op.drop_column("job_files", "speech_mapping_json")
    op.drop_column("job_files", "recording_time_source")
    op.drop_column("job_files", "recording_started_at")
