from __future__ import annotations

import sqlite3

from audio_memory.db import run_migrations


def test_asr_migration_adds_resumable_file_tasks(tmp_path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    run_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(asr_file_tasks)")
        }
        index_names = [
            row[1] for row in connection.execute("PRAGMA index_list(asr_file_tasks)")
        ]
        indexed_columns = {
            tuple(
                row[2]
                for row in connection.execute(
                    f'PRAGMA index_info("{name}")'
                )
            )
            for name in index_names
        }

    assert {
        "job_id",
        "job_file_id",
        "relative_source_path",
        "sha256",
        "request_id",
        "storage_object_id",
        "storage_status",
        "remote_task_id",
        "status",
        "attempt_count",
        "next_attempt_at",
        "error_code",
        "result_json",
        "materialized_at",
    } <= columns
    assert "error_message" not in columns
    assert ("job_file_id",) in indexed_columns
    assert ("request_id",) in indexed_columns
