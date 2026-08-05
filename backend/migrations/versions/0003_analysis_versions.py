"""versioned analysis storage

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05
"""

from datetime import UTC, datetime
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "reanalysis_batches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("credential_generation", sa.Integer(), nullable=False),
        sa.Column("prompt_snapshot_json", sa.Text(), nullable=False),
        sa.Column("profile_snapshot_json", sa.Text(), nullable=False),
        sa.Column("fixed_rules_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "analysis_versions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("source_job_id", sa.String(length=36), nullable=False),
        sa.Column("batch_id", sa.String(length=36), nullable=True),
        sa.Column("provider_id", sa.String(length=32), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("credential_generation", sa.Integer(), nullable=False),
        sa.Column("prompt_snapshot_json", sa.Text(), nullable=False),
        sa.Column("profile_snapshot_json", sa.Text(), nullable=False),
        sa.Column("fixed_rules_hash", sa.String(length=64), nullable=False),
        sa.Column("event_map_json", sa.Text(), nullable=True),
        sa.Column("event_map_hash", sa.String(length=64), nullable=True),
        sa.Column("staged_results_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("reanalysis_batch_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_job_id"], ["analysis_jobs.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["batch_id"], ["batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["reanalysis_batch_id"],
            ["reanalysis_batches.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_analysis_versions_source_job_id",
        "analysis_versions",
        ["source_job_id"],
    )
    op.create_index(
        "ix_analysis_versions_batch_id", "analysis_versions", ["batch_id"]
    )
    op.create_index(
        "uq_analysis_versions_running_source_job",
        "analysis_versions",
        ["source_job_id"],
        unique=True,
        sqlite_where=sa.text("status = 'running'"),
    )
    op.create_table(
        "todo_candidates",
        sa.Column("id", sa.String(length=80), nullable=False),
        sa.Column("analysis_version_id", sa.String(length=36), nullable=False),
        sa.Column("source_job_id", sa.String(length=36), nullable=False),
        sa.Column("source_event_id", sa.String(length=80), nullable=False),
        sa.Column("evidence_segment_ids_json", sa.Text(), nullable=False),
        sa.Column("normalized_action", sa.Text(), nullable=False),
        sa.Column("normalized_object", sa.Text(), nullable=True),
        sa.Column("normalized_assignee", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("due_at", sa.String(length=40), nullable=True),
        sa.Column("source_fingerprint", sa.String(length=128), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_version_id"], ["analysis_versions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_job_id"], ["analysis_jobs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "analysis_version_id",
            "source_fingerprint",
            name="uq_todo_candidate_version_fingerprint",
        ),
    )
    op.create_index(
        "ix_todo_candidates_analysis_version_id",
        "todo_candidates",
        ["analysis_version_id"],
    )
    op.create_table(
        "todo_tombstones",
        sa.Column("source_fingerprint", sa.String(length=128), nullable=False),
        sa.Column("deleted_at", sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint("source_fingerprint"),
    )
    op.create_table(
        "profile_candidates",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("analysis_version_id", sa.String(length=36), nullable=False),
        sa.Column("subject_id", sa.String(length=80), nullable=False),
        sa.Column("dimension", sa.String(length=80), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("evidence_segment_ids_json", sa.Text(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.ForeignKeyConstraint(
            ["analysis_version_id"], ["analysis_versions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_profile_candidates_analysis_version_id",
        "profile_candidates",
        ["analysis_version_id"],
    )
    op.create_table(
        "reanalysis_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("reanalysis_batch_id", sa.String(length=36), nullable=False),
        sa.Column("source_batch_id", sa.String(length=36), nullable=False),
        sa.Column("analysis_version_id", sa.String(length=36), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.String(length=40), nullable=False),
        sa.Column("updated_at", sa.String(length=40), nullable=False),
        sa.Column("completed_at", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(
            ["reanalysis_batch_id"], ["reanalysis_batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_batch_id"], ["batches.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["analysis_version_id"], ["analysis_versions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "reanalysis_batch_id",
            "source_batch_id",
            name="uq_reanalysis_item_source_batch",
        ),
    )
    op.create_index(
        "ix_reanalysis_items_reanalysis_batch_id",
        "reanalysis_items",
        ["reanalysis_batch_id"],
    )

    with op.batch_alter_table("batches", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("current_analysis_version_id", sa.String(length=36))
        )
        batch_op.create_foreign_key(
            "fk_batches_current_analysis_version_id",
            "analysis_versions",
            ["current_analysis_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("cards", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("analysis_version_id", sa.String(length=36))
        )
        batch_op.create_foreign_key(
            "fk_cards_analysis_version_id",
            "analysis_versions",
            ["analysis_version_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_cards_analysis_version_id", "cards", ["analysis_version_id"]
    )
    with op.batch_alter_table("todos", recreate="always") as batch_op:
        batch_op.add_column(
            sa.Column("analysis_version_id", sa.String(length=36))
        )
        batch_op.add_column(sa.Column("source_job_id", sa.String(length=36)))
        batch_op.add_column(sa.Column("source_event_id", sa.String(length=80)))
        batch_op.add_column(
            sa.Column("evidence_segment_ids_json", sa.Text(), nullable=True)
        )
        batch_op.add_column(sa.Column("normalized_action", sa.Text()))
        batch_op.add_column(sa.Column("normalized_object", sa.Text()))
        batch_op.add_column(sa.Column("normalized_assignee", sa.Text()))
        batch_op.add_column(
            sa.Column("source_fingerprint", sa.String(length=128))
        )
        batch_op.add_column(
            sa.Column(
                "user_edited", sa.Boolean(), nullable=False, server_default=sa.false()
            )
        )
        batch_op.add_column(
            sa.Column(
                "completion_source",
                sa.String(length=16),
                nullable=False,
                server_default="model",
            )
        )
        batch_op.create_foreign_key(
            "fk_todos_analysis_version_id",
            "analysis_versions",
            ["analysis_version_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_todos_source_job_id",
            "analysis_jobs",
            ["source_job_id"],
            ["id"],
            ondelete="CASCADE",
        )
    op.create_index(
        "ix_todos_analysis_version_id", "todos", ["analysis_version_id"]
    )
    op.create_index(
        "ix_todos_source_fingerprint", "todos", ["source_fingerprint"]
    )

    _backfill_initial_versions()


def _backfill_initial_versions() -> None:
    connection = op.get_bind()
    batches = connection.execute(
        sa.text(
            "SELECT batches.id AS batch_id, batches.job_id, "
            "COALESCE(batches.provider_id, analysis_jobs.provider_id, 'unknown') "
            "AS provider_id, "
            "COALESCE(batches.model_id, analysis_jobs.model_id, 'unknown') AS model_id, "
            "analysis_jobs.prompt_snapshot_json, analysis_jobs.staged_results_json, "
            "analysis_jobs.created_at, batches.uploaded_at "
            "FROM batches JOIN analysis_jobs ON analysis_jobs.id = batches.job_id"
        )
    ).mappings()
    for batch in batches:
        version_id = str(uuid4())
        completed_at = batch["uploaded_at"] or datetime.now(UTC).isoformat()
        connection.execute(
            sa.text(
                "INSERT INTO analysis_versions "
                "(id, source_job_id, batch_id, provider_id, model_id, "
                "credential_generation, prompt_snapshot_json, profile_snapshot_json, "
                "fixed_rules_hash, event_map_json, event_map_hash, staged_results_json, "
                "status, error_code, reanalysis_batch_id, created_at, completed_at) "
                "VALUES (:id, :source_job_id, :batch_id, :provider_id, :model_id, 0, "
                ":prompt_snapshot_json, '[]', '', NULL, NULL, :staged_results_json, "
                "'completed', NULL, NULL, :created_at, :completed_at)"
            ),
            {
                "id": version_id,
                "source_job_id": batch["job_id"],
                "batch_id": batch["batch_id"],
                "provider_id": batch["provider_id"],
                "model_id": batch["model_id"],
                "prompt_snapshot_json": batch["prompt_snapshot_json"] or "{}",
                "staged_results_json": batch["staged_results_json"] or "[]",
                "created_at": batch["created_at"] or completed_at,
                "completed_at": completed_at,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE batches SET current_analysis_version_id = :version_id "
                "WHERE id = :batch_id"
            ),
            {"version_id": version_id, "batch_id": batch["batch_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE cards SET analysis_version_id = :version_id "
                "WHERE batch_id = :batch_id"
            ),
            {"version_id": version_id, "batch_id": batch["batch_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE todos SET analysis_version_id = :version_id, "
                "source_job_id = :source_job_id, user_edited = 1, "
                "source_fingerprint = 'legacy:' || id "
                "WHERE batch_id = :batch_id"
            ),
            {
                "version_id": version_id,
                "source_job_id": batch["job_id"],
                "batch_id": batch["batch_id"],
            },
        )


def downgrade() -> None:
    op.drop_index("ix_todos_source_fingerprint", table_name="todos")
    op.drop_index("ix_todos_analysis_version_id", table_name="todos")
    with op.batch_alter_table("todos", recreate="always") as batch_op:
        batch_op.drop_constraint("fk_todos_source_job_id", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_todos_analysis_version_id", type_="foreignkey"
        )
        batch_op.drop_column("completion_source")
        batch_op.drop_column("user_edited")
        batch_op.drop_column("source_fingerprint")
        batch_op.drop_column("normalized_assignee")
        batch_op.drop_column("normalized_object")
        batch_op.drop_column("normalized_action")
        batch_op.drop_column("evidence_segment_ids_json")
        batch_op.drop_column("source_event_id")
        batch_op.drop_column("source_job_id")
        batch_op.drop_column("analysis_version_id")
    op.drop_index("ix_cards_analysis_version_id", table_name="cards")
    with op.batch_alter_table("cards", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_cards_analysis_version_id", type_="foreignkey"
        )
        batch_op.drop_column("analysis_version_id")
    with op.batch_alter_table("batches", recreate="always") as batch_op:
        batch_op.drop_constraint(
            "fk_batches_current_analysis_version_id", type_="foreignkey"
        )
        batch_op.drop_column("current_analysis_version_id")
    op.drop_index(
        "ix_reanalysis_items_reanalysis_batch_id", table_name="reanalysis_items"
    )
    op.drop_table("reanalysis_items")
    op.drop_index(
        "ix_profile_candidates_analysis_version_id", table_name="profile_candidates"
    )
    op.drop_table("profile_candidates")
    op.drop_table("todo_tombstones")
    op.drop_index(
        "ix_todo_candidates_analysis_version_id", table_name="todo_candidates"
    )
    op.drop_table("todo_candidates")
    op.drop_index(
        "uq_analysis_versions_running_source_job",
        table_name="analysis_versions",
        sqlite_where=sa.text("status = 'running'"),
    )
    op.drop_index("ix_analysis_versions_batch_id", table_name="analysis_versions")
    op.drop_index(
        "ix_analysis_versions_source_job_id", table_name="analysis_versions"
    )
    op.drop_table("analysis_versions")
    op.drop_table("reanalysis_batches")
