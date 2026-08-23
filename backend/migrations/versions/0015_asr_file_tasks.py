"""persist resumable cloud ASR file tasks

Revision ID: 0015
Revises: 0014
"""

from alembic import op
import sqlalchemy as sa


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asr_file_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("job_id", sa.String(length=36), nullable=False),
        sa.Column("job_file_id", sa.String(length=36), nullable=False),
        sa.Column("relative_source_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("request_id", sa.String(length=36), nullable=False),
        sa.Column("storage_object_id", sa.String(length=120), nullable=True),
        sa.Column(
            "storage_status", sa.String(length=32), nullable=False, server_default="pending"
        ),
        sa.Column("remote_task_id", sa.String(length=120), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.String(length=40), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("materialized_at", sa.String(length=40), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.ForeignKeyConstraint(["job_file_id"], ["job_files.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["analysis_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_file_id", name="uq_asr_file_task_job_file"),
        sa.UniqueConstraint("request_id", name="uq_asr_file_task_request"),
    )
    op.create_index("ix_asr_file_tasks_job_id", "asr_file_tasks", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_asr_file_tasks_job_id", table_name="asr_file_tasks")
    op.drop_table("asr_file_tasks")

